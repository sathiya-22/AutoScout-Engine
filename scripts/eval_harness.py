#!/usr/bin/env python3
"""Frozen eval harness: score-gated advancement.

research.py answers one-off questions. This module answers a standing one:
"did this pass's change make the repo measurably better or worse?" On a
repo's first pass with a research key available, the model proposes a
FIXED eval/dataset.json + eval/run_eval.py once; every pass after, both
files are frozen (see freeze_eval_files()) — the model is never allowed to
rewrite the yardstick it's being measured against, only the code being
measured, regardless of what the prompt asks — freeze_eval_files() strips
any attempted edit to them deterministically, the same "don't trust
compliance" pattern as sanitize_log() and research.py's marker-only trust.

Every pass runs the same eval before and after its edit. A regression on
the deterministic score aborts the push exactly like a verify_with_retries
failure, because a real number going down is not noise. A separate,
non-gating LLM-judge opinion is also collected and logged for context, but
never itself grounds for reverting a change — a judge call has run-to-run
noise a measured score doesn't.
"""

import json
import re

from verify import extract_marker_json, run_script_in_sandbox

EVAL_DATASET = "eval/dataset.json"
EVAL_SCRIPT = "eval/run_eval.py"
EVAL_SCORES_LOG = "eval/scores.jsonl"
EVAL_MARKER = "AUTOSCOUT_EVAL_SCORE:"
PROPOSE_MAX_TOKENS = 2500
JUDGE_MAX_TOKENS = 250

SYSTEM_EVAL_PROMPT = (
    "You are AutoScout-Engine's eval-harness designer. Design a FIXED, "
    "reusable evaluation for this repo's core functionality: a small set of "
    "representative test cases and a script that scores the repo against "
    "them. This harness will be reused UNCHANGED on every future pass to "
    "measure whether changes help or hurt — so make the cases genuinely "
    "representative of the problem, not trivially easy to pass.\n\n"
    f"Write two files:\n"
    f"1. {EVAL_DATASET} — a JSON array of fixed test cases (inputs and, "
    "where applicable, expected outputs or acceptance criteria).\n"
    f"2. {EVAL_SCRIPT} — a self-contained Python script using only this "
    "repo's existing dependencies or the standard library. It must load "
    f"{EVAL_DATASET}, exercise the repo's actual core logic against each "
    "case (synthetic/fixture data only — no real API keys, no network), "
    "and print EXACTLY ONE line starting with "
    f"'{EVAL_MARKER} ' followed by a single-line JSON object that MUST "
    "include a numeric 'score' field where HIGHER IS ALWAYS BETTER (e.g. "
    "a pass rate, or an inverted latency), plus any other detail metrics "
    "you want tracked over time.\n\n"
    "Output in EXACTLY this form:\n"
    f"=== {EVAL_DATASET} ===\n<json content>\n"
    f"=== {EVAL_SCRIPT} ===\n<script content>\n"
    "Be concise."
)

PROPOSE_TEMPLATE = """\
Repo: {full_name}
Problem: {topic}

Current files:
{file_dump}

Design the fixed eval harness per your instructions.
"""

JUDGE_TEMPLATE = """\
A repo just went through one advancement pass. Give your honest opinion of \
whether this pass's change was a genuine improvement.

What changed (diff-relevant files, before -> after):
{diff_summary}

Deterministic eval score: before={before_score}, after={after_score} \
(higher is better; this is measured, not your opinion — you may agree or \
disagree with it).

Respond with ONLY a JSON object: {{"quality_score": <1-10 integer>, \
"reasoning": "<one sentence>"}}
"""


def build_propose_prompt(entry: dict, files: dict[str, str]) -> str:
    dump = "\n\n".join(f"----- FILE: {path} -----\n{content}"
                       for path, content in files.items())
    return PROPOSE_TEMPLATE.format(
        full_name=entry["full_name"], topic=entry.get("topic", entry["name"]),
        file_dump=dump)


def parse_harness_proposal(raw: str) -> dict[str, str] | None:
    pattern = r"=== (eval/[\w.\-]+) ===\n(.*?)(?==== eval/[\w.\-]+ ===|\Z)"
    files: dict[str, str] = {}
    for match in re.finditer(pattern, raw, re.DOTALL):
        path = match.group(1).strip()
        if ".." in path.split("/"):
            continue
        files[path] = match.group(2).strip()
    if EVAL_DATASET not in files or EVAL_SCRIPT not in files:
        return None
    return files


def propose_eval_harness(call_llm, api_key: str, entry: dict,
                         files: dict[str, str]) -> dict[str, str] | None:
    try:
        raw = call_llm(api_key, build_propose_prompt(entry, files),
                       SYSTEM_EVAL_PROMPT, max_tokens=PROPOSE_MAX_TOKENS)
    except RuntimeError as e:
        print(f"  eval harness: proposal call failed: {e}")
        return None
    return parse_harness_proposal(raw)


def freeze_eval_files(edited: dict[str, str], original_files: dict[str, str]) -> dict[str, str]:
    """Drops any attempted edit to the eval harness files if they already
    existed before this pass — structurally impossible to rewrite,
    regardless of whether the model tries to."""
    return {path: content for path, content in edited.items()
           if not (path in (EVAL_DATASET, EVAL_SCRIPT) and path in original_files)}


def run_eval(files: dict[str, str]) -> dict | None:
    if EVAL_SCRIPT not in files:
        return None
    run = run_script_in_sandbox(files, EVAL_SCRIPT)
    if run["status"] != "ran":
        print(f"  eval harness: {EVAL_SCRIPT} failed to run ({run['reason']})")
        return None
    result = extract_marker_json(run["output"], EVAL_MARKER)
    if result is None or "score" not in result:
        print(f"  eval harness: {EVAL_SCRIPT} produced no usable score")
        return None
    return result


def is_regression(before: dict | None, after: dict | None) -> bool:
    """True only when both scores are real numbers and after < before — an
    unavailable score on either side is inconclusive, not a regression, and
    never blocks a push on its own."""
    if not before or not after:
        return False
    try:
        return float(after["score"]) < float(before["score"])
    except (TypeError, ValueError, KeyError):
        return False


def _parse_json_object(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def judge_score(call_llm, api_key: str, diff_summary: str,
                before_score: dict | None, after_score: dict | None) -> dict | None:
    """Non-gating: a second opinion logged alongside the deterministic
    score, never itself a reason to discard a change (see module docstring)."""
    try:
        raw = call_llm(api_key, JUDGE_TEMPLATE.format(
            diff_summary=diff_summary[:2000],
            before_score=json.dumps(before_score), after_score=json.dumps(after_score)),
            "You are a terse, honest code-quality judge.", max_tokens=JUDGE_MAX_TOKENS)
    except RuntimeError as e:
        print(f"  eval harness: judge call failed: {e}")
        return None
    return _parse_json_object(raw)


def append_score_log(old_log: str, today: str, pass_num: int, score: dict | None,
                     judge: dict | None) -> str:
    entry = {"date": today, "pass": pass_num, "score": score, "judge": judge}
    line = json.dumps(entry, ensure_ascii=False)
    return (old_log.rstrip("\n") + "\n" if old_log.strip() else "") + line + "\n"
