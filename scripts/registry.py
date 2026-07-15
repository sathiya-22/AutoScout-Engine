#!/usr/bin/env python3
"""AutoScout-Engine's own registry of AutoScout-generated repos.

Deliberately independent from AutoScout-Lab's repos/registry.jsonl — same
discovery method (GitHub repos with the AutoScout description), but its own
state file and its own rotation bookkeeping, so neither repo depends on the
other's internals.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
REGISTRY_PATH = REPO_ROOT / "state" / "registry.jsonl"
GITHUB_API = "https://api.github.com"
AUTOSCOUT_DESCRIPTION = "Auto-generated AI prototype by AutoScout"


def load_registry() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    return [json.loads(line) for line in
            REGISTRY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_registry(entries: list[dict]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )


def _gh_get(path: str, token: str):
    req = urllib.request.Request(f"{GITHUB_API}{path}")
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"GitHub API GET {path} failed: {e.code}") from e


def sync_registry(owner: str, token: str) -> list[dict]:
    """Reconcile against GitHub's actual repo list: add any AutoScout repo
    missing from the registry, drop any that were deleted."""
    entries = {e["full_name"]: e for e in load_registry()}
    live_full_names: set[str] = set()

    page = 1
    while True:
        repos = _gh_get(
            f"/users/{owner}/repos?per_page=100&page={page}&sort=created&direction=asc",
            token,
        )
        if not repos:
            break
        for r in repos:
            if r.get("description") != AUTOSCOUT_DESCRIPTION:
                continue
            full_name = r["full_name"]
            live_full_names.add(full_name)
            if full_name not in entries:
                entries[full_name] = {
                    "full_name": full_name,
                    "name": r["name"],
                    "created": r["created_at"][:10],
                    "topic": r["name"].rsplit("-", 3)[0].replace("-", " "),
                    "advancement_passes": 0,
                    "last_reviewed": None,
                }
        if len(repos) < 100:
            break
        page += 1

    pruned = [e for full_name, e in entries.items() if full_name in live_full_names]
    save_registry(pruned)
    return pruned


def pick_due_repo(registry: list[dict]) -> dict | None:
    """Never-reviewed repos first (oldest created), then oldest-last-reviewed."""
    if not registry:
        return None
    return sorted(registry,
                  key=lambda r: (r.get("last_reviewed") or "0000-00-00",
                                 r.get("created", "9999-99-99")))[0]
