#!/usr/bin/env python3
"""AutoScout-Engine's real-research stage.

The advancement loop used to let the model *narrate* progress ("we tried X,
found Y") with nothing behind the claim — indistinguishable from
fabrication. This stage instead makes the model propose ONE concrete,
narrow question answerable by code, actually RUNS that code in the same
sandbox verify.py uses, and treats only what the script's stdout contains as
ground truth.

The model gets exactly one way to report a result: print a line starting
with RESULT_MARKER followed by a single-line JSON object. Anything else it
claims is not trusted. A second, separate call may add prose interpretation,
but sanitize_interpretation() strips any number that doesn't actually appear
in the measured result — the same "don't trust the model to reproduce facts,
verify deterministically" pattern as advance_repo.py's sanitize_log().

If the model can't propose something runnable, a qualitative question is
allowed (per project scope) but is always logged as explicitly unverified —
never presented with the same confidence as a measured result.

The benchmark script itself is committed alongside the log entry, so a
reader can rerun it and check the claim rather than take it on faith.
"""

import json
import re

from verify import run_script_in_sandbox

RESEARCH_LOG = "RESEARCH.md"
RESULT_MARKER = "AUTOSCOUT_RESEARCH_RESULT:"
RESEARCH_MAX_TOKENS = 1800
INTERPRET_MAX_TOKENS = 300

SYSTEM_RESEARCH_PROMPT = (
    "You are AutoScout-Engine's research stage. Propose ONE concrete, narrow "
    "question about this repo's specific problem space that can be answered "
    "by writing and running a small, self-contained Python script using only "
    "this repo's existing dependencies or the standard library — e.g. "
    "comparing two algorithms/approaches on speed, memory, correctness, or "
    "output quality using synthetic or fixture data. No real API keys, no "
    "network calls, no GPU. Prefer a measurable benchmark; only fall back to "
    "a qualitative investigation (reasoning about two approaches without "
    "running code) if a benchmark genuinely isn't possible here, and say so.\n\n"
    f"Your script MUST print exactly one line starting with '{RESULT_MARKER} ' "
    "followed by a single-line JSON object of your measured metrics (e.g. "
    '{"approach_a_ms": 12.4, "approach_b_ms": 31.0}). This is the ONLY way '
    "your results are trusted — anything you claim outside this JSON line is "
    "ignored, so do not skip it.\n\n"
    "Output in EXACTLY this form:\n"
    "QUESTION: <one sentence>\n"
    "TYPE: benchmark|qualitative\n"
    "=== research/<descriptive-name>.py ===\n"
    "<script content — required only if TYPE is benchmark>\n"
    "NOTES: <required only if TYPE is qualitative — your reasoning, clearly "
    "unverified, not measured>\n"
    "Be concise."
)

PROPOSE_TEMPLATE = """\
Repo: {full_name}
Problem: {topic}
Advancement pass: {pass_num}

Prior research log (complete history, verbatim):
{research_log}

Current files:
{file_dump}

Propose the next research question per your instructions.
"""

INTERPRET_TEMPLATE = """\
Research question: {question}

You proposed and ran a benchmark script; here is the ACTUAL measured result \
— this is ground truth, do not restate different numbers or invent \
additional metrics beyond what's here:
{result_json}

Write a short (2-4 sentence) interpretation: what this measurement means for \
this project, and a concrete recommendation (adopt an approach, keep things \
as they are, or investigate further). Reference ONLY the numbers given \
above — no other numeric claim. Be concise.
"""


CONTEXT_DUMP_EXCLUDE_PREFIXES = ("research/", "eval/")


def build_propose_prompt(entry: dict, files: dict[str, str], research_log: str) -> str:
    dump = "\n\n".join(f"----- FILE: {path} -----\n{content}"
                       for path, content in files.items()
                       if not path.startswith(CONTEXT_DUMP_EXCLUDE_PREFIXES))
    return PROPOSE_TEMPLATE.format(
        full_name=entry["full_name"],
        topic=entry.get("topic", entry["name"]),
        pass_num=entry.get("advancement_passes", 0) + 1,
        research_log=research_log or "(none yet — this is the first research pass.)",
        file_dump=dump,
    )


def parse_research_proposal(raw: str) -> dict | None:
    q_match = re.search(r"QUESTION:\s*(.+)", raw)
    t_match = re.search(r"TYPE:\s*(benchmark|qualitative)", raw, re.IGNORECASE)
    if not q_match or not t_match:
        return None
    question = q_match.group(1).strip()
    kind = t_match.group(1).lower()

    if kind == "qualitative":
        notes_match = re.search(r"NOTES:\s*(.+)", raw, re.DOTALL)
        return {"question": question, "type": "qualitative",
                "notes": notes_match.group(1).strip() if notes_match else ""}

    file_match = re.search(r"=== (research/[\w.\-]+\.py) ===\n(.*?)(?=\Z)", raw, re.DOTALL)
    if not file_match:
        return None
    script_filename = file_match.group(1).strip()
    if ".." in script_filename.split("/"):
        return None
    return {"question": question, "type": "benchmark",
            "script_filename": script_filename,
            "script_content": file_match.group(2).strip()}


def extract_result(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        if line.startswith(RESULT_MARKER):
            try:
                return json.loads(line[len(RESULT_MARKER):].strip())
            except json.JSONDecodeError:
                return None
    return None


def _numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d+\.?\d*", text))


def sanitize_interpretation(interpretation: str, result: dict, question: str) -> str:
    """Only trust prose that doesn't introduce numbers absent from the
    actual measured result — a model asked to interpret real numbers can
    still slip in a fabricated one (e.g. a made-up percentage), and a
    plausible-sounding fabricated number is much harder to catch later than
    a wrong date was in sanitize_log()."""
    allowed = _numbers_in(json.dumps(result)) | _numbers_in(question)
    foreign = _numbers_in(interpretation) - allowed
    if foreign:
        return (f"(Model interpretation discarded — it referenced number(s) "
                f"{sorted(foreign)} not present in the measured data below.)")
    return interpretation.strip()


def compose_research_entry(today: str, pass_num: int, question: str, kind: str,
                           script_filename: str | None, status: str,
                           result: dict | None, interpretation: str) -> str:
    lines = [f"### {today} — pass {pass_num} research", f"**Question:** {question}"]
    if kind == "benchmark":
        if status == "ok" and result is not None:
            lines.append(f"**Script:** `{script_filename}` (committed — rerun it yourself to verify)")
            lines.append(f"**Measured result:** `{json.dumps(result)}`")
            lines.append(f"**Interpretation:** {interpretation}")
        else:
            lines.append(f"**Status:** benchmark attempted but produced no verifiable "
                         f"result ({status}) — no conclusion drawn.")
    else:
        lines.append(f"**Notes (qualitative, unverified — not a measured result):** "
                     f"{interpretation}")
    return "\n".join(lines) + "\n"


def run_research_stage(call_llm, api_key: str, entry: dict, files: dict[str, str],
                       research_log: str, today: str, pass_num: int) -> tuple[dict, str]:
    """Returns (extra_files, log_entry_text). Never raises — a failed or
    unusable proposal just yields ('', {}) so it never blocks the main
    advancement flow."""
    try:
        raw = call_llm(api_key, build_propose_prompt(entry, files, research_log),
                       SYSTEM_RESEARCH_PROMPT, max_tokens=RESEARCH_MAX_TOKENS)
    except RuntimeError as e:
        print(f"  research stage: proposal call failed: {e}")
        return {}, ""

    proposal = parse_research_proposal(raw)
    if not proposal:
        print("  research stage: no usable proposal in model output")
        return {}, ""

    if proposal["type"] == "qualitative":
        entry_text = compose_research_entry(
            today, pass_num, proposal["question"], "qualitative",
            None, "n/a", None, proposal.get("notes", "").strip() or "(no notes given)")
        return {}, entry_text

    script_filename = proposal["script_filename"]
    sandbox_files = {**files, script_filename: proposal["script_content"]}
    run = run_script_in_sandbox(sandbox_files, script_filename)

    if run["status"] != "ran":
        print(f"  research stage: benchmark script failed to run ({run['reason']})")
        entry_text = compose_research_entry(
            today, pass_num, proposal["question"], "benchmark",
            script_filename, "did not run", None, "")
        return {}, entry_text

    result = extract_result(run["output"])
    if result is None:
        print("  research stage: benchmark ran but printed no result marker")
        entry_text = compose_research_entry(
            today, pass_num, proposal["question"], "benchmark",
            script_filename, "no verifiable result", None, "")
        return {}, entry_text

    try:
        raw_interp = call_llm(
            api_key,
            INTERPRET_TEMPLATE.format(question=proposal["question"], result_json=json.dumps(result)),
            SYSTEM_RESEARCH_PROMPT, max_tokens=INTERPRET_MAX_TOKENS)
        interpretation = sanitize_interpretation(raw_interp, result, proposal["question"])
    except RuntimeError:
        interpretation = "(interpretation call failed — see measured result above.)"

    entry_text = compose_research_entry(
        today, pass_num, proposal["question"], "benchmark",
        script_filename, "ok", result, interpretation)
    return {script_filename: proposal["script_content"]}, entry_text
