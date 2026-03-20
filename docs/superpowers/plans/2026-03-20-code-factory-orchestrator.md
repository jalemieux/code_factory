# Code Factory Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `run_loop.sh`, `check_actionable.py`, and SKILL.md with a single `code_factory.py` orchestrator that handles deterministic logic in Python and shells out to `claude` CLI for LLM-dependent phases.

**Architecture:** Single-file Python script (~500 lines) with prompt template files. Polls GitHub for actionable work, routes to phase functions, manages labels, and invokes `claude` CLI for reasoning tasks. No external dependencies — standard library only.

**Tech Stack:** Python 3 (standard library: `argparse`, `json`, `subprocess`, `re`, `time`, `pathlib`, `sys`, `datetime`), `gh` CLI, `claude` CLI, `git`

**Spec:** `docs/superpowers/specs/2026-03-20-code-factory-orchestrator-design.md`

---

### File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `code_factory.py` | All orchestration: CLI wrappers, router, phase functions, main loop |
| Create | `prompts/phase1_claim_and_plan.md` | Prompt template for plan generation |
| Create | `prompts/phase2_process_feedback.md` | Prompt template for feedback classification |
| Create | `prompts/phase4_implement.md` | Prompt template for implementation |
| Create | `prompts/phase6_process_review.md` | Prompt template for code review classification |
| Create | `prompts/phase6_apply_fixes.md` | Prompt template for applying review fix requests |
| Create | `tests/test_code_factory.py` | Unit tests for deterministic logic |
| Modify | `skills/git-contribute/SKILL.md` | Slim down to thin wrapper |
| Modify | `skills/git-contribute/TROUBLESHOOTING.md:155-158` | Fix label creation recipe to include `bot:in-progress` |
| Modify | `README.md` | Update usage instructions |
| Delete | `check-actionable-issues/check_actionable.py` | Absorbed into `code_factory.py` |
| Delete | `check-actionable-issues/run_loop.sh` | Absorbed into `code_factory.py` |

---

### Task 1: CLI Wrappers and Helpers

Core utility functions that all other tasks depend on: `gh()`, `gh_json()`, `git()`, `slugify()`, `ensure_labels()`, label helpers, `read_repo_conventions()`, `load_prompt()`, `claude()`, `claude_interactive()`.

**Files:**
- Create: `code_factory.py`
- Create: `tests/test_code_factory.py`

- [ ] **Step 1: Write tests for `gh()` retry logic**

```python
# tests/test_code_factory.py
import json
import subprocess
import unittest
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_code_factory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'code_factory'`

- [ ] **Step 3: Implement CLI wrappers and helpers**

```python
# code_factory.py
#!/usr/bin/env python3
"""Code Factory — autonomous GitHub contribution orchestrator."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} — {msg}", file=sys.stderr)


def gh(*args: str) -> str:
    """Run a gh CLI command and return stdout, retrying on rate limits."""
    for attempt in range(4):
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
        if "rate limit" in result.stderr.lower() and attempt < 3:
            wait = 2 ** attempt * 15
            log(f"Rate limited, retrying in {wait}s...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return ""


def gh_json(*args: str) -> list | dict:
    return json.loads(gh(*args) or "[]")


def git(*args: str) -> str:
    """Run a git command, raise on failure."""
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]


def ensure_labels(repo: str) -> None:
    for label in (
        "bot:plan-proposed",
        "bot:plan-accepted",
        "bot:in-progress",
        "bot:review-requested",
    ):
        gh(
            "label", "create", label,
            "--repo", repo,
            "--description", "Managed by git-contribute",
            "--color", "0E8A16",
            "--force",
        )


def add_in_progress(repo: str, num: int) -> None:
    gh("pr", "edit", str(num), "--repo", repo, "--add-label", "bot:in-progress")


def remove_in_progress(repo: str, num: int) -> None:
    gh("pr", "edit", str(num), "--repo", repo, "--remove-label", "bot:in-progress")


def swap_label(repo: str, num: int, old: str, new: str) -> None:
    gh("pr", "edit", str(num), "--repo", repo, "--remove-label", old, "--add-label", new)


def get_repo(repo: str | None = None) -> str:
    if repo:
        return repo
    return gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")


def read_repo_conventions(repo: str) -> str:
    conventions = []
    for fname in ("CONTRIBUTING.md", "CLAUDE.md", "AGENTS.md", "CODING_GUIDELINES.md"):
        try:
            content = git("show", f"HEAD:{fname}")
            conventions.append(f"## {fname}\n{content}")
        except RuntimeError:
            pass
    return "\n\n".join(conventions) or "(no convention files found)"


def load_prompt(phase: str, **kwargs: str) -> str:
    template_path = PROMPTS_DIR / f"{phase}.md"
    template = template_path.read_text()
    return template.format(**kwargs)


def claude(prompt: str) -> str:
    """Run claude CLI for reasoning tasks. Output-only, no tool access."""
    result = subprocess.run(
        ["claude", "-p", prompt, "--print"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr.strip()}")
    return result.stdout.strip()


def claude_interactive(prompt: str, workdir: str) -> str:
    """Run claude with full tool access for implementation work."""
    result = subprocess.run(
        ["claude", "--dangerously-skip-permissions", "-p", prompt, "--print"],
        capture_output=True, text=True,
        cwd=workdir,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr.strip()}")
    return result.stdout.strip()
```

- [ ] **Step 4: Write tests for helpers**

Add to `tests/test_code_factory.py`:

```python
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
    def test_loads_and_interpolates(self, tmp_path=None):
        import tempfile
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
        import tempfile
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_code_factory.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add code_factory.py tests/test_code_factory.py
git commit -m "feat: add CLI wrappers and helper functions for code_factory"
```

---

### Task 2: Router

Priority-based routing logic, ported from `check_actionable.py`. All four priority checks plus the `bot:in-progress` exclusion.

**Files:**
- Modify: `code_factory.py`
- Modify: `tests/test_code_factory.py`

- [ ] **Step 1: Write tests for router priority checks**

Add to `tests/test_code_factory.py`:

```python
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
            1,  # linked PR count
        ]
        result = code_factory.check_unclaimed_issues("owner/repo")
        self.assertEqual(result, [])

    @patch("code_factory.gh_json")
    def test_returns_unassigned_issue_with_no_prs(self, mock_gh_json):
        mock_gh_json.side_effect = [
            [{"number": 1, "title": "Bug", "labels": [], "assignees": []}],
            0,  # no linked PRs
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_code_factory.py::TestRoute -v`
Expected: FAIL — functions not defined

- [ ] **Step 3: Implement router functions**

Add to `code_factory.py`:

```python
def get_in_progress_prs(repo: str) -> set[int]:
    prs = gh_json(
        "pr", "list", "--repo", repo,
        "--author", "@me",
        "--label", "bot:in-progress",
        "--json", "number",
    )
    return {pr["number"] for pr in prs}


def check_review_requested(repo: str) -> list[dict]:
    """Priority 1: Own PRs with code review feedback."""
    prs = gh_json(
        "pr", "list", "--repo", repo,
        "--author", "@me",
        "--label", "bot:review-requested",
        "--json", "number,title,updatedAt",
    )
    actionable = []
    for pr in prs:
        info = gh_json(
            "pr", "view", str(pr["number"]), "--repo", repo,
            "--json", "reviews,commits",
            "--jq",
            "{last_review: .reviews[-1].submittedAt, last_commit: .commits[-1].committedDate}",
        )
        last_review = info.get("last_review")
        last_commit = info.get("last_commit")
        if last_review and last_commit and last_review > last_commit:
            actionable.append(pr)
    return actionable


def check_plan_feedback(repo: str) -> list[dict]:
    """Priority 2: Own draft PRs with plan feedback."""
    prs = gh_json(
        "pr", "list", "--repo", repo,
        "--author", "@me",
        "--draft",
        "--label", "bot:plan-proposed",
        "--json", "number,title",
    )
    actionable = []
    for pr in prs:
        comment_count = gh_json(
            "pr", "view", str(pr["number"]), "--repo", repo,
            "--json", "comments",
            "--jq", ".comments | length",
        )
        if comment_count and comment_count > 0:
            actionable.append(pr)
    return actionable


def check_accepted_plans(repo: str) -> list[dict]:
    """Priority 3: Accepted plans ready for implementation."""
    return gh_json(
        "pr", "list", "--repo", repo,
        "--author", "@me",
        "--label", "bot:plan-accepted",
        "--json", "number,title",
    )


def check_unclaimed_issues(repo: str) -> list[dict]:
    """Priority 4: Unassigned open issues with no linked open PRs."""
    issues = gh_json(
        "issue", "list", "--repo", repo,
        "--state", "open",
        "--json", "number,title,labels,assignees",
        "--limit", "20",
    )
    actionable = []
    for issue in issues:
        if issue.get("assignees"):
            continue
        linked_prs = gh_json(
            "pr", "list", "--repo", repo,
            "--search", f"#{issue['number']}",
            "--state", "open",
            "--json", "number",
            "--jq", "length",
        )
        count = linked_prs if isinstance(linked_prs, int) else len(linked_prs)
        if count == 0:
            actionable.append(issue)
    # Prefer issues with good-first-issue or help-wanted labels
    preferred = {"good first issue", "good-first-issue", "help wanted", "help-wanted"}
    def sort_key(issue: dict) -> int:
        labels = {l["name"].lower() for l in issue.get("labels", [])}
        return 0 if labels & preferred else 1
    actionable.sort(key=sort_key)
    return actionable


def route(repo: str) -> tuple[str, dict] | None:
    """Find highest-priority actionable work. Returns (phase_name, context) or None."""
    in_progress = get_in_progress_prs(repo)

    for pr in check_review_requested(repo):
        if pr["number"] not in in_progress:
            return ("phase6_process_review", {"repo": repo, "pr": pr})

    for pr in check_plan_feedback(repo):
        if pr["number"] not in in_progress:
            return ("phase2_process_feedback", {"repo": repo, "pr": pr})

    for pr in check_accepted_plans(repo):
        if pr["number"] not in in_progress:
            return ("phase4_implement", {"repo": repo, "pr": pr})

    for issue in check_unclaimed_issues(repo):
        return ("phase1_claim_and_plan", {"repo": repo, "issue": issue})

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_code_factory.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add code_factory.py tests/test_code_factory.py
git commit -m "feat: add priority-based router for actionable work"
```

---

### Task 3: Phase Functions

All five phase functions: `phase1_claim_and_plan`, `phase2_process_feedback`, `phase4_implement`, `phase5_post_implementation`, `phase6_process_review`.

**Files:**
- Modify: `code_factory.py`
- Modify: `tests/test_code_factory.py`

- [ ] **Step 1: Write tests for Phase 5 (fully deterministic, easiest to test)**

```python
class TestPhase5(unittest.TestCase):
    @patch("code_factory.remove_in_progress")
    @patch("code_factory.swap_label")
    @patch("code_factory.gh")
    @patch("code_factory.gh_json")
    def test_marks_ready_when_files_changed(self, mock_json, mock_gh, mock_swap, mock_remove):
        mock_json.return_value = 3  # changedFiles
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
```

- [ ] **Step 2: Write tests for `parse_claude_json`**

```python
class TestParseclaudeJson(unittest.TestCase):
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
```

- [ ] **Step 3: Write tests for Phase 2 routing logic** (renumbered from Step 2)

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_code_factory.py::TestPhase5 tests/test_code_factory.py::TestPhase2 -v`
Expected: FAIL — phase functions not defined

- [ ] **Step 4: Implement all phase functions**

Add to `code_factory.py`:

```python
def parse_claude_json(output: str) -> dict | None:
    """Extract and parse JSON from claude output. Returns None on failure."""
    # Claude may wrap JSON in markdown code blocks
    cleaned = re.sub(r"^```(?:json)?\n?", "", output.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None


def get_pr_branch(repo: str, num: int) -> str:
    return gh("pr", "view", str(num), "--repo", repo, "--json", "headRefName", "-q", ".headRefName")


# --- Phase Functions ---
# Each returns (next_phase, context) to chain, or None to exit.


def phase1_claim_and_plan(repo: str, issue: dict) -> tuple[str, dict] | None:
    """Claim an issue and propose an implementation plan via draft PR."""
    num = issue["number"]
    title = issue["title"]
    log(f"Phase 1: claiming issue #{num} — {title}")

    gh("issue", "edit", str(num), "--repo", repo, "--add-assignee", "@me")
    ensure_labels(repo)

    issue_body = gh("issue", "view", str(num), "--repo", repo, "--json", "body", "-q", ".body")
    conventions = read_repo_conventions(repo)
    recent_prs = gh(
        "pr", "list", "--repo", repo, "--state", "merged",
        "--limit", "5", "--json", "title,body",
    )

    slug = slugify(title)
    branch = f"bot/{num}-{slug}"
    default_branch = gh("repo", "view", "--repo", repo, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name")
    git("checkout", default_branch)
    git("pull", "--ff-only")
    git("checkout", "-b", branch)
    git("commit", "--allow-empty", "-m", f"plan: {title} (#{num})")
    git("push", "-u", "origin", branch)

    prompt = load_prompt(
        "phase1_claim_and_plan",
        issue_number=str(num),
        issue_title=title,
        issue_body=issue_body or "(no description)",
        conventions=conventions,
        recent_prs=recent_prs,
    )
    plan = claude(prompt)

    gh(
        "pr", "create", "--draft", "--repo", repo,
        "--title", title,
        "--body", plan,
        "--label", "bot:plan-proposed",
    )
    log(f"Phase 1 complete: draft PR created for issue #{num}")
    return None


def phase2_process_feedback(repo: str, pr: dict) -> tuple[str, dict] | None:
    """Classify feedback on a plan PR and route accordingly."""
    num = pr["number"]
    log(f"Phase 2: processing feedback on PR #{num}")
    add_in_progress(repo, num)

    comments = gh(
        "pr", "view", str(num), "--repo", repo,
        "--json", "comments",
        "--jq", '.comments[] | "\(.author.login) (\(.createdAt)): \(.body)"',
    )
    plan_body = gh("pr", "view", str(num), "--repo", repo, "--json", "body", "-q", ".body")

    prompt = load_prompt(
        "phase2_process_feedback",
        pr_number=str(num),
        pr_title=pr["title"],
        plan_body=plan_body,
        comments=comments,
    )
    result = claude(prompt)
    parsed = parse_claude_json(result)

    if not parsed or "action" not in parsed:
        log(f"Phase 2: malformed response from claude, will retry next iteration")
        remove_in_progress(repo, num)
        return None

    action = parsed["action"]
    log(f"Phase 2: feedback classified as '{action}'")

    if action == "approve":
        swap_label(repo, num, "bot:plan-proposed", "bot:plan-accepted")
        return ("phase4_implement", {"repo": repo, "pr": pr})

    if action == "revise_minor":
        if parsed.get("revised_plan"):
            gh("pr", "edit", str(num), "--repo", repo, "--body", parsed["revised_plan"])
        swap_label(repo, num, "bot:plan-proposed", "bot:plan-accepted")
        return ("phase4_implement", {"repo": repo, "pr": pr})

    if action == "revise_major":
        if parsed.get("revised_plan"):
            gh("pr", "edit", str(num), "--repo", repo, "--body", parsed["revised_plan"])
        if parsed.get("comment"):
            gh("pr", "comment", str(num), "--repo", repo, "--body", parsed["comment"])
        remove_in_progress(repo, num)
        return None

    if action == "clarify":
        if parsed.get("comment"):
            gh("pr", "comment", str(num), "--repo", repo, "--body", parsed["comment"])
        remove_in_progress(repo, num)
        return None

    # noop or unknown
    remove_in_progress(repo, num)
    return None


def phase4_implement(repo: str, pr: dict) -> tuple[str, dict] | None:
    """Implement the accepted plan."""
    num = pr["number"]
    log(f"Phase 4: implementing PR #{num}")
    add_in_progress(repo, num)

    plan = gh("pr", "view", str(num), "--repo", repo, "--json", "body", "-q", ".body")
    branch = get_pr_branch(repo, num)

    # Ensure we're on the right branch
    git("fetch", "origin", branch)
    git("checkout", branch)
    git("pull", "--ff-only", "origin", branch)

    prompt = load_prompt(
        "phase4_implement",
        pr_number=str(num),
        plan=plan,
        repo=repo,
        branch=branch,
    )
    workdir = git("rev-parse", "--show-toplevel")
    claude_interactive(prompt, workdir)

    log(f"Phase 4 complete: implementation done for PR #{num}")
    return ("phase5_post_implementation", {"repo": repo, "pr": pr})


def phase5_post_implementation(repo: str, pr: dict) -> tuple[str, dict] | None:
    """Verify push landed and mark PR ready for review."""
    num = pr["number"]
    log(f"Phase 5: post-implementation for PR #{num}")

    changed_files = gh_json(
        "pr", "view", str(num), "--repo", repo,
        "--json", "changedFiles", "-q", ".changedFiles",
    )
    count = changed_files if isinstance(changed_files, int) else 0
    if count == 0:
        raise RuntimeError(f"PR #{num} has 0 changed files — push did not land")

    gh("pr", "ready", str(num), "--repo", repo)
    swap_label(repo, num, "bot:plan-accepted", "bot:review-requested")
    remove_in_progress(repo, num)
    log(f"Phase 5 complete: PR #{num} marked ready for review")
    return None


def phase6_process_review(repo: str, pr: dict) -> tuple[str, dict] | None:
    """Process code review feedback."""
    num = pr["number"]
    log(f"Phase 6: processing review on PR #{num}")
    add_in_progress(repo, num)

    reviews = gh(
        "pr", "view", str(num), "--repo", repo,
        "--json", "reviews,reviewThreads",
    )

    prompt = load_prompt(
        "phase6_process_review",
        pr_number=str(num),
        pr_title=pr["title"],
        reviews=reviews,
        review_threads=reviews,  # same json blob contains both
    )
    result = claude(prompt)
    parsed = parse_claude_json(result)

    if not parsed or "action" not in parsed:
        log(f"Phase 6: malformed response from claude, will retry next iteration")
        remove_in_progress(repo, num)
        return None

    action = parsed["action"]
    log(f"Phase 6: review classified as '{action}'")

    if action == "approved":
        gh("pr", "merge", str(num), "--repo", repo, "--squash", "--delete-branch")
        log(f"Phase 6 complete: PR #{num} merged")
        return None

    if action == "changes_requested":
        branch = get_pr_branch(repo, num)
        git("fetch", "origin", branch)
        git("checkout", branch)
        git("pull", "--ff-only", "origin", branch)

        fix_prompt = load_prompt(
            "phase6_apply_fixes",
            pr_number=str(num),
            pr_title=pr["title"],
            reviews=reviews,
            branch=branch,
        )
        workdir = git("rev-parse", "--show-toplevel")
        claude_interactive(fix_prompt, workdir)

        # Re-request review from reviewers
        review_data = json.loads(reviews)
        reviewers = {r["author"]["login"] for r in review_data.get("reviews", []) if r.get("author")}
        for reviewer in reviewers:
            try:
                gh("pr", "edit", str(num), "--repo", repo, "--add-reviewer", reviewer)
            except RuntimeError:
                pass  # reviewer may not be valid
        remove_in_progress(repo, num)
        return None

    if action == "design_objection":
        swap_label(repo, num, "bot:review-requested", "bot:plan-proposed")
        remove_in_progress(repo, num)
        return None

    remove_in_progress(repo, num)
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_code_factory.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add code_factory.py tests/test_code_factory.py
git commit -m "feat: add phase functions for full lifecycle orchestration"
```

---

### Task 4: Main Loop and CLI

The `main()` function with argument parsing, repo bootstrap, polling loop, phase dispatch, and error handling.

**Files:**
- Modify: `code_factory.py`
- Modify: `tests/test_code_factory.py`

- [ ] **Step 1: Write tests for main loop behavior**

```python
class TestMain(unittest.TestCase):
    @patch("code_factory.time.sleep")
    @patch("code_factory.route", return_value=None)
    @patch("code_factory.get_repo", return_value="owner/repo")
    def test_once_mode_exits_when_no_work(self, mock_repo, mock_route, mock_sleep):
        with patch("sys.argv", ["code_factory.py", "--once"]):
            code_factory.main()
        mock_route.assert_called_once_with("owner/repo")
        mock_sleep.assert_not_called()

    @patch("code_factory.time.sleep")
    @patch("code_factory.remove_in_progress")
    @patch("code_factory.route")
    @patch("code_factory.get_repo", return_value="owner/repo")
    def test_once_mode_runs_phase_and_exits(self, mock_repo, mock_route, mock_remove, mock_sleep):
        phase_fn = MagicMock(return_value=None)
        mock_route.return_value = ("test_phase", {"repo": "owner/repo", "pr": {"number": 1}})
        with patch("sys.argv", ["code_factory.py", "--once"]):
            with patch.dict(code_factory.PHASES, {"test_phase": phase_fn}):
                code_factory.main()
        phase_fn.assert_called_once()

    @patch("code_factory.time.sleep")
    @patch("code_factory.remove_in_progress")
    @patch("code_factory.route")
    @patch("code_factory.get_repo", return_value="owner/repo")
    def test_error_cleans_up_in_progress(self, mock_repo, mock_route, mock_remove, mock_sleep):
        def failing_phase(**ctx):
            raise RuntimeError("boom")
        mock_route.return_value = ("test_phase", {"repo": "owner/repo", "pr": {"number": 7}})
        with patch("sys.argv", ["code_factory.py", "--once"]):
            with patch.dict(code_factory.PHASES, {"test_phase": failing_phase}):
                code_factory.main()
        mock_remove.assert_called_once_with("owner/repo", 7)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_code_factory.py::TestMain -v`
Expected: FAIL — `main` and `PHASES` not defined

- [ ] **Step 3: Implement main loop**

Add to `code_factory.py`:

```python
PHASES: dict[str, callable] = {
    "phase1_claim_and_plan": phase1_claim_and_plan,
    "phase2_process_feedback": phase2_process_feedback,
    "phase4_implement": phase4_implement,
    "phase5_post_implementation": phase5_post_implementation,
    "phase6_process_review": phase6_process_review,
}


def bootstrap_repo(repo: str) -> None:
    """Ensure the repo is cloned and default branch is synced."""
    try:
        current_repo = gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        if current_repo == repo:
            default_branch = gh(
                "repo", "view", "--repo", repo,
                "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name",
            )
            git("checkout", default_branch)
            git("pull", "--ff-only")
            return
    except RuntimeError:
        pass
    # Not in the repo — clone it
    gh("repo", "clone", repo)
    repo_name = repo.split("/")[-1]
    os.chdir(repo_name)
    log(f"Cloned and entered {repo}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Code Factory — autonomous GitHub contributions")
    parser.add_argument("--repo", help="owner/repo (default: current repo)")
    parser.add_argument("--once", action="store_true", help="single pass, then exit")
    args = parser.parse_args()

    repo = get_repo(args.repo)
    log(f"Code Factory targeting: {repo}")
    bootstrap_repo(repo)

    while True:
        log("Checking for actionable work...")
        result = route(repo)

        if result:
            phase_name, ctx = result
            log(f"Work found — starting {phase_name}")
            try:
                next_result = result
                while next_result:
                    phase_name, ctx = next_result
                    phase_fn = PHASES[phase_name]
                    next_result = phase_fn(**ctx)
            except Exception as e:
                log(f"Error in {phase_name}: {e}")
                pr = ctx.get("pr")
                if pr:
                    try:
                        remove_in_progress(repo, pr["number"])
                    except Exception:
                        pass
        else:
            log("No actionable work found.")

        if args.once:
            break

        sleep_time = 5 if result else 300
        log(f"Sleeping {sleep_time} seconds...")
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_code_factory.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add code_factory.py tests/test_code_factory.py
git commit -m "feat: add main loop with polling, dispatch, and error recovery"
```

---

### Task 5: Prompt Templates

The four prompt template files that Claude receives for LLM-dependent phases.

**Files:**
- Create: `prompts/phase1_claim_and_plan.md`
- Create: `prompts/phase2_process_feedback.md`
- Create: `prompts/phase4_implement.md`
- Create: `prompts/phase6_process_review.md`
- Create: `prompts/phase6_apply_fixes.md`

- [ ] **Step 0: Create prompts directory**

```bash
mkdir -p prompts
```

- [ ] **Step 1: Create `prompts/phase1_claim_and_plan.md`**

```markdown
You are proposing an implementation plan for a GitHub issue.

## Issue #{issue_number}: {issue_title}

{issue_body}

## Repository Conventions

{conventions}

## Recent Merged PRs (for style reference)

{recent_prs}

## Your Task

Produce a structured implementation plan in markdown. The plan will become the body of a draft PR for human review.

Your plan MUST include the line `Closes #{issue_number}` to link the PR to the issue.

Use this structure:

## Problem
What the issue asks for, in your own words.

## Approach
High-level strategy — what changes, why this approach.

## Planned Changes
- `file/path`: what changes and why
- ...

## Test Strategy
How the implementation will be verified.

## Risks / Open Questions
- Anything uncertain or worth flagging.

Keep the plan concise — bullet points, not essays. Reviewers skim.
```

- [ ] **Step 2: Create `prompts/phase2_process_feedback.md`**

```markdown
You are classifying human feedback on a proposed implementation plan.

## PR #{pr_number}: {pr_title}

## Current Plan

{plan_body}

## Comments from reviewers

{comments}

## Your Task

Classify the feedback and respond with a JSON object. Output ONLY the JSON — no other text.

Actions:
- "approve": Explicit approval (LGTM, looks good, approved, ship it) with no requested changes
- "revise_minor": Approval with small requested changes (e.g., "looks good, but change X"). Include the full revised plan in "revised_plan"
- "revise_major": Rejection or major rethink needed. Include the full revised plan in "revised_plan" and an explanatory comment in "comment"
- "clarify": Questions that need answering before proceeding. Include your answer in "comment"
- "noop": No actionable feedback (reactions only, unrelated chatter, or no comments)

Response format:
```json
{{
  "action": "approve | revise_minor | revise_major | clarify | noop",
  "summary": "brief explanation of your classification",
  "revised_plan": "full updated plan markdown (only for revise_minor/revise_major, omit otherwise)",
  "comment": "reply to post on the PR (only for clarify/revise_major, omit otherwise)"
}}
```

Important:
- Treat silence as NOT approval. Only explicit approval words trigger "approve".
- When revising, include the FULL updated plan, not just the diff.
- If feedback is ambiguous, prefer "clarify" over guessing intent.
```

- [ ] **Step 3: Create `prompts/phase4_implement.md`**

```markdown
You are implementing a plan that has been reviewed and approved by a human.

## PR #{pr_number} in {repo}

## Branch: {branch}

## Approved Plan

{plan}

## Your Task

Implement the plan above. You have full access to the codebase and all tools.

Follow this process:

1. **Read conventions first** — Check for CONTRIBUTING.md, CLAUDE.md, AGENTS.md, or CODING_GUIDELINES.md in the repo root. Follow their instructions.

2. **Write tests first (TDD)** — For each component in the plan:
   - Write the failing test
   - Run it to confirm it fails
   - Write the minimal implementation to make it pass
   - Run it to confirm it passes

3. **Debug failures** — If any test fails unexpectedly:
   - Read the error message carefully
   - Check your assumptions
   - Fix the root cause, don't patch symptoms

4. **Verify before declaring done** — Run the full test suite. Check that all tests pass. If the repo has a linter or formatter, run those too.

5. **Commit and push** — Make focused commits with clear messages. Push all commits to the branch:
   ```
   git push origin {branch}
   ```

Important:
- Follow the repo's existing patterns and test framework
- Do not introduce new dependencies unless the plan explicitly calls for them
- Do not force-push — it destroys review context
- Every commit should leave the repo in a working state
```

- [ ] **Step 4: Create `prompts/phase6_process_review.md`**

```markdown
You are classifying code review feedback on a pull request.

## PR #{pr_number}: {pr_title}

## Reviews and Threads

{reviews}

## Your Task

Classify the review feedback and respond with a JSON object. Output ONLY the JSON — no other text.

Actions:
- "approved": The PR has been formally approved via GitHub's review system (not just a comment saying "looks good")
- "changes_requested": Specific fixes or changes were requested. The review may include inline comments on specific lines
- "design_objection": Fundamental disagreement with the approach — the plan itself needs rethinking, not just the implementation

Response format:
```json
{{
  "action": "approved | changes_requested | design_objection",
  "summary": "brief explanation of the review state"
}}
```

Important:
- Only "approved" if there is a formal GitHub review approval (state: APPROVED), not just a comment
- If multiple reviewers have reviewed, go with the most recent review state
- If there are unresolved review threads, prefer "changes_requested" even if the overall review is approved
```

- [ ] **Step 5: Create `prompts/phase6_apply_fixes.md`**

```markdown
You are applying code review fixes to a pull request.

## PR #{pr_number}: {pr_title}

## Branch: {branch}

## Review Feedback

{reviews}

## Your Task

Apply the requested changes from the code review above. You have full access to the codebase and all tools.

For each review comment or thread:
1. Read the comment and understand what change is requested
2. Apply the fix
3. If you disagree with a suggestion, leave a reply explaining your reasoning instead of changing the code

After applying all fixes:
1. Run the test suite to ensure nothing is broken
2. Commit with a clear message referencing the review (e.g., "fix: address review feedback on PR #{pr_number}")
3. Push to the branch:
   ```
   git push origin {branch}
   ```

Important:
- Do not force-push — it destroys review context
- Keep changes focused on what was requested — do not refactor or "improve" unrelated code
- Every commit should leave the repo in a working state
```

- [ ] **Step 6: Verify prompt templates load correctly**

Run: `python3 -c "from code_factory import load_prompt, PROMPTS_DIR; print(load_prompt('phase1_claim_and_plan', issue_number='1', issue_title='Test', issue_body='body', conventions='none', recent_prs='none')[:50])"`
Expected: Prints first 50 chars of interpolated template without error

- [ ] **Step 7: Commit**

```bash
git add prompts/
git commit -m "feat: add prompt templates for all LLM-dependent phases"
```

---

### Task 6: Migration — Update Skill, Docs, Delete Old Files

Slim down SKILL.md, update README, fix TROUBLESHOOTING.md, delete old scripts.

**Files:**
- Modify: `skills/git-contribute/SKILL.md`
- Modify: `skills/git-contribute/TROUBLESHOOTING.md:155-158`
- Modify: `README.md`
- Delete: `check-actionable-issues/check_actionable.py`
- Delete: `check-actionable-issues/run_loop.sh`

- [ ] **Step 1: Slim down SKILL.md**

Replace the full content of `skills/git-contribute/SKILL.md` with:

```markdown
---
name: git-contribute
description: "Autonomous bug fix and feature implementation lifecycle for GitHub codebases — picks up open issues, proposes an implementation plan via draft PR for human review, incorporates feedback, implements the fix or feature using TDD, and shepherds the PR through code review to merge."
---

# Git Contribute

Run the Code Factory orchestrator for a single pass:

\`\`\`bash
python3 code_factory.py --once
\`\`\`

To target a specific repo:

\`\`\`bash
python3 code_factory.py --once --repo {repo}
\`\`\`

For continuous polling:

\`\`\`bash
python3 code_factory.py --repo {repo}
\`\`\`

See `TROUBLESHOOTING.md` for diagnostics.
```

- [ ] **Step 2: Fix TROUBLESHOOTING.md label recipe**

In `skills/git-contribute/TROUBLESHOOTING.md`, change the label creation block (lines 155-158) from:

```bash
for label in "bot:plan-proposed" "bot:plan-accepted" "bot:review-requested"; do
```

to:

```bash
for label in "bot:plan-proposed" "bot:plan-accepted" "bot:in-progress" "bot:review-requested"; do
```

- [ ] **Step 3: Update README.md**

Replace `README.md` with:

````markdown
<p align="center">
  <img src="banner.png" alt="Code Factory" width="100%">
</p>

# Code Factory

Automation that turns GitHub issues into merged PRs with human oversight, powered by [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

## Quick Start

```bash
# Continuous polling (runs until stopped)
python3 code_factory.py --repo owner/repo

# Single pass (process one item and exit)
python3 code_factory.py --once --repo owner/repo

# Auto-detect repo from current directory
cd /path/to/your-repo
python3 /path/to/code_factory/code_factory.py --once
```

## How It Works

A single Python script checks a GitHub repo for actionable work, then orchestrates Claude Code to handle it. Every change goes through a **plan-first workflow** — Claude proposes a plan as a draft PR, waits for human review, and only implements after approval.

### Lifecycle

1. **Claim** — picks an unassigned issue and self-assigns
2. **Plan** — creates a draft PR with an implementation plan
3. **Review** — waits for human feedback on the plan
4. **Implement** — writes the code using TDD after plan approval
5. **Verify** — runs tests and CI checks
6. **Merge** — after human code review approval

### Priority Order

Existing work is always finished before new work is started:

| Priority | What | Action |
|----------|------|--------|
| 1 | PRs with code review feedback | Address reviewer comments |
| 2 | PRs with plan feedback | Incorporate feedback or proceed |
| 3 | Accepted plans | Implement the approved plan |
| 4 | Unclaimed issues | Claim and propose a plan |

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed
- [`gh` CLI](https://cli.github.com/) authenticated with repo permissions
- Python 3

## Usage

```bash
# Continuous loop — polls every 5 minutes, dispatches work automatically
python3 code_factory.py --repo owner/repo

# Single pass — find one item, process it, exit
python3 code_factory.py --once --repo owner/repo

# Auto-detect repo from current directory
python3 code_factory.py --once
```

## Project Structure

```
code_factory.py           # Orchestrator: poll, route, manage phases, invoke claude
prompts/                  # Prompt templates for LLM-dependent phases
  phase1_claim_and_plan.md
  phase2_process_feedback.md
  phase4_implement.md
  phase6_process_review.md
  phase6_apply_fixes.md
skills/
  git-contribute/
    SKILL.md              # Thin wrapper for Claude Code skill invocation
    TROUBLESHOOTING.md    # Diagnostics and manual fix recipes
tests/
  test_code_factory.py    # Unit tests
```

## Labels

The workflow uses these GitHub labels (created automatically on first run):

- `bot:plan-proposed` — draft PR with a plan awaiting human review
- `bot:plan-accepted` — plan approved, ready for implementation
- `bot:in-progress` — PR currently being processed
- `bot:review-requested` — implementation complete, awaiting code review
```
````

- [ ] **Step 4: Delete old files**

```bash
git rm check-actionable-issues/check_actionable.py
git rm check-actionable-issues/run_loop.sh
rmdir check-actionable-issues
```

- [ ] **Step 5: Run all tests one final time**

Run: `python3 -m pytest tests/test_code_factory.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add skills/git-contribute/SKILL.md skills/git-contribute/TROUBLESHOOTING.md README.md
git commit -m "refactor: migrate to code_factory.py orchestrator

Replace run_loop.sh, check_actionable.py, and SKILL.md behavioral
logic with a single Python orchestrator. Slim SKILL.md to a thin
wrapper. Fix TROUBLESHOOTING.md label recipe."
```

---

### Task Summary

| Task | What | Depends On |
|------|------|------------|
| 1 | CLI wrappers and helpers | — |
| 2 | Router | Task 1 |
| 3 | Phase functions | Tasks 1, 2 |
| 4 | Main loop and CLI | Tasks 1, 2, 3 |
| 5 | Prompt templates | Task 1 (for `load_prompt`) |
| 6 | Migration (skill, docs, cleanup) | Tasks 1-5 |

Tasks 1-4 are sequential (each builds on the previous). Task 5 can run in parallel with Tasks 2-4.
