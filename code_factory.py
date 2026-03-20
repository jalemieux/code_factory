#!/usr/bin/env python3
"""Code Factory — autonomous GitHub contribution orchestrator."""

from __future__ import annotations

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
    return re.sub(r"[^a-z0-9]+", "-", title.lower())[:40].strip("-")


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
