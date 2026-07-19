#!/usr/bin/env python3
"""Unit tests for AutoScout-Engine's pipeline logic.

Pure-logic only — no network, no Groq, no filesystem side effects.
Run: python3 -m unittest discover tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from advance_repo import (commit_summary, research_keywords,  # noqa: E402
                          sanitize_log)
from groq_common import broken_python_files, parse_sections  # noqa: E402
from registry import pick_due_repo  # noqa: E402


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
    def test_never_reviewed_first(self):
        reg = [
            {"full_name": "a/1", "created": "2026-07-10", "last_reviewed": "2026-07-13"},
            {"full_name": "a/2", "created": "2026-07-08", "last_reviewed": None},
        ]
        self.assertEqual(pick_due_repo(reg)["full_name"], "a/2")


class TestCommitSummary(unittest.TestCase):
    def test_new_line_extracted(self):
        old = "- 2026-07-01: a\n"
        new = old + "- 2026-07-15: added retries\n"
        self.assertEqual(commit_summary(old, new), "2026-07-15: added retries")


if __name__ == "__main__":
    unittest.main()
