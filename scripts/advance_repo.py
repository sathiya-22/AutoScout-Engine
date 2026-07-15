#!/usr/bin/env python3
"""AutoScout-Engine: deep advancement pass.

Each run picks the ONE repo most overdue for review, researches what's
currently happening around that repo's SPECIFIC problem (targeted HN +
GitHub searches — free, no Groq tokens spent), and asks Groq's
llama-3.3-70b-versatile to combine that research with its own knowledge to
propose and directly implement ONE substantial advancement. Commits straight
to the repo's main and logs it in that repo's own ADVANCEMENT_LOG.md.

Runs daily — this engine only needs ONE call/day, so even the 70B model's
lower 1,000-requests/day quota (vs. the 8B model's 14,400) is 1,000x more
than needed. What actually matters at this cadence is per-call token budget
(12,000 TPM here, 2x the 8B model's 6,000) and code judgment quality — a
first live run on the 8B model produced a real regression (a working Gemini
API call replaced with a hardcoded stub) and a fabricated log entry, so the
70B model was chosen specifically to reduce that risk.
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from groq_common import MODEL, call_groq, parse_sections
from registry import load_registry, pick_due_repo, save_registry, sync_registry

GITHUB_API = "https://api.github.com"
MAX_OUTPUT_TOKENS = 2000       # leaves ~10000 TPM headroom for input on a 12K TPM model

MAX_FILES_READ = 20
MAX_FILE_BYTES = 6_000
MAX_CONTEXT_CHARS = 20_000     # ≈ well under the remaining TPM budget once tokenized

ADVANCEMENT_LOG = "ADVANCEMENT_LOG.md"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
SKIP_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".lock")
USER_AGENT = "AutoScout-Engine (github.com/sathiya-22/AutoScout-Engine)"


# ── GitHub helpers ───────────────────────────────────────────────────────────

def _gh(method: str, path: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{GITHUB_API}{path}", data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"GitHub API {method} {path} failed: {e.code} "
                           f"{e.read().decode()[:300]}") from e


def get_authenticated_user(token: str) -> str:
    return _gh("GET", "/user", token)["login"]


def fetch_repo_context(full_name: str, token: str) -> dict[str, str] | None:
    tree = _gh("GET", f"/repos/{full_name}/git/trees/main?recursive=1", token)
    if not tree:
        return None
    files: dict[str, str] = {}
    total = 0
    for item in tree.get("tree", []):
        if item["type"] != "blob":
            continue
        path = item["path"]
        if any(part in SKIP_DIRS for part in path.split("/")):
            continue
        if path.endswith(SKIP_EXTS):
            continue
        if item.get("size", 0) > MAX_FILE_BYTES:
            continue
        if len(files) >= MAX_FILES_READ or total >= MAX_CONTEXT_CHARS:
            break
        blob = _gh("GET", f"/repos/{full_name}/git/blobs/{item['sha']}", token)
        if not blob:
            continue
        try:
            content = base64.b64decode(blob["content"]).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
        files[path] = content
        total += len(content)
    return files


# ── Research (free — no Groq tokens spent) ──────────────────────────────────

def _get_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  WARN: research fetch failed: {str(e)[:120]}", file=sys.stderr)
        return None


def research_topic(topic: str, gh_token: str) -> list[dict]:
    """Targeted HN + GitHub signals for this repo's SPECIFIC problem, not
    the general agentic-AI space — grounds the advancement in what's
    currently happening around that exact idea."""
    since_ts = int((datetime.now(timezone.utc) - timedelta(days=120)).timestamp())
    signals = []

    params = urllib.parse.urlencode({
        "query": topic, "tags": "story",
        "numericFilters": f"created_at_i>{since_ts}", "hitsPerPage": 8,
    })
    data = _get_json(f"https://hn.algolia.com/api/v1/search_by_date?{params}")
    for hit in (data or {}).get("hits", []):
        signals.append({
            "source": "hackernews",
            "title": hit.get("title") or "",
            "url": f"https://news.ycombinator.com/item?id={hit['objectID']}",
            "score": (hit.get("points") or 0) + (hit.get("num_comments") or 0),
        })

    headers = {"Accept": "application/vnd.github+json"}
    if gh_token:
        headers["Authorization"] = f"token {gh_token}"
    params = urllib.parse.urlencode({
        "q": f'"{topic}" in:name,description,readme',
        "sort": "stars", "order": "desc", "per_page": 8,
    })
    data = _get_json(f"https://api.github.com/search/repositories?{params}", headers)
    for r in (data or {}).get("items", []):
        signals.append({
            "source": "github-repo",
            "title": f"{r['full_name']}: {r.get('description') or ''}".strip(": "),
            "url": r.get("html_url") or "",
            "score": r.get("stargazers_count", 0),
        })

    signals = [s for s in signals if s["title"] and s["url"]]
    signals.sort(key=lambda s: -s["score"])
    return signals[:8]


# ── Prompt ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are AutoScout-Engine, a deep-advancement engineer for agentic-AI "
    "prototypes. Combine the research signals given with your own knowledge "
    "to implement ONE substantial advancement — a real step forward, not a "
    "trivial tweak. Never regenerate the whole project; only output files "
    "you are creating or changing, and never break what already works.\n\n"
    "Hard rules:\n"
    "1. NEVER replace real, working logic (a real API call, a real "
    "computation) with a stub, mock, placeholder, or simulated/fake data — "
    "that is a regression, not an advancement, even if the surrounding code "
    "looks cleaner.\n"
    "2. Only add complexity (e.g. threading, new abstractions) if it "
    "produces a real, measurable benefit — not for appearance.\n"
    "3. The advancement log is exactly what is shown to you. If it says "
    "'none yet', do not invent any prior entries — write ONLY the one new "
    "line, using the exact date given, not a guessed or remembered one."
)

ADVANCE_TEMPLATE = """\
Repo: {full_name}
Problem: {topic}
Advancement pass: {pass_num}
Today's date (use this exact date in the log, do not guess another one): {today}

Research on this SPECIFIC problem (may be sparse — use your own knowledge too):
{research}

Advancement log so far — this is the COMPLETE history, verbatim, nothing is \
hidden from you:
{advancement_log}

Current files:
{file_dump}

Pick the ONE most valuable substantial advancement (a real feature, better \
architecture, error handling, tests, or catching up to a now-standard \
technique) — bigger than a routine bugfix. Do not remove or fake any \
existing real functionality (see hard rule 1).

Output ONLY changed/new files, one header per file, in EXACTLY this form \
(real filename substituted in, never the literal word "path"):

=== <filename-or-relative-path> ===
<the file's full new content>

You MUST include an updated === {log_name} === with exactly ONE new bullet \
appended, dated {today} — keep every prior line exactly as shown above, and \
do not add any entry that isn't shown above plus this one new one.

No markdown fences inside file content. Be concise.
"""


def build_prompt(entry: dict, files: dict[str, str], research: list[dict]) -> str:
    advancement_log = files.get(ADVANCEMENT_LOG,
                                "(none yet — this is truly the first advancement "
                                "pass; do not invent any earlier entries.)")
    dump = "\n\n".join(f"----- FILE: {path} -----\n{content}"
                       for path, content in files.items())
    research_text = "\n".join(f"- [{s['source']}, score {s['score']}] {s['title']} ({s['url']})"
                              for s in research) or "(no strong external signals found)"
    return ADVANCE_TEMPLATE.format(
        full_name=entry["full_name"],
        topic=entry.get("topic", entry["name"]),
        pass_num=entry.get("advancement_passes", 0) + 1,
        today=date.today().isoformat(),
        research=research_text,
        advancement_log=advancement_log,
        file_dump=dump,
        log_name=ADVANCEMENT_LOG,
    )


def sanitize_log(old_log: str, model_log: str, today: str) -> str:
    """Rebuild the log deterministically instead of trusting the model to
    reproduce prior lines unchanged and not invent new ones: keep the real
    old_log verbatim, and append ONLY genuinely-new lines dated today. A
    70B model still fabricated a fake prior entry in testing despite being
    told explicitly not to — prompting alone isn't reliable enough here."""
    old_lines = set(old_log.splitlines())
    new_dated_lines = [line for line in model_log.splitlines()
                      if line.strip() and line not in old_lines and today in line]
    if not new_dated_lines:
        return old_log  # model didn't produce a valid dated line — caller falls back
    return (old_log.rstrip("\n") + "\n" if old_log.strip() else "") + "\n".join(new_dated_lines)


def commit_summary(old_log: str, new_log: str) -> str:
    old_lines = set(old_log.splitlines())
    for line in new_log.splitlines():
        if line.strip() and line not in old_lines:
            return line.strip("- ").strip()[:72]
    return "advancement pass"


# ── Apply changes ────────────────────────────────────────────────────────────

def push_advancement(full_name: str, files: dict[str, str], token: str,
                     pass_num: int, summary: str) -> None:
    authed_url = f"https://x-access-token:{token}@github.com/{full_name}.git"
    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = Path(tmp)

        def run(*args: str) -> None:
            subprocess.run(args, cwd=repo_dir, check=True)

        subprocess.run(["git", "clone", "--depth", "1", "-q", authed_url, str(repo_dir)],
                       check=True)
        run("git", "config", "user.name", "AutoScout Engine")
        run("git", "config", "user.email", "autoscout-engine@users.noreply.github.com")

        for filename, content in files.items():
            target = repo_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content + "\n", encoding="utf-8")
            print(f"  wrote {filename}  ({len(content):,} chars)")

        run("git", "add", "-A")
        run("git", "commit", "-q", "-m",
           f"feat(autoscout-engine): advancement pass {pass_num} — {summary} "
           f"(powered by {MODEL})")
        run("git", "push", "-q")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        print("ERROR: GROQ_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    gh_token = os.environ.get("SCOUT_PAT", "")
    if not gh_token:
        print("ERROR: SCOUT_PAT is not set.", file=sys.stderr)
        sys.exit(1)

    print("─── AutoScout-Engine: deep advancement pass ───")
    owner = get_authenticated_user(gh_token)
    registry = sync_registry(owner, gh_token)
    print(f"Registry   : {len(registry)} tracked repo(s)")

    if not registry:
        print("No repos to advance yet.")
        return

    entry = pick_due_repo(registry)
    full_name = entry["full_name"]
    pass_num = entry.get("advancement_passes", 0) + 1
    print(f"Due repo   : {full_name}  (pass {pass_num}, "
         f"last reviewed: {entry.get('last_reviewed') or 'never'})")

    files = fetch_repo_context(full_name, gh_token)
    if files is None:
        print(f"WARN: {full_name} has no 'main' branch content — "
             "dropping from registry.", file=sys.stderr)
        registry = [e for e in registry if e["full_name"] != full_name]
        save_registry(registry)
        return

    research = research_topic(entry.get("topic", entry["name"]), gh_token)
    print(f"Research   : {len(research)} signal(s) for '{entry.get('topic')}'")

    old_log = files.get(ADVANCEMENT_LOG, "")
    prompt = build_prompt(entry, files, research)

    try:
        raw = call_groq(groq_key, prompt, SYSTEM_PROMPT, max_tokens=MAX_OUTPUT_TOKENS)
    except RuntimeError as e:
        print(f"ERROR: {e} — leaving registry untouched for a retry next cycle.",
             file=sys.stderr)
        sys.exit(1)

    edited = parse_sections(raw)
    if not edited:
        print("ERROR: no sections found in model response — retry next cycle.",
             file=sys.stderr)
        print(raw[:1000], file=sys.stderr)
        sys.exit(1)

    today = date.today().isoformat()
    model_log = edited.get(ADVANCEMENT_LOG, "")
    new_log = sanitize_log(old_log, model_log, today) if model_log else old_log
    if new_log == old_log:
        # model gave no usable dated line — fall back to a generic one so
        # the log still records that a pass happened
        new_log = (old_log.rstrip("\n") + "\n" if old_log.strip() else "") + \
                 f"- {today}: advancement pass {pass_num} (see commit for details)"
    edited[ADVANCEMENT_LOG] = new_log

    summary = commit_summary(old_log, new_log)

    try:
        push_advancement(full_name, edited, gh_token, pass_num, summary)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: failed to push to {full_name}: {e} — "
             "leaving registry untouched for a retry next cycle.", file=sys.stderr)
        sys.exit(1)

    entry["advancement_passes"] = pass_num
    entry["last_reviewed"] = date.today().isoformat()
    save_registry(registry)

    print(f"\nDone — {full_name} advanced to pass {pass_num}: {summary}")


if __name__ == "__main__":
    main()
