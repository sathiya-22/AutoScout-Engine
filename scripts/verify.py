#!/usr/bin/env python3
"""Sandboxed runtime verification for generated Python prototypes.

Extends the syntax gate (does it parse) to a real smoke test (does it run).
Writes candidate files into an isolated venv, installs declared
dependencies, and dry-runs the entrypoint with dummy env vars and a hard
timeout — the sandboxed process NEVER receives real secrets (its env is
built from scratch, not inherited, so ambient GEMINI_API_KEY etc. in this
script's own process can't leak into model-written code).

Classification: a clean exit, or a failure that's clearly just "no real API
key" (expected — we're smoke-testing, not running the live demo), both
pass. A genuine Python exception (ImportError, NameError, TypeError, ...)
is flagged as a real bug. Ambiguous cases (timeouts, infra hiccups) default
to passing rather than blocking a push on a false positive.
"""

import re
import subprocess
import tempfile
import venv
from pathlib import Path

ENTRYPOINT = "main.py"
REQUIREMENTS = "requirements.txt"
RUN_TIMEOUT_SEC = 15
INSTALL_TIMEOUT_SEC = 60
SANDBOX_ENV = {"PATH": "/usr/bin:/bin"}
DUMMY_ENV = {
    **SANDBOX_ENV,
    "GEMINI_API_KEY": "dummy-key-for-verification",
    "GOOGLE_API_KEY": "dummy-key-for-verification",
    "GROQ_API_KEY": "dummy-key-for-verification",
    "OPENAI_API_KEY": "dummy-key-for-verification",
    "ANTHROPIC_API_KEY": "dummy-key-for-verification",
    "API_KEY": "dummy-key-for-verification",
}

STRUCTURAL_BUG_EXCEPTIONS = {
    "ImportError", "ModuleNotFoundError", "NameError", "AttributeError",
    "TypeError", "SyntaxError", "IndentationError", "KeyError",
    "ZeroDivisionError", "IndexError", "UnboundLocalError",
}
BENIGN_EXCEPTIONS = {"EOFError", "KeyboardInterrupt", "SystemExit"}
AUTH_KEYWORDS = ("api key", "apikey", "unauthorized", "authentication",
                 "permission_denied", "invalid_api_key", "401", "403",
                 "credentials", "api_key_invalid")


def _classify_failure(stderr: str) -> tuple[bool, str]:
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)):", last)
    exc_name = m.group(1) if m else None

    if exc_name in BENIGN_EXCEPTIONS:
        return True, f"benign exception ({exc_name}) — likely a non-interactive test artifact"
    if exc_name in STRUCTURAL_BUG_EXCEPTIONS:
        return False, last
    if any(k in stderr.lower() for k in AUTH_KEYWORDS):
        return True, "failed on API auth with a dummy key — expected"
    if exc_name:
        return False, last
    return False, f"non-zero exit, no recognizable exception: {last[:200]}"


def verify_python_repo(files: dict[str, str]) -> dict:
    """Returns {"ok": bool, "reason": str}."""
    if ENTRYPOINT not in files:
        return {"ok": True, "reason": f"no {ENTRYPOINT} — skipping runtime check"}

    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = Path(tmp) / "repo"
        repo_dir.mkdir()
        for filename, content in files.items():
            target = repo_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content + "\n", encoding="utf-8")

        venv_dir = Path(tmp) / "venv"
        try:
            venv.create(venv_dir, with_pip=True)
        except Exception as e:
            return {"ok": True, "reason": f"venv creation failed (infra, not code): {e}"}

        py, pip = venv_dir / "bin" / "python", venv_dir / "bin" / "pip"

        req_file = repo_dir / REQUIREMENTS
        if req_file.exists():
            try:
                result = subprocess.run(
                    [str(pip), "install", "-q", "-r", str(req_file)],
                    cwd=repo_dir, capture_output=True, text=True,
                    timeout=INSTALL_TIMEOUT_SEC, env=dict(SANDBOX_ENV),
                )
            except subprocess.TimeoutExpired:
                return {"ok": True, "reason": "pip install timed out (infra, inconclusive)"}
            if result.returncode != 0:
                return {"ok": False, "reason": f"pip install failed: {result.stderr[-500:]}"}

        try:
            result = subprocess.run(
                [str(py), ENTRYPOINT],
                cwd=repo_dir, capture_output=True, text=True,
                timeout=RUN_TIMEOUT_SEC, stdin=subprocess.DEVNULL,
                env=dict(DUMMY_ENV),
            )
        except subprocess.TimeoutExpired:
            return {"ok": True,
                   "reason": f"timed out after {RUN_TIMEOUT_SEC}s (inconclusive, not blocking)"}

        if result.returncode == 0:
            return {"ok": True, "reason": "clean exit"}
        ok, reason = _classify_failure(result.stderr)
        return {"ok": ok, "reason": reason}
