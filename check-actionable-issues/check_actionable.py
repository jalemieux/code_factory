#!/usr/bin/env python3
"""Check if a GitHub repo has issues/PRs actionable by the git-contribute workflow."""

from __future__ import annotations

import json
import subprocess
import sys
import time


def gh(*args: str) -> str:
    """Run a gh CLI command and return stdout, retrying on rate limits."""
    for attempt in range(4):
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
        if "rate limit" in result.stderr.lower() and attempt < 3:
            wait = 2 ** attempt * 15  # 15s, 30s, 60s
            print(f"  Rate limited, retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return ""


def gh_json(*args: str) -> list | dict:
    return json.loads(gh(*args) or "[]")


def get_repo(repo: str | None = None) -> str:
    if repo:
        return repo
    return gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")


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
            "--jq", '{last_review: .reviews[-1].submittedAt, last_commit: .commits[-1].committedDate}',
        )
        last_review = info.get("last_review")
        last_commit = info.get("last_commit")
        if last_review and last_commit and last_review > last_commit:
            actionable.append(pr)
    return actionable


def check_plan_feedback(repo: str) -> list[dict]:
    """Priority 2: Own draft PRs with plan feedback awaiting processing.

    Matches git-contribute's routing: only draft PRs with bot:plan-proposed
    label that have any comments. Since the bot creates the PR description
    (not comments), any comment on a plan PR is human feedback — even if
    the comment author matches the PR author (common when the bot and human
    share the same GitHub account).
    """
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
    """Priority 4: Unassigned open issues with no linked PRs."""
    issues = gh_json(
        "issue", "list",
        "--repo", repo,
        "--assignee", "",
        "--state", "open",
        "--json", "number,title,labels",
        "--limit", "20",
    )
    actionable = []
    for issue in issues:
        linked_prs = gh_json(
            "pr", "list", "--repo", repo,
            "--search", f"#{issue['number']}",
            "--state", "all",
            "--json", "number",
            "--jq", "length",
        )
        # gh --jq length returns an integer, not a list
        count = linked_prs if isinstance(linked_prs, int) else len(linked_prs)
        if count == 0:
            actionable.append(issue)
    return actionable


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        repo = get_repo(repo)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Checking repo: {repo}\n")
    found_any = False

    # Priority 1
    prs = check_review_requested(repo)
    if prs:
        found_any = True
        print(f"[Priority 1] PRs with review feedback ({len(prs)}):")
        for pr in prs:
            print(f"  #{pr['number']} — {pr['title']}")
        print()

    # Priority 2
    prs = check_plan_feedback(repo)
    if prs:
        found_any = True
        print(f"[Priority 2] PRs with plan feedback ({len(prs)}):")
        for pr in prs:
            print(f"  #{pr['number']} — {pr['title']}")
        print()

    # Priority 3
    prs = check_accepted_plans(repo)
    if prs:
        found_any = True
        print(f"[Priority 3] Accepted plans ready for implementation ({len(prs)}):")
        for pr in prs:
            print(f"  #{pr['number']} — {pr['title']}")
        print()

    # Priority 4
    issues = check_unclaimed_issues(repo)
    if issues:
        found_any = True
        print(f"[Priority 4] Unclaimed issues ({len(issues)}):")
        for issue in issues:
            labels = ", ".join(l["name"] for l in issue.get("labels", []))
            label_str = f" [{labels}]" if labels else ""
            print(f"  #{issue['number']} — {issue['title']}{label_str}")
        print()

    if not found_any:
        print("No actionable work found.")
        sys.exit(1)
    else:
        print("Actionable work exists — git-contribute can pick this up.")
        sys.exit(0)


if __name__ == "__main__":
    main()
