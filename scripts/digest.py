#!/usr/bin/env python3
"""Shared daily-activity digest: one GitHub issue per day in AutoScout-Lab,
with a section each subsystem (generate, mature, advance) fills in
independently. This is the one place to see what AutoScout did each day
across both AutoScout-Lab and AutoScout-Engine, instead of having to check
14+ scattered repos individually.

Always targets AutoScout-Lab regardless of which repo calls it — the same
GitHub PAT (SCOUT_PAT) both repos already use has the scope to post issues
cross-repo, so no new secret is needed.
"""

import json
import re
import urllib.error
import urllib.request
from datetime import date

DIGEST_OWNER = "sathiya-22"
DIGEST_REPO = "AutoScout-Lab"
GITHUB_API = "https://api.github.com"

SECTIONS = {
    "generate": "🔭 Scout + Generate",
    "mature":   "🔧 Gemini Maturation",
    "advance":  "⚡ Groq Advancement",
}


def _gh(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{GITHUB_API}{path}", data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _title(today: str) -> str:
    return f"Activity — {today}"


def _skeleton_body(today: str) -> str:
    parts = [f"Daily activity across AutoScout-Lab and AutoScout-Engine for {today}.\n"]
    for key, heading in SECTIONS.items():
        parts.append(f"## {heading}\n<!-- section:{key} -->\n_(pending)_\n<!-- /section:{key} -->\n")
    return "\n".join(parts)


def _find_or_create_issue(token: str, today: str) -> int:
    title = _title(today)
    issues = _gh("GET", f"/repos/{DIGEST_OWNER}/{DIGEST_REPO}/issues"
                        f"?state=all&per_page=30&sort=created&direction=desc", token)
    for issue in issues:
        if issue.get("title") == title:
            return issue["number"]
    created = _gh("POST", f"/repos/{DIGEST_OWNER}/{DIGEST_REPO}/issues", token, {
        "title": title,
        "body": _skeleton_body(today),
        "labels": ["autoscout-digest"],
    })
    return created["number"]


def update_section(token: str, section_key: str, content: str,
                   today: str | None = None) -> None:
    """Idempotent: re-running the same day's cycle replaces this section
    rather than duplicating it."""
    today = today or date.today().isoformat()
    try:
        number = _find_or_create_issue(token, today)
        issue = _gh("GET", f"/repos/{DIGEST_OWNER}/{DIGEST_REPO}/issues/{number}", token)
        body = issue.get("body") or _skeleton_body(today)

        start, end = f"<!-- section:{section_key} -->", f"<!-- /section:{section_key} -->"
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        replacement = f"{start}\n{content.strip()}\n{end}"
        if pattern.search(body):
            body = pattern.sub(replacement, body)
        else:
            heading = SECTIONS.get(section_key, section_key)
            body += f"\n\n## {heading}\n{replacement}\n"

        _gh("PATCH", f"/repos/{DIGEST_OWNER}/{DIGEST_REPO}/issues/{number}", token,
           {"body": body})
    except Exception as e:
        # The digest is a nice-to-have, never worth failing the actual
        # scout/generate/mature/advance run over.
        print(f"  WARN: digest update failed: {str(e)[:200]}")
