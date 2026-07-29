#!/usr/bin/env python3
"""Unit tests for AutoScout-Engine's pipeline logic.

Mostly pure-logic — no network, no Groq, no filesystem side effects —
except TestVerifyPythonRepo, which really does spin up a venv and run
generated code in a sandbox (that's the point of the module under test).
Run: python3 -m unittest discover tests -v
"""

import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import advance_repo  # noqa: E402 — imported as a module so call_groq can be patched
from advance_repo import (commit_summary, research_keywords,  # noqa: E402
                          sanitize_log)
from eval_harness import (append_score_log, freeze_eval_files,  # noqa: E402
                          is_regression, judge_score, parse_harness_proposal,
                          run_eval)
from groq_common import broken_python_files, parse_sections  # noqa: E402
from registry import pick_due_repo  # noqa: E402
from research import (extract_result, parse_research_proposal,  # noqa: E402
                      run_research_stage, sanitize_interpretation)
from verify import _classify_failure, verify_python_repo  # noqa: E402


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

class TestClassifyFailureCliUsage(unittest.TestCase):
    def test_rich_boxed_missing_command_is_benign(self):
        # A Typer/Click CLI with >1 @app.command() exits 2 with a rich-styled
        # box when run with zero args — the last line is just a box border,
        # not a recognizable "SomeError:" (see 2026-07-29 incident).
        stderr = (
            "Usage: main.py [OPTIONS] COMMAND [ARGS]...\n"
            "╭─ Error ────────────────────────────────────────────\n"
            "│ Missing command. │\n"
            "╰────────────────────────────────────────────╯\n"
        )
        ok, reason = _classify_failure(stderr)
        self.assertTrue(ok)
        self.assertIn("subcommand", reason)


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
        still_broken_raw = "=== config.py ===\n# still broken\n"
        with unittest.mock.patch("advance_repo.call_groq", return_value=still_broken_raw):
            verified, reason = advance_repo.verify_with_retries("fake-key", base, edited)
        self.assertIsNone(verified)
        self.assertIn("ImportError", reason)


class TestResearchProposalParsing(unittest.TestCase):
    def test_benchmark_proposal_parsed(self):
        raw = ("QUESTION: is A faster than B?\nTYPE: benchmark\n"
              "=== research/bench.py ===\n"
              "print('AUTOSCOUT_RESEARCH_RESULT: {\"a\": 1}')\n")
        parsed = parse_research_proposal(raw)
        self.assertEqual(parsed["type"], "benchmark")
        self.assertEqual(parsed["script_filename"], "research/bench.py")

    def test_path_traversal_rejected(self):
        raw = ("QUESTION: q\nTYPE: benchmark\n"
              "=== research/../../evil.py ===\nprint('hi')\n")
        self.assertIsNone(parse_research_proposal(raw))


class TestExtractResult(unittest.TestCase):
    def test_extracts_json_after_marker(self):
        stdout = "noise\nAUTOSCOUT_RESEARCH_RESULT: {\"ms\": 4.2}\n"
        self.assertEqual(extract_result(stdout), {"ms": 4.2})

    def test_no_marker_returns_none(self):
        self.assertIsNone(extract_result("nothing here\n"))


class TestSanitizeInterpretation(unittest.TestCase):
    def test_fabricated_number_discarded(self):
        result = {"a_ms": 5.0}
        text = "This is a 90% speedup over the baseline."
        self.assertIn("discarded", sanitize_interpretation(text, result, "q"))

    def test_clean_interpretation_kept(self):
        result = {"a_ms": 5.0}
        text = "Approach at 5.0ms — acceptable."
        self.assertEqual(sanitize_interpretation(text, result, "q"), text)


class TestRunResearchStage(unittest.TestCase):
    def test_benchmark_success_produces_entry_and_script_file(self):
        proposal_raw = ("QUESTION: is A faster?\nTYPE: benchmark\n"
                        "=== research/bench.py ===\n"
                        "print('AUTOSCOUT_RESEARCH_RESULT: {\"a_ms\": 1.0}')\n")
        interp_raw = "A at 1.0ms is fast enough — no change needed."
        calls = iter([proposal_raw, interp_raw])
        call_llm = unittest.mock.Mock(side_effect=lambda *a, **k: next(calls))

        extra_files, entry_text = run_research_stage(
            call_llm, "fake-key",
            {"full_name": "x/y", "name": "y", "topic": "t", "advancement_passes": 0},
            {"main.py": "print('hi')\n"}, "", "2026-07-27", 1)

        self.assertIn("research/bench.py", extra_files)
        self.assertIn("Measured result", entry_text)

    def test_no_proposal_yields_nothing(self):
        call_llm = unittest.mock.Mock(return_value="garbage")
        extra_files, entry_text = run_research_stage(
            call_llm, "fake-key",
            {"full_name": "x/y", "name": "y", "topic": "t", "advancement_passes": 0},
            {"main.py": "print('hi')\n"}, "", "2026-07-27", 1)
        self.assertEqual(extra_files, {})
        self.assertEqual(entry_text, "")


class TestEvalHarnessParsing(unittest.TestCase):
    def test_valid_proposal_parsed(self):
        raw = ("=== eval/dataset.json ===\n[{\"input\": 1}]\n"
              "=== eval/run_eval.py ===\nprint('AUTOSCOUT_EVAL_SCORE: {\"score\": 1.0}')\n")
        parsed = parse_harness_proposal(raw)
        self.assertIn("eval/dataset.json", parsed)
        self.assertIn("eval/run_eval.py", parsed)

    def test_missing_dataset_rejected(self):
        raw = "=== eval/run_eval.py ===\nprint('x')\n"
        self.assertIsNone(parse_harness_proposal(raw))


class TestFreezeEvalFiles(unittest.TestCase):
    def test_pre_existing_eval_files_stripped(self):
        original = {"eval/dataset.json": "[]", "eval/run_eval.py": "old"}
        edited = {"eval/run_eval.py": "model tried to rewrite this", "main.py": "print(1)"}
        result = freeze_eval_files(edited, original)
        self.assertNotIn("eval/run_eval.py", result)
        self.assertIn("main.py", result)

    def test_newly_created_eval_files_not_stripped(self):
        edited = {"eval/run_eval.py": "brand new"}
        self.assertIn("eval/run_eval.py", freeze_eval_files(edited, {}))


class TestIsRegression(unittest.TestCase):
    def test_lower_after_is_regression(self):
        self.assertTrue(is_regression({"score": 5.0}, {"score": 3.0}))

    def test_missing_score_is_inconclusive(self):
        self.assertFalse(is_regression(None, {"score": 1.0}))


class TestJudgeScore(unittest.TestCase):
    def test_parses_json_from_model_response(self):
        call_llm = unittest.mock.Mock(
            return_value='{"quality_score": 6, "reasoning": "ok"}')
        result = judge_score(call_llm, "fake-key", "diff", {"score": 1}, {"score": 2})
        self.assertEqual(result["quality_score"], 6)


class TestAppendScoreLog(unittest.TestCase):
    def test_appends_valid_json_line(self):
        log = append_score_log("", "2026-07-27", 1, {"score": 3.0}, None)
        entry = json.loads(log.strip())
        self.assertEqual(entry["pass"], 1)


class TestRunEvalNoScript(unittest.TestCase):
    def test_missing_script_returns_none(self):
        self.assertIsNone(run_eval({"main.py": "print(1)"}))


if __name__ == "__main__":
    unittest.main()
