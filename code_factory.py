#!/usr/bin/env python3
"""Code Factory — autonomous GitHub contribution orchestrator."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

# GitHub state-machine primitives live in spine (see agentic-stack-v2.md);
# this file keeps the phase logic and drives them.
from spine import (
    PHASE2_MARKER,
    _fmt_argv,
    _issue_num_from_branch,
    add_in_progress,
    add_label,
    bot_login,
    check_accepted_plans,
    check_plan_feedback,
    check_review_requested,
    check_unclaimed_issues,
    ensure_labels,
    get_in_progress_prs,
    get_repo,
    gh,
    gh_json,
    log,
    remove_in_progress,
    slugify,
    swap_label,
)

PROMPTS_DIR = Path(__file__).parent / "prompts"
ENV_FILE = Path(__file__).parent / ".env"
AGENT_CLI = "claude"

# Absolute path of the main clone, set by bootstrap_repo(). Phase code asks
# for it via _repo_root() and passes it (or a worktree path) explicitly to
# every git() call — nothing below bootstrap relies on the process cwd.
REPO_ROOT: str | None = None


def _repo_root() -> str:
    return REPO_ROOT or os.getcwd()


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


def git(*args: str, cwd: str | None = None) -> str:
    """Run a git command, raise on failure.

    `cwd=None` keeps legacy behavior (the process working directory); phase
    code always passes an explicit cwd so work can happen in worktrees
    without `os.chdir` games.
    """
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
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "<no output>"
        raise RuntimeError(
            f"{_fmt_argv('git', args)} (exit {result.returncode}): {detail}"
        )
    return result.stdout.strip()


def git_retry(*args: str, cwd: str | None = None, attempts: int = 3, delay: float = 0.3) -> str:
    """git() with a short retry for shared-state lock contention.

    Sibling worktrees share one `.git` — concurrent commands that write
    shared files (`config`, worktree bookkeeping, packed-refs) can collide
    on a `.lock` (observed in spike 0.1 on concurrent `push -u`). Three
    attempts with a short sleep is enough; anything else re-raises.
    """
    last_error: RuntimeError | None = None
    for attempt in range(attempts):
        try:
            return git(*args, cwd=cwd)
        except RuntimeError as e:
            message = str(e).lower()
            retryable = "could not lock" in message or "unable to lock" in message or ".lock" in message
            if not retryable or attempt == attempts - 1:
                raise
            last_error = e
            log(f"git lock contention, retrying in {delay}s: {e}")
            time.sleep(delay)
    raise last_error  # unreachable, but keeps the type checker honest


class WorktreeCleanupRefused(RuntimeError):
    """Worktree cleanup declined to destroy state (dirty tree or unpushed work).

    Everything is left in place; the caller decides what to do (phases mark
    the PR `bot:failed` with an explanatory comment).
    """


class Worktree:
    """A git worktree for exactly one unit of work.

    Enter: `git worktree prune` (clears stale entries from crashed workers),
    then `git worktree add` under `<repo_root>/.worktrees/` on <branch>,
    creating the branch from `origin/<branch>` (or HEAD) if needed.

    Exit: hard-gated cleanup per spike 0.1 (dev_stack notes/spike-01):
    `worktree remove` alone is recoverable (the branch ref survives in the
    shared clone) — the destroyer is the follow-up `git branch -D`. So:

      - dirty tree        → refuse removal; NEVER pass `--force`.
      - unpushed commits  → refuse `branch -D`. Measured by
        `rev-list --count @{upstream}..HEAD`, falling back to
        `origin/<branch>..HEAD`; "no upstream and no remote ref" is
        treated as unpushed.

    On refusal everything stays in place and WorktreeCleanupRefused is
    raised. If the body itself raised, no cleanup is attempted (the
    worktree is left for inspection) and the original exception propagates.
    """

    def __init__(self, repo_root: str, branch: str):
        self.repo_root = os.path.abspath(repo_root)
        self.branch = branch
        self.path = os.path.join(self.repo_root, ".worktrees", branch.replace("/", "-"))

    def _ref_exists(self, ref: str) -> bool:
        try:
            git("rev-parse", "--verify", "--quiet", ref, cwd=self.repo_root)
            return True
        except RuntimeError:
            return False

    def __enter__(self) -> "Worktree":
        git_retry("worktree", "prune", cwd=self.repo_root)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if self._ref_exists(f"refs/heads/{self.branch}"):
            git_retry("worktree", "add", self.path, self.branch, cwd=self.repo_root)
        elif self._ref_exists(f"refs/remotes/origin/{self.branch}"):
            git_retry(
                "worktree", "add", "-b", self.branch, self.path,
                f"origin/{self.branch}", cwd=self.repo_root,
            )
        else:
            git_retry("worktree", "add", "-b", self.branch, self.path, cwd=self.repo_root)
        return self

    def is_dirty(self) -> bool:
        return bool(git("status", "--porcelain", cwd=self.path))

    def unpushed_count(self) -> int | None:
        """Commits on HEAD that the remote doesn't have; None = no remote ref at all."""
        try:
            return int(git("rev-list", "--count", "@{upstream}..HEAD", cwd=self.path))
        except RuntimeError:
            pass
        # Workers push with an explicit refspec (no -u), so there's usually no
        # upstream — but the push still updates refs/remotes/origin/<branch>.
        try:
            return int(git("rev-list", "--count", f"origin/{self.branch}..HEAD", cwd=self.path))
        except RuntimeError:
            return None

    def cleanup(self) -> None:
        """Remove the worktree and delete the local branch — gated. May refuse."""
        if self.is_dirty():
            raise WorktreeCleanupRefused(
                f"worktree {self.path} has uncommitted changes; "
                "refusing removal (and never passing --force)"
            )
        unpushed = self.unpushed_count()
        if unpushed is None or unpushed > 0:
            reason = (
                "no upstream/remote ref (treated as unpushed)"
                if unpushed is None
                else f"{unpushed} unpushed commit(s)"
            )
            raise WorktreeCleanupRefused(
                f"branch {self.branch} has {reason}; refusing `git branch -D` "
                f"(worktree left at {self.path})"
            )
        git_retry("worktree", "remove", self.path, cwd=self.repo_root)
        git_retry("branch", "-D", self.branch, cwd=self.repo_root)

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            log(
                f"Worktree {self.path}: leaving worktree and branch in place "
                f"after {exc_type.__name__}"
            )
            return False
        self.cleanup()
        return False


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


def read_repo_conventions(repo: str, cwd: str | None = None) -> str:
    conventions = []
    for fname in ("CONTRIBUTING.md", "CLAUDE.md", "AGENTS.md", "CODING_GUIDELINES.md"):
        try:
            content = git("show", f"HEAD:{fname}", cwd=cwd or _repo_root())
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

    gh("api", f"repos/{repo}/issues/{num}/assignees", "-f", f"assignees[]={bot_login()}")
    ensure_labels(repo)

    issue_body = gh("issue", "view", str(num), "--repo", repo, "--json", "body", "-q", ".body")
    issue_comments = gh(
        "issue", "view", str(num), "--repo", repo,
        "--json", "comments",
        "--jq", r'.comments[] | "\(.author.login) (\(.createdAt)): \(.body)"',
    ) or "(none)"
    root = _repo_root()
    conventions = read_repo_conventions(repo, cwd=root)
    recent_prs = gh(
        "pr", "list", "--repo", repo, "--state", "merged",
        "--limit", "5", "--json", "title,body",
    )

    slug = slugify(title)
    branch = f"bot/{num}-{slug}"
    default_branch = gh("repo", "view", repo, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name")
    git("checkout", default_branch, cwd=root)
    git("pull", "--ff-only", cwd=root)
    # Clean up stale local branch from a previous failed run
    try:
        git("branch", "-D", branch, cwd=root)
    except RuntimeError:
        pass
    # Also delete any stale remote branch — a prior phase1 may have pushed
    # before failing, leaving a remote ref that would cause non-fast-forward
    # rejection when we push the fresh branch built from the default branch.
    try:
        git("push", "origin", "--delete", branch, cwd=root)
    except RuntimeError:
        pass
    git("checkout", "-b", branch, cwd=root)
    git("commit", "--allow-empty", "-m", f"plan: {title} (#{num})", cwd=root)
    # Explicit refspec, no -u: upstream tracking writes shared .git/config,
    # which contends across concurrent worktree workers (spike 0.1).
    git_retry("push", "origin", f"HEAD:{branch}", cwd=root)

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
        "--head", branch,
        "--base", default_branch,
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

    # The main clone is never checked out, cleaned, or pulled here — all
    # work happens in an isolated worktree on the PR branch.
    root = _repo_root()
    git("fetch", "origin", branch, cwd=root)

    prompt = load_prompt(
        "phase4_implement",
        pr_number=str(num),
        plan=plan,
        repo=repo,
        branch=branch,
    )
    with Worktree(root, branch) as wt:
        # A pre-existing local branch may lag the remote; catch up ff-only.
        git("merge", "--ff-only", f"origin/{branch}", cwd=wt.path)
        llm_interactive(prompt, wt.path)

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
        # Never touch the main clone's checkout — fixes happen in a worktree.
        root = _repo_root()
        git("fetch", "origin", branch, cwd=root)

        fix_prompt = load_prompt(
            "phase6_apply_fixes",
            pr_number=str(num),
            pr_title=pr["title"],
            reviews=reviews,
            branch=branch,
        )
        with Worktree(root, branch) as wt:
            git("merge", "--ff-only", f"origin/{branch}", cwd=wt.path)
            llm_interactive(fix_prompt, wt.path)

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


def bootstrap_repo(repo: str, sync: bool = True) -> None:
    """Ensure the repo is cloned, record REPO_ROOT, and optionally sync.

    `sync=True` (legacy loop behavior) checks out and ff-pulls the default
    branch. `run` mode passes `sync=False`: each phase fetches exactly what
    it needs, and worktree-based phases must never have the main clone's
    checkout moved underneath them.
    """
    global REPO_ROOT
    # Check if we're already inside the target repo
    try:
        current_repo = gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        if current_repo == repo:
            REPO_ROOT = git("rev-parse", "--show-toplevel", cwd=os.getcwd())
            if sync:
                default_branch = gh(
                    "repo", "view", repo,
                    "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name",
                )
                git("checkout", default_branch, cwd=REPO_ROOT)
                git("pull", "--ff-only", cwd=REPO_ROOT)
            return
    except RuntimeError:
        pass
    # Not inside the repo — clone it or enter existing clone
    repo_name = repo.split("/")[-1]
    if os.path.isdir(repo_name):
        os.chdir(repo_name)
        REPO_ROOT = os.getcwd()
        if sync:
            default_branch = gh(
                "repo", "view", repo,
                "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name",
            )
            git("checkout", default_branch, cwd=REPO_ROOT)
            git("pull", "--ff-only", cwd=REPO_ROOT)
        log(f"Entered existing clone: {repo}")
    else:
        gh("repo", "clone", repo)
        os.chdir(repo_name)
        REPO_ROOT = os.getcwd()
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
