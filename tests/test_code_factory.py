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
