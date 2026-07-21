#!/usr/bin/env python3
"""Unit tests for AutoScout-Engine's pipeline logic.

Mostly pure-logic — no network, no Groq, no filesystem side effects —
except TestVerifyPythonRepo, which really does spin up a venv and run
generated code in a sandbox (that's the point of the module under test).
Run: python3 -m unittest discover tests -v
"""

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import advance_repo  # noqa: E402 — imported as a module so call_groq can be patched
from advance_repo import (commit_summary, research_keywords,  # noqa: E402
                          sanitize_log)
from groq_common import broken_python_files, parse_sections  # noqa: E402
from registry import pick_due_repo  # noqa: E402
from verify import verify_python_repo  # noqa: E402


class TestParseSections(unittest.TestCase):
    def test_unsafe_paths_dropped(self):
        raw = ("=== ../escape.txt ===\nbad\n\n=== /tmp/abs.txt ===\nbad\n\n"
               "=== ok.py ===\nprint(1)\n")
        self.assertEqual(list(parse_sections(raw)), ["ok.py"])


class TestBrokenPythonFiles(unittest.TestCase):
    def test_broken_detected(self):
        self.assertEqual(broken_python_files({"a.py": "def f(:\n  pass"}), ["a.py"])

    def test_valid_and_non_python_pass(self):
        self.assertEqual(broken_python_files({"a.py": "x = 1", "b.md": "((("}), [])


class TestSanitizeLog(unittest.TestCase):
    def test_fabricated_history_stripped(self):
        model_log = "- 2024-07-07: invented\n- 2026-07-15: real"
        self.assertEqual(sanitize_log("", model_log, "2026-07-15"),
                         "- 2026-07-15: real")


class TestResearchKeywords(unittest.TestCase):
    def test_stopwords_removed(self):
        kw = research_keywords("lack of robust tools for ai agents to interact with office files")
        self.assertNotIn("lack", kw)
        self.assertNotIn("of", kw)
        self.assertIn("ai", kw)
        self.assertIn("agents", kw)

    def test_capped_length(self):
        kw = research_keywords("one two three four five six seven eight")
        self.assertLessEqual(len(kw.split()), 4)


class TestRotation(unittest.TestCase):
    TODAY = __import__("datetime").date(2026, 7, 19)

    def test_never_reviewed_first(self):
        reg = [
            {"full_name": "a/1", "created": "2026-07-10", "last_reviewed": "2026-07-13"},
            {"full_name": "a/2", "created": "2026-07-08", "last_reviewed": None},
        ]
        self.assertEqual(pick_due_repo(reg, self.TODAY)["full_name"], "a/2")

    def test_starred_repo_beats_dormant(self):
        reg = [
            {"full_name": "a/1", "created": "2026-05-01", "stars": 0,
             "last_reviewed": "2026-07-10"},
            {"full_name": "a/2", "created": "2026-05-01", "stars": 3,
             "last_reviewed": "2026-07-15"},
        ]
        self.assertEqual(pick_due_repo(reg, self.TODAY)["full_name"], "a/2")

    def test_all_dormant_never_stalls(self):
        reg = [{"full_name": "a/1", "created": "2026-05-01", "stars": 0,
                "last_reviewed": "2026-07-18"}]
        self.assertIsNotNone(pick_due_repo(reg, self.TODAY))


class TestCommitSummary(unittest.TestCase):
    def test_new_line_extracted(self):
        old = "- 2026-07-01: a\n"
        new = old + "- 2026-07-15: added retries\n"
        self.assertEqual(commit_summary(old, new), "2026-07-15: added retries")


class TestVerifyPythonRepo(unittest.TestCase):
    def test_clean_exit_passes(self):
        self.assertTrue(verify_python_repo({"main.py": "print('hi')\n"})["ok"])

    def test_name_error_flagged(self):
        result = verify_python_repo({"main.py": "print(undefined_var)\n"})
        self.assertFalse(result["ok"])
        self.assertIn("NameError", result["reason"])

    def test_auth_failure_with_dummy_key_passes(self):
        code = ("import os, sys\n"
               "if os.environ.get('GROQ_API_KEY') == 'dummy-key-for-verification':\n"
               "    print('401 Unauthorized', file=sys.stderr); sys.exit(1)\n")
        self.assertTrue(verify_python_repo({"main.py": code})["ok"])


class TestVerifyWithRetries(unittest.TestCase):
    def test_succeeds_first_try_no_model_call_needed(self):
        verified, _ = advance_repo.verify_with_retries(
            "fake-key", {}, {"main.py": "print('hi')\n"})
        self.assertIsNotNone(verified)

    def test_fix_applied_on_retry(self):
        broken = {"main.py": "print(undefined_var)\n"}
        fixed_raw = "=== main.py ===\nprint('fixed')\n"
        with unittest.mock.patch("advance_repo.call_groq", return_value=fixed_raw):
            verified, _ = advance_repo.verify_with_retries("fake-key", {}, broken)
        self.assertIsNotNone(verified)
        self.assertEqual(verified["main.py"], "print('fixed')")

    def test_gives_up_after_retries_exhausted(self):
        broken = {"main.py": "print(undefined_var)\n"}
        still_broken_raw = "=== main.py ===\nprint(undefined_var)\n"
        with unittest.mock.patch("advance_repo.call_groq", return_value=still_broken_raw):
            verified, _ = advance_repo.verify_with_retries("fake-key", {}, broken)
        self.assertIsNone(verified)

    def test_untouched_dependent_file_break_is_caught(self):
        base = {"main.py": "from config import VALUE\nprint(VALUE)\n"}
        edited = {"config.py": "# VALUE removed by mistake\n"}
        verified, reason = advance_repo.verify_with_retries("fake-key", base, edited)
        self.assertIsNone(verified)
        self.assertIn("ImportError", reason)


if __name__ == "__main__":
    unittest.main()
