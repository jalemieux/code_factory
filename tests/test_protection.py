"""Task 2.4 tests: timeouts, streamed logs, failure protection, janitor.

Everything runs with fakes or local subprocesses (/bin/sh) — no network,
no gh, no real claude/codex, and the repo's `.env` is never loaded.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import code_factory
import spine


class TestAgentTimeout(unittest.TestCase):
    def test_timeout_kills_process_and_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            code_factory._run_agent_command(
                ["/bin/sh", "-c", "sleep 5"], timeout=0.2
            )
        self.assertIn("timed out", str(ctx.exception))

    def test_fast_command_unaffected_by_timeout(self):
        result = code_factory._run_agent_command(
            ["/bin/sh", "-c", "echo done"], timeout=10
        )
        self.assertEqual(result, "done")

    def test_default_timeout_comes_from_module_setting(self):
        original = code_factory.PHASE_TIMEOUT_SECONDS
        code_factory.PHASE_TIMEOUT_SECONDS = 0.2
        try:
            with self.assertRaises(RuntimeError) as ctx:
                code_factory._run_agent_command(["/bin/sh", "-c", "sleep 5"])
        finally:
            code_factory.PHASE_TIMEOUT_SECONDS = original
        self.assertIn("timed out", str(ctx.exception))


class TestStreamedLogs(unittest.TestCase):
    def test_stdout_and_stderr_teed_to_log_and_stdout_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = str(Path(tmp) / "unit-phase.log")
            result = code_factory._run_agent_command(
                ["/bin/sh", "-c", "echo out-line; echo err-line >&2"],
                log_path=log_path,
            )
            self.assertEqual(result, "out-line")
            content = Path(log_path).read_text()
            self.assertIn("out-line", content)
            self.assertIn("err-line", content)

    def test_failure_still_leaves_log_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = str(Path(tmp) / "unit-phase.log")
            with self.assertRaises(RuntimeError):
                code_factory._run_agent_command(
                    ["/bin/sh", "-c", "echo partial; exit 3"],
                    log_path=log_path,
                )
            self.assertIn("partial", Path(log_path).read_text())

    def test_log_path_naming(self):
        self.assertEqual(
            code_factory._log_path("pr-42-phase4_implement"),
            str(code_factory.LOGS_DIR / "pr-42-phase4_implement.log"),
        )
        self.assertIsNone(code_factory._log_path(None))


class TestClaudeStreamResult(unittest.TestCase):
    def test_extracts_final_result_event(self):
        raw = "\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": "thinking"}}),
            json.dumps({"type": "result", "is_error": False, "result": '{"action": "approve"}'}),
        ])
        self.assertEqual(
            code_factory._claude_result_from_stream(raw), '{"action": "approve"}'
        )

    def test_error_result_raises(self):
        raw = json.dumps({"type": "result", "is_error": True, "result": "credit exhausted"})
        with self.assertRaises(RuntimeError) as ctx:
            code_factory._claude_result_from_stream(raw)
        self.assertIn("credit exhausted", str(ctx.exception))

    def test_non_stream_output_falls_through_untouched(self):
        self.assertEqual(
            code_factory._claude_result_from_stream("plain text answer"),
            "plain text answer",
        )


class FailureCounterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(
            code_factory, "FAILURE_STATE_PATH",
            Path(self._tmp.name) / "failure_counts.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_counts_increment_and_clear(self):
        self.assertEqual(code_factory.record_failure("o/r", 5), 1)
        self.assertEqual(code_factory.record_failure("o/r", 5), 2)
        self.assertEqual(code_factory.record_failure("o/r", 6), 1)  # separate PR
        code_factory.clear_failures("o/r", 5)
        self.assertEqual(code_factory.record_failure("o/r", 5), 1)

    def test_counts_survive_reload(self):
        # A new `run` invocation is a new process — state must be on disk.
        code_factory.record_failure("o/r", 9)
        self.assertEqual(code_factory._load_failure_counts()["o/r#9"], 1)

    @patch("code_factory.gh")
    @patch("code_factory.add_label")
    @patch("code_factory.remove_in_progress")
    def test_second_consecutive_failure_marks_bot_failed(self, _rm, mock_add, mock_gh):
        def failing_phase(**ctx):
            raise RuntimeError("boom")

        start = ("phase4_implement", {"repo": "o/r", "pr": {"number": 5, "title": "Fix"}})
        with patch.dict(code_factory.PHASES, {"phase4_implement": failing_phase}):
            self.assertFalse(code_factory.run_chain(start))
            mock_add.assert_not_called()  # first failure: no label yet
            self.assertFalse(code_factory.run_chain(start))
        mock_add.assert_called_once_with("o/r", 5, "bot:failed")
        # Comment posted with the log tail and retry instructions.
        comment = mock_gh.call_args[0]
        self.assertEqual(comment[0:2], ("pr", "comment"))
        body = comment[comment.index("--body") + 1]
        self.assertIn("bot:failed", body)
        self.assertIn("Log tail", body)
        # Counter reset after parking, so an unlabel gets a fresh 2 strikes.
        self.assertEqual(code_factory._load_failure_counts(), {})

    @patch("code_factory.gh")
    @patch("code_factory.add_label")
    @patch("code_factory.remove_in_progress")
    def test_success_clears_the_streak(self, _rm, mock_add, _gh):
        calls = {"n": 0}

        def flaky_phase(**ctx):
            calls["n"] += 1
            if calls["n"] in (1, 3):
                raise RuntimeError("boom")
            return None

        start = ("phase4_implement", {"repo": "o/r", "pr": {"number": 5, "title": "Fix"}})
        with patch.dict(code_factory.PHASES, {"phase4_implement": flaky_phase}):
            self.assertFalse(code_factory.run_chain(start))   # strike 1
            self.assertTrue(code_factory.run_chain(start))    # success clears
            self.assertFalse(code_factory.run_chain(start))   # strike 1 again
        mock_add.assert_not_called()

    @patch("code_factory.gh")
    @patch("code_factory.add_label")
    @patch("code_factory.remove_in_progress")
    def test_worktree_refusal_marks_failed_immediately(self, _rm, mock_add, mock_gh):
        def refusing_phase(**ctx):
            raise code_factory.WorktreeCleanupRefused("branch has 1 unpushed commit(s)")

        start = ("phase4_implement", {"repo": "o/r", "pr": {"number": 5, "title": "Fix"}})
        with patch.dict(code_factory.PHASES, {"phase4_implement": refusing_phase}):
            self.assertFalse(code_factory.run_chain(start))
        mock_add.assert_called_once_with("o/r", 5, "bot:failed")
        body = mock_gh.call_args[0][mock_gh.call_args[0].index("--body") + 1]
        self.assertIn("unpushed", body)

    def test_issue_chain_failure_does_not_touch_labels(self):
        def failing_phase(**ctx):
            raise RuntimeError("boom")

        start = ("phase1_claim_and_plan", {"repo": "o/r", "issue": {"number": 3, "title": "T"}})
        with patch.dict(code_factory.PHASES, {"phase1_claim_and_plan": failing_phase}):
            with patch("code_factory.add_label") as mock_add:
                self.assertFalse(code_factory.run_chain(start))
        mock_add.assert_not_called()


class TestLogTail(unittest.TestCase):
    def test_tail_of_existing_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "x.log"
            log.write_text("\n".join(f"line{i}" for i in range(100)))
            tail = code_factory._log_tail(str(log), lines=5)
            self.assertEqual(tail.splitlines()[0], "line95")
            self.assertEqual(tail.splitlines()[-1], "line99")

    def test_missing_log(self):
        self.assertEqual(code_factory._log_tail("/nonexistent/x.log"), "(no log captured)")
        self.assertEqual(code_factory._log_tail(None), "(no log captured)")


class TestJanitor(unittest.TestCase):
    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    @patch("code_factory.remove_in_progress")
    @patch("code_factory.gh")
    @patch("code_factory.get_in_progress_prs", return_value={7})
    def test_stale_claim_cleared(self, _prs, mock_gh, mock_remove):
        stale = datetime.now(timezone.utc) - timedelta(hours=3)
        mock_gh.return_value = self._iso(stale)
        code_factory.janitor_clear_stale_claims("o/r")
        mock_remove.assert_called_once_with("o/r", 7)

    @patch("code_factory.remove_in_progress")
    @patch("code_factory.gh")
    @patch("code_factory.get_in_progress_prs", return_value={7})
    def test_fresh_claim_kept(self, _prs, mock_gh, mock_remove):
        fresh = datetime.now(timezone.utc) - timedelta(minutes=30)
        mock_gh.return_value = self._iso(fresh)
        code_factory.janitor_clear_stale_claims("o/r")
        mock_remove.assert_not_called()

    @patch("code_factory.remove_in_progress")
    @patch("code_factory.gh")
    @patch("code_factory.get_in_progress_prs", return_value={7})
    def test_undatable_claim_left_alone(self, _prs, mock_gh, mock_remove):
        mock_gh.return_value = ""  # no labeled events found
        code_factory.janitor_clear_stale_claims("o/r")
        mock_remove.assert_not_called()

    @patch("code_factory.remove_in_progress")
    @patch("code_factory.gh")
    @patch("code_factory.get_in_progress_prs", return_value={7})
    def test_uses_latest_label_event(self, _prs, mock_gh, mock_remove):
        # Re-claimed PR: old event stale, latest fresh — keep the claim.
        old = datetime.now(timezone.utc) - timedelta(hours=5)
        recent = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_gh.return_value = f"{self._iso(old)}\n{self._iso(recent)}"
        code_factory.janitor_clear_stale_claims("o/r")
        mock_remove.assert_not_called()


class TestEnsureLabels(unittest.TestCase):
    @patch("spine.gh")
    def test_creates_every_schema_label_including_failed_and_planning(self, mock_gh):
        spine.ensure_labels("o/r")
        created = [call.args[2] for call in mock_gh.call_args_list]
        for label in ("bot:planning", "bot:plan-proposed", "bot:plan-accepted",
                      "bot:in-progress", "bot:review-requested", "bot:failed"):
            self.assertIn(label, created)


class TestGetFailedPrs(unittest.TestCase):
    @patch("spine.gh_json")
    def test_counts_only_verified_labels(self, mock_gh_json):
        mock_gh_json.return_value = [
            {"number": 1, "labels": [{"name": "bot:failed"}]},
            {"number": 2, "labels": [{"name": "bot:review-requested"}]},  # stale search hit
        ]
        self.assertEqual(spine.get_failed_prs("o/r"), {1})


if __name__ == "__main__":
    unittest.main()
