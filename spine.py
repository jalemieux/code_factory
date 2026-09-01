#!/usr/bin/env python3
"""Spine — the GitHub state-machine client shared by every factory process.

The layer between all actors (orchestrator, gates, factory) and remote
GitHub state: label vocabulary and transitions, tier path-globs, branch
naming, WIP limit, and the queue queries. A library, not a process —
GitHub itself remains the only coordinator.

Rule (enforced by a test in tests/test_spine.py): spine never imports
code_factory or any phase logic.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from fnmatch import fnmatchcase
from functools import lru_cache

PHASE2_MARKER = "<!-- code-factory:phase2-processed -->"


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} — {msg}", file=sys.stderr)


def _fmt_argv(prog: str, args: tuple[str, ...]) -> str:
    parts = [prog]
    for a in args:
        if "\n" in a or len(a) > 120:
            parts.append(f"<{len(a)}-char arg>")
        else:
            parts.append(shlex.quote(a))
    return " ".join(parts)


def gh(*args: str) -> str:
    """Run a gh CLI command and return stdout, retrying on rate limits and transient server errors."""
    for attempt in range(4):
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
        stderr_lower = result.stderr.lower()
        transient = (
            "rate limit" in stderr_lower
            or re.search(r"http 5\d\d", stderr_lower)
            or "gateway timeout" in stderr_lower
            or "timeout" in stderr_lower
            or "temporarily unavailable" in stderr_lower
            or "connection reset" in stderr_lower
        )
        if transient and attempt < 3:
            wait = 2 ** attempt * 15
            log(f"Transient gh error, retrying in {wait}s: {result.stderr.strip()[:120]}")
            time.sleep(wait)
            continue
        detail = result.stderr.strip() or result.stdout.strip() or "<no output>"
        raise RuntimeError(
            f"{_fmt_argv('gh', args)} (exit {result.returncode}): {detail}"
        )
    return ""


def gh_json(*args: str) -> list | dict:
    return json.loads(gh(*args) or "[]")


@lru_cache(maxsize=1)
def bot_login() -> str:
    """The gh login this bot authenticates as. Cached — it can't change mid-run."""
    return gh("api", "user", "-q", ".login")


# --- Schema ---
# The one place the state machine is written down: label vocabulary, legal
# transitions, WIP limit, branch naming, and per-repo tier path-globs.
# `bot:in-progress` is a claim marker orthogonal to the state labels, and
# `bot:failed` is reachable from any state (a human unlabels to retry), so
# neither appears as a transition source below.

SCHEMA = {
    "labels": {
        "planning": "bot:planning",
        "plan_proposed": "bot:plan-proposed",
        "plan_accepted": "bot:plan-accepted",
        "in_progress": "bot:in-progress",
        "review_requested": "bot:review-requested",
        "failed": "bot:failed",
    },
    "transitions": {
        "bot:planning": {"bot:plan-proposed"},
        "bot:plan-proposed": {"bot:plan-accepted"},
        "bot:plan-accepted": {"bot:review-requested"},
        # A design objection in review reopens the plan.
        "bot:review-requested": {"bot:plan-proposed"},
    },
    "wip_limit": 10,
    "branch_pattern": "bot/<issue>-<slug>",
    # Tier path-globs, per repo (fnmatch syntax; `*` crosses slashes, so
    # `dir/**` and `dir/*` both match nested paths — when in doubt, tier A).
    # Anything not matching A or B is tier C. Curunir's A set was widened
    # after the spike 0.2 backlog dry-run (dev_stack notes/
    # backlog-classification.md): src/channels/** is the agent's transport
    # layer (both [security] path-traversal PRs and the unauthenticated-
    # listener fix classified C without it); schemas.py/delegate.py are the
    # tool-parse and delegation critical path; run.py is a fat critical
    # entrypoint touched by 19 of 53 diffed PRs.
    "tier_globs": {
        "jalemieux/curunir": {
            "A": (
                "src/agent/**",
                "src/channels/**",
                "src/llm.py",
                "src/tools/dispatcher.py",
                "src/tools/schemas.py",
                "src/tools/delegate.py",
                "run.py",
                "portal/ws_*",
            ),
            "B": ("skills/**", "personas/**"),
        },
    },
}

WIP_LIMIT = SCHEMA["wip_limit"]

# Strictest first — used to resolve mixed-tier diffs.
TIERS = ("A", "B", "C")

# Sentinel for an empty diff (the plan-in-PR-body pattern: one empty commit,
# changedFiles == 0). 56% of the curunir backlog looks like this (spike 0.2);
# a glob classifier would default it to C and auto-merge an empty commit on
# green tests, so it must never classify as a mergeable tier.
PLAN = "PLAN"


def legal_transition(old: str, new: str) -> bool:
    """Whether a state-label move is allowed by the schema.

    `bot:failed` is a legal destination from anywhere; leaving it is a
    human action (unlabel), not a transition the machine performs.
    """
    if new == SCHEMA["labels"]["failed"]:
        return True
    return new in SCHEMA["transitions"].get(old, set())


def _is_security_flagged(title: str | None, labels) -> bool:
    if title and "[security]" in title.lower():
        return True
    for label in labels or ():
        name = label.get("name", "") if isinstance(label, dict) else str(label)
        if "security" in name.lower():
            return True
    return False


def classify(
    changed_files: list[str],
    repo: str,
    title: str | None = None,
    labels: list | None = None,
) -> str:
    """Classify a diff into a tier by the paths it touches. Pure — no network.

    Security override first: '[security]' in the title or a security label
    forces tier A regardless of globs (spike 0.2 caught three security fixes
    that would have auto-merged as tier C).

    An EMPTY diff returns the PLAN sentinel, not a tier — plan-only drafts
    (empty commit, plan in the PR body) must never look auto-mergeable.

    Otherwise mixed-tier diffs take the strictest tier; repos without
    configured globs (and files matching nothing) default to tier C.
    `labels` accepts either label-name strings or GitHub {"name": ...} dicts.
    """
    if _is_security_flagged(title, labels):
        return "A"
    if not changed_files:
        return PLAN
    globs = SCHEMA["tier_globs"].get(repo, {})
    strictest = "C"
    for path in changed_files:
        if any(fnmatchcase(path, g) for g in globs.get("A", ())):
            return "A"
        if any(fnmatchcase(path, g) for g in globs.get("B", ())):
            strictest = "B"
    return strictest


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower())[:40].strip("-")


def _issue_num_from_branch(branch: str) -> int | None:
    """Branches created by phase1 are `bot/<num>-<slug>` — extract <num>."""
    m = re.match(r"^bot/(\d+)-", branch or "")
    return int(m.group(1)) if m else None


def ensure_labels(repo: str) -> None:
    """Create every label in SCHEMA (idempotent via --force)."""
    for label in SCHEMA["labels"].values():
        color = "B60205" if label == SCHEMA["labels"]["failed"] else "0E8A16"
        gh(
            "label", "create", label,
            "--repo", repo,
            "--description", "Managed by git-contribute",
            "--color", color,
            "--force",
        )


def add_label(repo: str, num: int, label: str) -> None:
    gh("api", f"repos/{repo}/issues/{num}/labels", "-f", f"labels[]={label}")


def remove_label(repo: str, num: int, label: str) -> None:
    try:
        gh("api", f"repos/{repo}/issues/{num}/labels/{label}", "-X", "DELETE")
    except RuntimeError:
        pass


def add_in_progress(repo: str, num: int) -> None:
    add_label(repo, num, "bot:in-progress")


def remove_in_progress(repo: str, num: int) -> None:
    remove_label(repo, num, "bot:in-progress")


def swap_label(repo: str, num: int, old: str, new: str) -> None:
    remove_label(repo, num, old)
    add_label(repo, num, new)


def get_repo(repo: str | None = None) -> str:
    if repo:
        return repo
    return gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")


def _has_label(pr: dict, name: str) -> bool:
    """Authoritative check for label presence.

    `gh pr list --label X` queries the search index, which is eventually
    consistent — when labels flip rapidly it returns PRs whose actual
    labels no longer include X. The labels embedded in `--json labels`
    come from the PR detail API and reflect current state, so we
    re-verify before trusting a search hit.
    """
    return any(l.get("name") == name for l in pr.get("labels", []))


def get_in_progress_prs(repo: str) -> set[int]:
    prs = gh_json(
        "pr", "list", "--repo", repo,
        "--label", "bot:in-progress",
        "--json", "number,labels",
    )
    return {pr["number"] for pr in prs if _has_label(pr, "bot:in-progress")}


def get_failed_prs(repo: str) -> set[int]:
    """PRs parked as bot:failed — routing must skip them until a human unlabels."""
    prs = gh_json(
        "pr", "list", "--repo", repo,
        "--label", "bot:failed",
        "--json", "number,labels",
    )
    return {pr["number"] for pr in prs if _has_label(pr, "bot:failed")}


def open_plan_count(repo: str) -> int:
    """Open plan PRs awaiting a human — the number the WIP limit gates on."""
    prs = gh_json(
        "pr", "list", "--repo", repo,
        "--label", "bot:plan-proposed",
        "--json", "number,labels",
        "--limit", "100",
    )
    return sum(1 for pr in prs if _has_label(pr, "bot:plan-proposed"))


def check_review_requested(repo: str) -> list[dict]:
    """Priority 1: PRs with code review feedback.

    Authorship is intentionally not filtered — a `bot:*` label is the opt-in
    signal that the bot should act on a PR, regardless of who opened it. This
    lets a human author a plan PR by hand and hand it off by labeling.
    """
    prs = gh_json(
        "pr", "list", "--repo", repo,
        "--label", "bot:review-requested",
        "--json", "number,title,updatedAt,labels",
    )
    actionable = []
    for pr in prs:
        if not _has_label(pr, "bot:review-requested"):
            continue
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
    """Priority 2: Plan PRs with feedback (on the PR or the linked issue).

    The `bot:plan-proposed` label is the source of truth — we don't also gate
    on `--draft`, because a prior partial run or a manual "ready for review"
    click can flip draft state without changing the label, which would
    otherwise strand the PR with unprocessed feedback.

    Bot vs. human comments are distinguished by the PHASE2_MARKER, not by
    `author.login` — when the bot runs as the human user (same gh account),
    every comment shares the same login, so the marker is the only reliable
    signal that a comment came from the bot.
    """
    prs = gh_json(
        "pr", "list", "--repo", repo,
        "--label", "bot:plan-proposed",
        "--json", "number,title,headRefName,labels",
    )
    actionable = []
    for pr in prs:
        if not _has_label(pr, "bot:plan-proposed"):
            continue
        # `gh ... --json comments` returns {"comments": [...]} — unwrap to the list.
        pr_payload = gh_json(
            "pr", "view", str(pr["number"]), "--repo", repo,
            "--json", "comments",
        )
        pr_comments = pr_payload.get("comments", []) if isinstance(pr_payload, dict) else []

        issue_num = _issue_num_from_branch(pr.get("headRefName", ""))
        issue_comments = []
        if issue_num:
            try:
                issue_payload = gh_json(
                    "issue", "view", str(issue_num), "--repo", repo,
                    "--json", "comments",
                )
                issue_comments = issue_payload.get("comments", []) if isinstance(issue_payload, dict) else []
            except RuntimeError:
                issue_comments = []

        latest_human = None
        latest_marker = None
        for comment in [*pr_comments, *issue_comments]:
            created_at = comment.get("createdAt")
            body = comment.get("body") or ""
            if not created_at:
                continue
            if PHASE2_MARKER in body:
                if latest_marker is None or created_at > latest_marker:
                    latest_marker = created_at
                continue
            if latest_human is None or created_at > latest_human:
                latest_human = created_at

        if latest_human and (latest_marker is None or latest_human > latest_marker):
            pr["issue_number"] = issue_num
            actionable.append(pr)
    return actionable


def check_accepted_plans(repo: str) -> list[dict]:
    """Priority 3: Accepted plans ready for implementation."""
    prs = gh_json(
        "pr", "list", "--repo", repo,
        "--label", "bot:plan-accepted",
        "--json", "number,title,labels",
    )
    return [pr for pr in prs if _has_label(pr, "bot:plan-accepted")]


def check_unclaimed_issues(repo: str) -> list[dict]:
    """Priority 4: Open issues claimable by this bot with no existing plan PR.

    "Linked" = there's an open PR on a branch named `bot/<N>-*`. We don't use
    `--search "#N"` because GitHub does a fuzzy substring match across all PR
    text, so an unrelated PR mentioning "#65" anywhere in its body would mask a
    genuinely unclaimed issue.

    A self-assignment is not a blocker: phase 1 self-assigns the issue before
    opening the plan PR (so the bot can flag intent), and a crash between those
    two steps would otherwise strand the issue — assigned-to-self but with no
    PR, invisible to both the assignee filter and the branch dedup. We skip
    only issues assigned to *someone else*, which is a human claiming the work.
    """
    issues = gh_json(
        "issue", "list", "--repo", repo,
        "--state", "open",
        "--json", "number,title,labels,assignees",
        "--limit", "20",
    )
    open_bot_prs = gh_json(
        "pr", "list", "--repo", repo,
        "--state", "open",
        "--json", "headRefName",
        "--limit", "100",
    )
    claimed_nums = set()
    for pr in open_bot_prs:
        n = _issue_num_from_branch(pr.get("headRefName", ""))
        if n is not None:
            claimed_nums.add(n)

    me = bot_login()
    actionable = []
    for issue in issues:
        others = [a for a in issue.get("assignees", []) if a.get("login") != me]
        if others:
            continue
        if issue["number"] in claimed_nums:
            continue
        actionable.append(issue)
    # Prefer issues with good-first-issue or help-wanted labels
    preferred = {"good first issue", "good-first-issue", "help wanted", "help-wanted"}
    def sort_key(issue: dict) -> int:
        labels = {l["name"].lower() for l in issue.get("labels", [])}
        return 0 if labels & preferred else 1
    actionable.sort(key=sort_key)
    return actionable
