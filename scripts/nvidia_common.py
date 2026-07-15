#!/usr/bin/env python3
"""Shared helpers for AutoScout-Engine: NVIDIA NIM call wrapper + file-section
parsing (same '=== path ===' convention AutoScout-Lab uses, so a matured
repo's growth logs stay in a format both systems already understand)."""

import json
import re
import sys
import time
import urllib.error
import urllib.request

NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
# Nemotron: NVIDIA's own tuning, built for agentic/tool-use accuracy.
MODEL = "nvidia/llama-3.1-nemotron-70b-instruct"
MAX_RETRIES = 2
RETRY_BASE_SEC = 20  # back-off: 20s, 40s


def parse_sections(raw: str) -> dict[str, str]:
    """Parse '=== path ===\\ncontent' sections into {path: content}."""
    files: dict[str, str] = {}
    pattern = r"=== ([\w./\-]+) ===\n(.*?)(?==== [\w./\-]+ ===|\Z)"
    for match in re.finditer(pattern, raw, re.DOTALL):
        files[match.group(1).strip()] = match.group(2).strip()
    return files


def _is_transient(err: str) -> bool:
    if any(x in err for x in ("404", "403", "401")):
        return False
    keywords = ("429", "503", "rate limit", "overloaded", "unavailable", "retry")
    return any(k.lower() in err.lower() for k in keywords)


def call_nvidia(api_key: str, prompt: str, system: str, max_tokens: int) -> str:
    """One prompt -> response text via NVIDIA's OpenAI-compatible endpoint."""
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.5,
    }).encode()

    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(NVIDIA_API_URL, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                text = data["choices"][0]["message"]["content"]
                if not text:
                    raise RuntimeError("empty response")
                return text
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")[:300]
            err = f"{e.code} {err_body}"
        except Exception as e:
            err = str(e)

        if _is_transient(err) and attempt < MAX_RETRIES:
            wait = RETRY_BASE_SEC * (2 ** (attempt - 1))
            print(f"  Transient error (attempt {attempt}/{MAX_RETRIES}), "
                 f"retrying in {wait}s... [{err[:150]}]", flush=True)
            time.sleep(wait)
        else:
            print(f"  NVIDIA call failed after {attempt} attempt(s): {err[:200]}",
                 file=sys.stderr)
            break

    raise RuntimeError("NVIDIA API call exhausted retries")
