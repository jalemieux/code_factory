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


def parse_claude_json(output: str) -> dict | None:
    """Extract and parse JSON from claude output. Returns None on failure."""
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
        "--jq", r'.comments[] | "\(.author.login) (\(.createdAt)): \(.body)"',
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

        review_data = json.loads(reviews)
        reviewers = {r["author"]["login"] for r in review_data.get("reviews", []) if r.get("author")}
        for reviewer in reviewers:
            try:
                gh("pr", "edit", str(num), "--repo", repo, "--add-reviewer", reviewer)
            except RuntimeError:
                pass
        remove_in_progress(repo, num)
        return None

    if action == "design_objection":
        swap_label(repo, num, "bot:review-requested", "bot:plan-proposed")
        remove_in_progress(repo, num)
        return None

    remove_in_progress(repo, num)
    return None


PHASES: dict[str, callable] = {
    "phase1_claim_and_plan": phase1_claim_and_plan,
    "phase2_process_feedback": phase2_process_feedback,
    "phase4_implement": phase4_implement,
    "phase5_post_implementation": phase5_post_implementation,
    "phase6_process_review": phase6_process_review,
}


def bootstrap_repo(repo: str) -> None:
    """Ensure the repo is cloned and default branch is synced."""
    # Check if we're already inside the target repo
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
    # Not inside the repo — clone it or enter existing clone
    repo_name = repo.split("/")[-1]
    if os.path.isdir(repo_name):
        os.chdir(repo_name)
        git("pull", "--ff-only")
        log(f"Entered existing clone: {repo}")
    else:
        gh("repo", "clone", repo)
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
