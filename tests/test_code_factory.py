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
            1,
        ]
        result = code_factory.check_unclaimed_issues("owner/repo")
        self.assertEqual(result, [])

    @patch("code_factory.gh_json")
    def test_returns_unassigned_issue_with_no_prs(self, mock_gh_json):
        mock_gh_json.side_effect = [
            [{"number": 1, "title": "Bug", "labels": [], "assignees": []}],
            0,
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
    @patch("code_factory.claude")
    @patch("code_factory.gh")
    @patch("code_factory.swap_label")
    @patch("code_factory.add_in_progress")
    def test_approve_chains_to_phase4(self, mock_add, mock_swap, mock_gh, mock_claude):
        mock_gh.return_value = "user1 (2026-03-20): LGTM"
        mock_claude.return_value = '{"action": "approve", "summary": "approved"}'
        result = code_factory.phase2_process_feedback(
            repo="owner/repo", pr={"number": 5, "title": "Fix"}
        )
        self.assertEqual(result[0], "phase4_implement")
        mock_swap.assert_called_once_with("owner/repo", 5, "bot:plan-proposed", "bot:plan-accepted")

    @patch("code_factory.claude")
    @patch("code_factory.gh")
    @patch("code_factory.remove_in_progress")
    @patch("code_factory.add_in_progress")
    def test_noop_returns_none(self, mock_add, mock_remove, mock_gh, mock_claude):
        mock_gh.return_value = ""
        mock_claude.return_value = '{"action": "noop", "summary": "no feedback"}'
        result = code_factory.phase2_process_feedback(
            repo="owner/repo", pr={"number": 5, "title": "Fix"}
        )
        self.assertIsNone(result)
        mock_remove.assert_called_once_with("owner/repo", 5)

    @patch("code_factory.claude")
    @patch("code_factory.gh")
    @patch("code_factory.remove_in_progress")
    @patch("code_factory.add_in_progress")
    def test_malformed_json_returns_none(self, mock_add, mock_remove, mock_gh, mock_claude):
        mock_gh.return_value = ""
        mock_claude.return_value = "not json at all"
        result = code_factory.phase2_process_feedback(
            repo="owner/repo", pr={"number": 5, "title": "Fix"}
        )
        self.assertIsNone(result)
        mock_remove.assert_called_once_with("owner/repo", 5)
