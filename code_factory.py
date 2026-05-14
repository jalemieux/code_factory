#!/usr/bin/env python3
"""Code Factory — autonomous GitHub contribution orchestrator."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"
ENV_FILE = Path(__file__).parent / ".env"
AGENT_CLI = "claude"
PHASE2_MARKER = "<!-- code-factory:phase2-processed -->"


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} — {msg}", file=sys.stderr)


def load_env(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE pairs from a .env next to the script into os.environ.

    Values in `.env` win over the surrounding shell — the file is the
    authoritative bot identity, so a personal `export GH_TOKEN=...` in
    the user's shell shouldn't silently take over.

    `GIT_AUTHOR_NAME`/`EMAIL` automatically populate the committer
    identity too if those aren't set explicitly, so a single pair of
    keys is enough for the common case.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (len(value) >= 2) and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ[key] = value
    if "GH_TOKEN" in os.environ and "GITHUB_TOKEN" not in os.environ:
        os.environ["GITHUB_TOKEN"] = os.environ["GH_TOKEN"]
    for src, dst in (
        ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"),
        ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"),
    ):
        if src in os.environ and dst not in os.environ:
            os.environ[dst] = os.environ[src]


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


def fetch_review_payload(repo: str, num: int) -> str:
    """Return reviews + per-line review thread comments as pretty JSON.

    `gh pr view --json reviews` only carries review summaries, not the
    line-level diff comments inside each review, and `reviewThreads` isn't
    a `gh pr view` field at all (it's a GraphQL field on `pullRequest`).
    We need both — the prompt asks the LLM to reason about inline comments
    and unresolved threads, so we fetch them via GraphQL in one call.
    """
    owner, name = repo.split("/", 1)
    query = (
        "query($owner: String!, $name: String!, $num: Int!) {"
        "  repository(owner: $owner, name: $name) {"
        "    pullRequest(number: $num) {"
        "      reviews(first: 50) {"
        "        nodes { author { login } state submittedAt body }"
        "      }"
        "      reviewThreads(first: 100) {"
        "        nodes {"
        "          isResolved isOutdated"
        "          comments(first: 50) {"
        "            nodes { author { login } body path line originalLine diffHunk createdAt }"
        "          }"
        "        }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    raw = gh(
        "api", "graphql",
        "-f", f"query={query}",
        "-F", f"owner={owner}",
        "-F", f"name={name}",
        "-F", f"num={num}",
    )
    pr_data = json.loads(raw).get("data", {}).get("repository", {}).get("pullRequest", {})
    return json.dumps(pr_data, indent=2)


def git(*args: str) -> str:
    """Run a git command, raise on failure."""
    cmd = ["git"]
    if os.environ.get("GH_TOKEN"):
        # Force github.com pushes/fetches to authenticate with the token from
        # `.env` (via `gh auth git-credential`) rather than whatever https
        # credentials the user has cached for their personal account.
        cmd.extend([
            "-c", "credential.helper=",
            "-c", "credential.https://github.com.helper=!gh auth git-credential",
        ])
    cmd.extend(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "<no output>"
        raise RuntimeError(
            f"{_fmt_argv('git', args)} (exit {result.returncode}): {detail}"
        )
    return result.stdout.strip()


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower())[:40].strip("-")


def _strip_outer_fence(text: str) -> str:
    """If `text` is fully wrapped in a markdown code fence, remove it."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return stripped
    inner = "\n".join(lines[1:-1]).strip()
    # Only strip if there's no other fence inside — otherwise we'd corrupt nested code blocks
    if "```" in inner:
        return stripped
    return inner


def _issue_num_from_branch(branch: str) -> int | None:
    """Branches created by phase1 are `bot/<num>-<slug>` — extract <num>."""
    m = re.match(r"^bot/(\d+)-", branch or "")
    return int(m.group(1)) if m else None


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


def _run_agent_command(command: list[str], *, cwd: str | None = None) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        detail = stderr or stdout or "(no output)"
        raise RuntimeError(f"{command[0]} failed (exit {result.returncode}): {detail}")
    return result.stdout.strip()


def _codex(prompt: str, *, workdir: str | None = None, interactive: bool = False) -> str:
    with tempfile.NamedTemporaryFile(mode="r+", encoding="utf-8") as tmp:
        command = ["codex", "exec", "-o", tmp.name]
        if interactive:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["--sandbox", "read-only"])
        if workdir:
            command.extend(["-C", workdir])
        command.append(prompt)
        _run_agent_command(command, cwd=workdir)
        tmp.seek(0)
        return tmp.read().strip()


def llm_reason(prompt: str) -> str:
    """Run the configured agent CLI for reasoning tasks."""
    if AGENT_CLI == "codex":
        return _codex(prompt)
    return _run_agent_command(["claude", "-p", prompt, "--print"])


def llm_interactive(prompt: str, workdir: str) -> str:
    """Run the configured agent CLI with tool access for implementation work."""
    if AGENT_CLI == "codex":
        return _codex(prompt, workdir=workdir, interactive=True)
    return _run_agent_command(
        ["claude", "--dangerously-skip-permissions", "-p", prompt, "--print"],
        cwd=workdir,
    )


def get_in_progress_prs(repo: str) -> set[int]:
    prs = gh_json(
        "pr", "list", "--repo", repo,
        "--author", "@me",
        "--label", "bot:in-progress",
        "--json", "number,labels",
    )
    return {pr["number"] for pr in prs if _has_label(pr, "bot:in-progress")}


def check_review_requested(repo: str) -> list[dict]:
    """Priority 1: Own PRs with code review feedback."""
    prs = gh_json(
        "pr", "list", "--repo", repo,
        "--author", "@me",
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
    """Priority 2: Own plan PRs with feedback (on the PR or the linked issue).

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
        "--author", "@me",
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
        "--author", "@me",
        "--label", "bot:plan-accepted",
        "--json", "number,title,labels",
    )
    return [pr for pr in prs if _has_label(pr, "bot:plan-accepted")]


def check_unclaimed_issues(repo: str) -> list[dict]:
    """Priority 4: Unassigned open issues with no existing plan PR from this bot.

    "Linked" = there's an open PR on a branch named `bot/<N>-*`. We don't use
    `--search "#N"` because GitHub does a fuzzy substring match across all PR
    text, so an unrelated PR mentioning "#65" anywhere in its body would mask a
    genuinely unclaimed issue.
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

    actionable = []
    for issue in issues:
        if issue.get("assignees"):
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


def mark_phase2_processed(repo: str, num: int, action: str, summary: str | None = None) -> None:
    body = PHASE2_MARKER
    if action:
        body += f"\naction={action}"
    if summary:
        body += f"\nsummary={summary}"
    gh("pr", "comment", str(num), "--repo", repo, "--body", body)


def get_pr_branch(repo: str, num: int) -> str:
    return gh("pr", "view", str(num), "--repo", repo, "--json", "headRefName", "-q", ".headRefName")


# --- Phase Functions ---
# Each returns (next_phase, context) to chain, or None to exit.


def phase1_claim_and_plan(repo: str, issue: dict) -> tuple[str, dict] | None:
    """Claim an issue and propose an implementation plan via draft PR."""
    num = issue["number"]
    title = issue["title"]
    log(f"Phase 1: claiming issue #{num} — {title}")

    username = gh("api", "user", "-q", ".login")
    gh("api", f"repos/{repo}/issues/{num}/assignees", "-f", f"assignees[]={username}")
    ensure_labels(repo)

    issue_body = gh("issue", "view", str(num), "--repo", repo, "--json", "body", "-q", ".body")
    issue_comments = gh(
        "issue", "view", str(num), "--repo", repo,
        "--json", "comments",
        "--jq", r'.comments[] | "\(.author.login) (\(.createdAt)): \(.body)"',
    ) or "(none)"
    conventions = read_repo_conventions(repo)
    recent_prs = gh(
        "pr", "list", "--repo", repo, "--state", "merged",
        "--limit", "5", "--json", "title,body",
    )

    slug = slugify(title)
    branch = f"bot/{num}-{slug}"
    default_branch = gh("repo", "view", repo, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name")
    git("checkout", default_branch)
    git("pull", "--ff-only")
    # Clean up stale local branch from a previous failed run
    try:
        git("branch", "-D", branch)
    except RuntimeError:
        pass
    git("checkout", "-b", branch)
    git("commit", "--allow-empty", "-m", f"plan: {title} (#{num})")
    git("push", "-u", "origin", branch)

    prompt = load_prompt(
        "phase1_claim_and_plan",
        issue_number=str(num),
        issue_title=title,
        issue_body=issue_body or "(no description)",
        issue_comments=issue_comments,
        conventions=conventions,
        recent_prs=recent_prs,
    )
    plan = llm_reason(prompt)
    plan_body = _strip_outer_fence(plan)
    # Always append `Closes #N` on its own line, outside any fence, so GitHub
    # links the PR to the issue (the LLM often buries it inside a code block).
    plan_body = f"{plan_body}\n\nCloses #{num}"

    pr_url = gh(
        "pr", "create", "--draft", "--repo", repo,
        "--title", title,
        "--body", plan_body,
    )
    # Extract PR number from URL and add label via API to avoid Projects Classic bug
    pr_num = int(pr_url.rstrip("/").split("/")[-1])
    add_label(repo, pr_num, "bot:plan-proposed")
    log(f"Phase 1 complete: draft PR #{pr_num} created for issue #{num}")
    return None


def phase2_process_feedback(repo: str, pr: dict) -> tuple[str, dict] | None:
    """Classify feedback on a plan PR and route accordingly."""
    num = pr["number"]
    log(f"Phase 2: processing feedback on PR #{num}")
    add_in_progress(repo, num)

    pr_comments = gh(
        "pr", "view", str(num), "--repo", repo,
        "--json", "comments",
        "--jq", r'.comments[] | "\(.author.login) (\(.createdAt)): \(.body)"',
    )
    plan_body = gh("pr", "view", str(num), "--repo", repo, "--json", "body", "-q", ".body")

    issue_num = pr.get("issue_number") or _issue_num_from_branch(get_pr_branch(repo, num))
    parts = []
    if pr_comments:
        parts.append(f"### Comments on PR #{num}\n{pr_comments}")
    if issue_num:
        try:
            issue_comments = gh(
                "issue", "view", str(issue_num), "--repo", repo,
                "--json", "comments",
                "--jq", r'.comments[] | "\(.author.login) (\(.createdAt)): \(.body)"',
            )
            if issue_comments:
                parts.append(f"### Comments on linked issue #{issue_num}\n{issue_comments}")
        except RuntimeError:
            pass
    comments = "\n\n".join(parts) or "(no comments found)"

    prompt = load_prompt(
        "phase2_process_feedback",
        pr_number=str(num),
        pr_title=pr["title"],
        plan_body=plan_body,
        comments=comments,
    )
    result = llm_reason(prompt)
    parsed = parse_claude_json(result)

    if not parsed or "action" not in parsed:
        log(f"Phase 2: malformed response from {AGENT_CLI}, will retry next iteration")
        remove_in_progress(repo, num)
        return None

    action = parsed["action"]
    log(f"Phase 2: feedback classified as '{action}'")

    if action == "approve":
        mark_phase2_processed(repo, num, action, parsed.get("summary"))
        swap_label(repo, num, "bot:plan-proposed", "bot:plan-accepted")
        return ("phase4_implement", {"repo": repo, "pr": pr})

    if action == "revise_minor":
        if parsed.get("revised_plan"):
            gh("pr", "edit", str(num), "--repo", repo, "--body", parsed["revised_plan"])
        mark_phase2_processed(repo, num, action, parsed.get("summary"))
        swap_label(repo, num, "bot:plan-proposed", "bot:plan-accepted")
        return ("phase4_implement", {"repo": repo, "pr": pr})

    if action == "revise_major":
        if parsed.get("revised_plan"):
            gh("pr", "edit", str(num), "--repo", repo, "--body", parsed["revised_plan"])
        if parsed.get("comment"):
            gh("pr", "comment", str(num), "--repo", repo, "--body", parsed["comment"])
        mark_phase2_processed(repo, num, action, parsed.get("summary"))
        remove_in_progress(repo, num)
        return None

    if action == "clarify":
        if parsed.get("comment"):
            gh("pr", "comment", str(num), "--repo", repo, "--body", parsed["comment"])
        mark_phase2_processed(repo, num, action, parsed.get("summary"))
        remove_in_progress(repo, num)
        return None

    # noop or unknown
    mark_phase2_processed(repo, num, action, parsed.get("summary"))
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
    git("clean", "-fd")
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
    llm_interactive(prompt, workdir)

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

    reviews = fetch_review_payload(repo, num)

    prompt = load_prompt(
        "phase6_process_review",
        pr_number=str(num),
        pr_title=pr["title"],
        reviews=reviews,
    )
    result = llm_reason(prompt)
    parsed = parse_claude_json(result)

    if not parsed or "action" not in parsed:
        log(f"Phase 6: malformed response from {AGENT_CLI}, will retry next iteration")
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
        llm_interactive(fix_prompt, workdir)

        review_data = json.loads(reviews)
        review_nodes = review_data.get("reviews", {}).get("nodes", []) if isinstance(review_data.get("reviews"), dict) else review_data.get("reviews", [])
        reviewers = {r["author"]["login"] for r in review_nodes if r.get("author")}
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
                "repo", "view", repo,
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
        default_branch = gh(
            "repo", "view", repo,
            "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name",
        )
        git("checkout", default_branch)
        git("pull", "--ff-only")
        log(f"Entered existing clone: {repo}")
    else:
        gh("repo", "clone", repo)
        os.chdir(repo_name)
        log(f"Cloned and entered {repo}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Code Factory — autonomous GitHub contributions")
    parser.add_argument("agent", nargs="?", choices=("claude", "codex"), help="agent CLI to use")
    parser.add_argument("--agent", dest="agent_flag", choices=("claude", "codex"), help="agent CLI to use")
    parser.add_argument("--repo", help="owner/repo (default: current repo)")
    parser.add_argument("--once", action="store_true", help="single pass, then exit")
    args = parser.parse_args()
    agent = args.agent_flag or args.agent or "claude"

    global AGENT_CLI
    AGENT_CLI = agent

    load_env()
    if "GH_TOKEN" in os.environ:
        log(f"Using GH_TOKEN from {ENV_FILE.name}")

    repo = get_repo(args.repo)
    log(f"Code Factory targeting: {repo} (agent: {AGENT_CLI})")
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
