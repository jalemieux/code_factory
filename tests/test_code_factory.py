import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import code_factory


class TestGh(unittest.TestCase):
    @patch("code_factory.subprocess.run")
    def test_gh_returns_stdout_on_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="  output\n", stderr="")
        result = code_factory.gh("repo", "view")
        self.assertEqual(result, "output")
        mock_run.assert_called_once_with(
            ["gh", "repo", "view"], capture_output=True, text=True
        )

    @patch("code_factory.subprocess.run")
    def test_gh_raises_on_non_rate_limit_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        with self.assertRaises(RuntimeError) as ctx:
            code_factory.gh("repo", "view")
        self.assertIn("not found", str(ctx.exception))

    @patch("code_factory.time.sleep")
    @patch("code_factory.subprocess.run")
    def test_gh_retries_on_rate_limit(self, mock_run, mock_sleep):
        fail = MagicMock(returncode=1, stdout="", stderr="API rate limit exceeded")
        success = MagicMock(returncode=0, stdout="ok", stderr="")
        mock_run.side_effect = [fail, success]
        result = code_factory.gh("pr", "list")
        self.assertEqual(result, "ok")
        self.assertEqual(mock_run.call_count, 2)
        mock_sleep.assert_called_once_with(15)


class TestGhJson(unittest.TestCase):
    @patch("code_factory.gh")
    def test_gh_json_parses_list(self, mock_gh):
        mock_gh.return_value = '[{"number": 1}]'
        result = code_factory.gh_json("pr", "list")
        self.assertEqual(result, [{"number": 1}])

    @patch("code_factory.gh")
    def test_gh_json_returns_empty_list_for_empty_string(self, mock_gh):
        mock_gh.return_value = ""
        result = code_factory.gh_json("pr", "list")
        self.assertEqual(result, [])


class TestSlugify(unittest.TestCase):
    def test_basic_slug(self):
        self.assertEqual(code_factory.slugify("Fix the Bug"), "fix-the-bug")

    def test_special_chars(self):
        self.assertEqual(code_factory.slugify("Add feature! (v2)"), "add-feature-v2")

    def test_truncates_at_40(self):
        long_title = "a" * 60
        self.assertEqual(len(code_factory.slugify(long_title)), 40)

    def test_strips_trailing_hyphens(self):
        self.assertEqual(code_factory.slugify("test---"), "test")


class TestLoadPrompt(unittest.TestCase):
    def test_loads_and_interpolates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prompts = Path(tmpdir)
            (prompts / "test_phase.md").write_text("Issue #{issue_number}: {title}")
            original = code_factory.PROMPTS_DIR
            code_factory.PROMPTS_DIR = prompts
            try:
                result = code_factory.load_prompt(
                    "test_phase", issue_number="42", title="Fix bug"
                )
                self.assertEqual(result, "Issue #42: Fix bug")
            finally:
                code_factory.PROMPTS_DIR = original

    def test_literal_braces_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prompts = Path(tmpdir)
            (prompts / "json_phase.md").write_text('{{"action": "{action}"}}')
            original = code_factory.PROMPTS_DIR
            code_factory.PROMPTS_DIR = prompts
            try:
                result = code_factory.load_prompt("json_phase", action="approve")
                self.assertEqual(result, '{"action": "approve"}')
            finally:
                code_factory.PROMPTS_DIR = original


class TestCheckReviewRequested(unittest.TestCase):
    @patch("code_factory.gh_json")
    def test_returns_pr_when_review_newer_than_commit(self, mock_gh_json):
        mock_gh_json.side_effect = [
            [{"number": 5, "title": "Fix", "updatedAt": "2026-01-01"}],
            {"last_review": "2026-03-20T10:00:00Z", "last_commit": "2026-03-19T10:00:00Z"},
        ]
        result = code_factory.check_review_requested("owner/repo")
        self.assertEqual(result, [{"number": 5, "title": "Fix", "updatedAt": "2026-01-01"}])

    @patch("code_factory.gh_json")
    def test_skips_pr_when_commit_newer_than_review(self, mock_gh_json):
        mock_gh_json.side_effect = [
            [{"number": 5, "title": "Fix", "updatedAt": "2026-01-01"}],
            {"last_review": "2026-03-19T10:00:00Z", "last_commit": "2026-03-20T10:00:00Z"},
        ]
        result = code_factory.check_review_requested("owner/repo")
        self.assertEqual(result, [])


class TestCheckPlanFeedback(unittest.TestCase):
    # `gh ... --json comments` returns {"comments": [...]}, not a bare list — match real shape.
    @patch("code_factory.gh_json")
    def test_returns_pr_when_human_comment_has_no_marker(self, mock_gh_json):
        mock_gh_json.side_effect = [
            [{"number": 5, "title": "Fix", "headRefName": "bot/42-fix"}],
            {"comments": [{"author": {"login": "reviewer"}, "createdAt": "2026-05-05T20:00:00Z", "body": "please revise"}]},
            {"comments": []},
        ]
        result = code_factory.check_plan_feedback("owner/repo")
        self.assertEqual(result, [{"number": 5, "title": "Fix", "headRefName": "bot/42-fix", "issue_number": 42}])

    @patch("code_factory.gh_json")
    def test_skips_pr_when_marker_is_newer_than_human_comment(self, mock_gh_json):
        mock_gh_json.side_effect = [
            [{"number": 5, "title": "Fix", "headRefName": "bot/42-fix"}],
            {"comments": [
                {"author": {"login": "reviewer"}, "createdAt": "2026-05-05T20:00:00Z", "body": "please revise"},
                {"author": {"login": "bot-user"}, "createdAt": "2026-05-05T20:05:00Z", "body": code_factory.PHASE2_MARKER},
            ]},
            {"comments": []},
        ]
        result = code_factory.check_plan_feedback("owner/repo")
        self.assertEqual(result, [])

    @patch("code_factory.gh_json")
    def test_returns_pr_when_issue_comment_is_newer_than_marker(self, mock_gh_json):
        mock_gh_json.side_effect = [
            [{"number": 5, "title": "Fix", "headRefName": "bot/42-fix"}],
            {"comments": [{"author": {"login": "bot-user"}, "createdAt": "2026-05-05T20:05:00Z", "body": code_factory.PHASE2_MARKER}]},
            {"comments": [{"author": {"login": "reviewer"}, "createdAt": "2026-05-05T20:10:00Z", "body": "one more change"}]},
        ]
        result = code_factory.check_plan_feedback("owner/repo")
        self.assertEqual(result, [{"number": 5, "title": "Fix", "headRefName": "bot/42-fix", "issue_number": 42}])

    @patch("code_factory.gh_json")
    def test_human_comments_detected_when_bot_shares_user_account(self, mock_gh_json):
        # Bot runs as the human user, so author.login is identical for both.
        # The marker — not the login — is what distinguishes bot from human.
        mock_gh_json.side_effect = [
            [{"number": 5, "title": "Fix", "headRefName": "bot/42-fix"}],
            {"comments": [
                {"author": {"login": "shared-user"}, "createdAt": "2026-05-05T20:00:00Z", "body": "lgtm"},
                {"author": {"login": "shared-user"}, "createdAt": "2026-05-05T20:10:00Z", "body": "actually, design question..."},
            ]},
            {"comments": []},
        ]
        result = code_factory.check_plan_feedback("owner/repo")
        self.assertEqual(result, [{"number": 5, "title": "Fix", "headRefName": "bot/42-fix", "issue_number": 42}])


class TestCheckUnclaimed(unittest.TestCase):
    @patch("code_factory.gh_json")
    def test_skips_assigned_issues(self, mock_gh_json):
        mock_gh_json.return_value = [
            {"number": 1, "title": "Bug", "labels": [], "assignees": [{"login": "bob"}]},
        ]
        result = code_factory.check_unclaimed_issues("owner/repo")
        self.assertEqual(result, [])

    @patch("code_factory.gh_json")
    def test_skips_issues_with_open_prs(self, mock_gh_json):
        mock_gh_json.side_effect = [
            [{"number": 1, "title": "Bug", "labels": [], "assignees": []}],
            [{"headRefName": "bot/1-bug"}],
        ]
        result = code_factory.check_unclaimed_issues("owner/repo")
        self.assertEqual(result, [])

    @patch("code_factory.gh_json")
    def test_returns_unassigned_issue_with_no_prs(self, mock_gh_json):
        mock_gh_json.side_effect = [
            [{"number": 1, "title": "Bug", "labels": [], "assignees": []}],
            [],
        ]
        result = code_factory.check_unclaimed_issues("owner/repo")
        self.assertEqual(result, [{"number": 1, "title": "Bug", "labels": [], "assignees": []}])


class TestRoute(unittest.TestCase):
    @patch("code_factory.check_unclaimed_issues", return_value=[])
    @patch("code_factory.check_accepted_plans", return_value=[])
    @patch("code_factory.check_plan_feedback", return_value=[])
    @patch("code_factory.check_review_requested", return_value=[])
    @patch("code_factory.get_in_progress_prs", return_value=set())
    def test_returns_none_when_no_work(self, *_):
        self.assertIsNone(code_factory.route("owner/repo"))

    @patch("code_factory.check_unclaimed_issues")
    @patch("code_factory.check_accepted_plans", return_value=[])
    @patch("code_factory.check_plan_feedback", return_value=[])
    @patch("code_factory.check_review_requested", return_value=[{"number": 5, "title": "Fix"}])
    @patch("code_factory.get_in_progress_prs", return_value=set())
    def test_priority1_takes_precedence(self, _, mock_review, *__):
        result = code_factory.route("owner/repo")
        self.assertEqual(result[0], "phase6_process_review")
        self.assertEqual(result[1]["pr"]["number"], 5)

    @patch("code_factory.check_unclaimed_issues", return_value=[{"number": 10, "title": "New"}])
    @patch("code_factory.check_accepted_plans", return_value=[])
    @patch("code_factory.check_plan_feedback", return_value=[])
    @patch("code_factory.check_review_requested", return_value=[])
    @patch("code_factory.get_in_progress_prs", return_value=set())
    def test_priority4_returns_issue(self, *_):
        result = code_factory.route("owner/repo")
        self.assertEqual(result[0], "phase1_claim_and_plan")
        self.assertEqual(result[1]["issue"]["number"], 10)

    @patch("code_factory.check_unclaimed_issues")
    @patch("code_factory.check_accepted_plans", return_value=[])
    @patch("code_factory.check_plan_feedback", return_value=[])
    @patch("code_factory.check_review_requested", return_value=[{"number": 5, "title": "Fix"}])
    @patch("code_factory.get_in_progress_prs", return_value={5})
    def test_in_progress_pr_excluded(self, *_):
        result = code_factory.route("owner/repo")
        self.assertIsNone(result)


class TestParseClaudeJson(unittest.TestCase):
    def test_parses_plain_json(self):
        result = code_factory.parse_claude_json('{"action": "approve"}')
        self.assertEqual(result, {"action": "approve"})

    def test_parses_json_in_code_block(self):
        result = code_factory.parse_claude_json('```json\n{"action": "approve"}\n```')
        self.assertEqual(result, {"action": "approve"})

    def test_parses_json_in_plain_code_block(self):
        result = code_factory.parse_claude_json('```\n{"action": "noop"}\n```')
        self.assertEqual(result, {"action": "noop"})

    def test_returns_none_for_invalid_json(self):
        self.assertIsNone(code_factory.parse_claude_json("not json"))

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(code_factory.parse_claude_json(""))


class TestPhase5(unittest.TestCase):
    @patch("code_factory.remove_in_progress")
    @patch("code_factory.swap_label")
    @patch("code_factory.gh")
    @patch("code_factory.gh_json")
    def test_marks_ready_when_files_changed(self, mock_json, mock_gh, mock_swap, mock_remove):
        mock_json.return_value = 3
        result = code_factory.phase5_post_implementation(
            repo="owner/repo", pr={"number": 5, "title": "Fix"}
        )
        self.assertIsNone(result)
        mock_gh.assert_any_call("pr", "ready", "5", "--repo", "owner/repo")
        mock_swap.assert_called_once_with("owner/repo", 5, "bot:plan-accepted", "bot:review-requested")
        mock_remove.assert_called_once_with("owner/repo", 5)

    @patch("code_factory.gh_json")
    def test_raises_when_no_files_changed(self, mock_json):
        mock_json.return_value = 0
        with self.assertRaises(RuntimeError) as ctx:
            code_factory.phase5_post_implementation(
                repo="owner/repo", pr={"number": 5, "title": "Fix"}
            )
        self.assertIn("0 changed files", str(ctx.exception))


class TestPhase2(unittest.TestCase):
    @patch("code_factory.llm_reason")
    @patch("code_factory.gh")
    @patch("code_factory.swap_label")
    @patch("code_factory.add_in_progress")
    def test_approve_chains_to_phase4(self, mock_add, mock_swap, mock_gh, mock_llm):
        mock_gh.return_value = "user1 (2026-03-20): LGTM"
        mock_llm.return_value = '{"action": "approve", "summary": "approved"}'
        result = code_factory.phase2_process_feedback(
            repo="owner/repo", pr={"number": 5, "title": "Fix"}
        )
        self.assertEqual(result[0], "phase4_implement")
        mock_swap.assert_called_once_with("owner/repo", 5, "bot:plan-proposed", "bot:plan-accepted")

    @patch("code_factory.llm_reason")
    @patch("code_factory.gh")
    @patch("code_factory.mark_phase2_processed")
    @patch("code_factory.remove_in_progress")
    @patch("code_factory.add_in_progress")
    def test_noop_returns_none(self, mock_add, mock_remove, mock_mark, mock_gh, mock_llm):
        mock_gh.return_value = ""
        mock_llm.return_value = '{"action": "noop", "summary": "no feedback"}'
        result = code_factory.phase2_process_feedback(
            repo="owner/repo", pr={"number": 5, "title": "Fix"}
        )
        self.assertIsNone(result)
        mock_mark.assert_called_once_with("owner/repo", 5, "noop", "no feedback")
        mock_remove.assert_called_once_with("owner/repo", 5)

    @patch("code_factory.llm_reason")
    @patch("code_factory.gh")
    @patch("code_factory.mark_phase2_processed")
    @patch("code_factory.remove_in_progress")
    @patch("code_factory.add_in_progress")
    def test_malformed_json_returns_none(self, mock_add, mock_remove, mock_mark, mock_gh, mock_llm):
        mock_gh.return_value = ""
        mock_llm.return_value = "not json at all"
        result = code_factory.phase2_process_feedback(
            repo="owner/repo", pr={"number": 5, "title": "Fix"}
        )
        self.assertIsNone(result)
        mock_mark.assert_not_called()
        mock_remove.assert_called_once_with("owner/repo", 5)

    @patch("code_factory.llm_reason")
    @patch("code_factory.gh")
    @patch("code_factory.mark_phase2_processed")
    @patch("code_factory.remove_in_progress")
    @patch("code_factory.add_in_progress")
    def test_revise_major_marks_processed(self, mock_add, mock_remove, mock_mark, mock_gh, mock_llm):
        mock_gh.return_value = ""
        mock_llm.return_value = (
            '{"action": "revise_major", "summary": "needs rethink", '
            '"revised_plan": "new plan", "comment": "rethinking"}'
        )
        result = code_factory.phase2_process_feedback(
            repo="owner/repo", pr={"number": 5, "title": "Fix"}
        )
        self.assertIsNone(result)
        mock_mark.assert_called_once_with("owner/repo", 5, "revise_major", "needs rethink")
        mock_remove.assert_called_once_with("owner/repo", 5)


class TestAgentSelection(unittest.TestCase):
    @patch("code_factory._run_agent_command")
    def test_llm_reason_uses_claude_by_default(self, mock_run):
        original = code_factory.AGENT_CLI
        code_factory.AGENT_CLI = "claude"
        mock_run.return_value = "ok"
        try:
            result = code_factory.llm_reason("prompt")
        finally:
            code_factory.AGENT_CLI = original
        self.assertEqual(result, "ok")
        mock_run.assert_called_once_with(["claude", "-p", "prompt", "--print"])

    @patch("code_factory._codex")
    def test_llm_reason_uses_codex_when_selected(self, mock_codex):
        original = code_factory.AGENT_CLI
        code_factory.AGENT_CLI = "codex"
        mock_codex.return_value = "ok"
        try:
            result = code_factory.llm_reason("prompt")
        finally:
            code_factory.AGENT_CLI = original
        self.assertEqual(result, "ok")
        mock_codex.assert_called_once_with("prompt")

    @patch("code_factory._run_agent_command")
    def test_llm_interactive_uses_claude(self, mock_run):
        original = code_factory.AGENT_CLI
        code_factory.AGENT_CLI = "claude"
        mock_run.return_value = "ok"
        try:
            result = code_factory.llm_interactive("prompt", "/tmp/repo")
        finally:
            code_factory.AGENT_CLI = original
        self.assertEqual(result, "ok")
        mock_run.assert_called_once_with(
            ["claude", "--dangerously-skip-permissions", "-p", "prompt", "--print"],
            cwd="/tmp/repo",
        )

    @patch("code_factory._codex")
    def test_llm_interactive_uses_codex(self, mock_codex):
        original = code_factory.AGENT_CLI
        code_factory.AGENT_CLI = "codex"
        mock_codex.return_value = "ok"
        try:
            result = code_factory.llm_interactive("prompt", "/tmp/repo")
        finally:
            code_factory.AGENT_CLI = original
        self.assertEqual(result, "ok")
        mock_codex.assert_called_once_with("prompt", workdir="/tmp/repo", interactive=True)


class TestMain(unittest.TestCase):
    @patch("code_factory.bootstrap_repo")
    @patch("code_factory.time.sleep")
    @patch("code_factory.route", return_value=None)
    @patch("code_factory.get_repo", return_value="owner/repo")
    def test_once_mode_exits_when_no_work(self, mock_repo, mock_route, mock_sleep, mock_bootstrap):
        with patch("sys.argv", ["code_factory.py", "--once"]):
            code_factory.main()
        mock_route.assert_called_once_with("owner/repo")
        mock_sleep.assert_not_called()

    @patch("code_factory.bootstrap_repo")
    @patch("code_factory.route", return_value=None)
    @patch("code_factory.get_repo", return_value="owner/repo")
    def test_agent_positional_argument_selects_codex(self, mock_repo, mock_route, mock_bootstrap):
        original = code_factory.AGENT_CLI
        try:
            with patch("sys.argv", ["code_factory.py", "codex", "--once"]):
                code_factory.main()
            self.assertEqual(code_factory.AGENT_CLI, "codex")
        finally:
            code_factory.AGENT_CLI = original

    @patch("code_factory.bootstrap_repo")
    @patch("code_factory.time.sleep")
    @patch("code_factory.remove_in_progress")
    @patch("code_factory.route")
    @patch("code_factory.get_repo", return_value="owner/repo")
    def test_once_mode_runs_phase_and_exits(self, mock_repo, mock_route, mock_remove, mock_sleep, mock_bootstrap):
        phase_fn = MagicMock(return_value=None)
        mock_route.return_value = ("test_phase", {"repo": "owner/repo", "pr": {"number": 1}})
        with patch("sys.argv", ["code_factory.py", "--once"]):
            with patch.dict(code_factory.PHASES, {"test_phase": phase_fn}):
                code_factory.main()
        phase_fn.assert_called_once()

    @patch("code_factory.bootstrap_repo")
    @patch("code_factory.time.sleep")
    @patch("code_factory.remove_in_progress")
    @patch("code_factory.route")
    @patch("code_factory.get_repo", return_value="owner/repo")
    def test_error_cleans_up_in_progress(self, mock_repo, mock_route, mock_remove, mock_sleep, mock_bootstrap):
        def failing_phase(**ctx):
            raise RuntimeError("boom")
        mock_route.return_value = ("test_phase", {"repo": "owner/repo", "pr": {"number": 7}})
        with patch("sys.argv", ["code_factory.py", "--once"]):
            with patch.dict(code_factory.PHASES, {"test_phase": failing_phase}):
                code_factory.main()
        mock_remove.assert_called_once_with("owner/repo", 7)
