---
name: git-contribute
description: "Autonomous bug fix and feature implementation lifecycle for GitHub codebases — picks up open issues, proposes an implementation plan via draft PR for human review, incorporates feedback, implements the fix or feature using TDD, and shepherds the PR through code review to merge. Covers the full issue-to-merge lifecycle: claim → plan → review → implement → verify → merge. Trigger: user asks to contribute to a repo, fix bugs, implement features, or invoked on a loop to pick up work from a GitHub issue tracker."
---

# Git Contribute

Autonomous contribution workflow for a git repository. Each invocation picks up ONE issue at its most advanced lifecycle state and moves it one phase forward.

**Requires:** `gh` CLI (authenticated with sufficient repo permissions)

## Parameters

- **repo** — `owner/repo` (optional). Defaults to current repo via `gh repo view --json nameWithOwner -q .nameWithOwner`

## Workflow

### Step 0: Ensure Local Clone

1. Determine target repo:
   - If `repo` param given, use it
   - Otherwise, derive from current directory: `gh repo view --json nameWithOwner -q .nameWithOwner`
2. Ensure cloned locally:
   ```bash
   # If not already in the repo directory:
   gh repo clone {owner/repo} && cd {repo-name}
   ```
3. Sync default branch:
   ```bash
   default_branch=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)
   git checkout "$default_branch" && git pull --ff-only
   ```

### Step 1: Route

Scan in priority order — **finish existing work before starting new work.** Take the FIRST match.

**Priority 1 — Own PRs with code review feedback:**
```bash
# Find ready (non-draft) PRs by me with the review-requested label
gh pr list --author @me --label "bot:review-requested" --json number,title,updatedAt
```
For each, check if there are review comments newer than your last commit:
```bash
gh pr view {num} --json reviews,commits --jq '{last_review: .reviews[-1].submittedAt, last_commit: .commits[-1].committedDate}'
```
If review is newer → **Phase 6**

**Priority 2 — Own PRs with plan feedback:**
```bash
gh pr list --author @me --label "bot:plan-proposed" --draft --json number,title,comments
```
Check for comments from humans (not the bot author). If found → **Phase 2**

**Priority 3 — Accepted plans ready for implementation:**
```bash
gh pr list --author @me --label "bot:plan-accepted" --json number,title
```
If found → **Phase 4**

**Priority 4 — Unclaimed issues:**
```bash
# Search for unassigned open issues
gh search issues --repo {owner/repo} --no-assignee --state open --json number,title,labels --limit 20
```
Filter out issues that already have linked PRs:
```bash
# For each candidate, check for existing PRs
gh pr list --search "#{issue_num}" --state all --json number --jq 'length'
```
Skip any issue where this returns > 0. If candidates remain → **Phase 1**

**Nothing found** → exit: "No actionable work found."

### Phase 1: Claim & Plan

1. Pick ONE issue (prefer labels: `good-first-issue`, `help-wanted`)
2. Self-assign:
   ```bash
   gh issue edit {num} --add-assignee @me
   ```
3. Read the full issue body and all comments
4. Read repo conventions:
   - `CONTRIBUTING.md`, `CLAUDE.md`, `AGENTS.md`, `CODING_GUIDELINES.md` — whatever exists
   - Recent merged PRs for style: `gh pr list --state merged --limit 5 --json title,body`
   - Directory structure and test patterns
5. Generate implementation plan — structured as:
   ```markdown
   ## Problem
   {what the issue asks for, in your own words}

   ## Approach
   {high-level strategy — what changes, why this approach}

   ## Planned Changes
   - `{file}`: {what changes and why}
   - ...

   ## Test Strategy
   {how the implementation will be verified}

   ## Risks / Open Questions
   - {anything uncertain or worth flagging}
   ```
6. Ensure labels exist (first-time only — safe to repeat, **must run before creating the PR**):
   ```bash
   for label in "bot:plan-proposed" "bot:plan-accepted" "bot:review-requested"; do
     gh label create "$label" --description "Managed by git-contribute" --color "0E8A16" --force 2>/dev/null
   done
   ```
7. Create draft PR:
   ```bash
   slug=$(echo "{issue_title}" | tr '[:upper:]' '[:lower:]' | tr -cs '[:alnum:]' '-' | head -c 40)
   git checkout -b bot/{issue_num}-${slug}
   git commit --allow-empty -m "plan: {issue title} (#{issue_num})"
   git push -u origin bot/{issue_num}-${slug}
   ```
   ```bash
   gh pr create --draft \
     --title "{issue title}" \
     --body "{plan_body}" \
     --label "bot:plan-proposed"
   ```
   The PR body MUST include `Closes #{issue_num}` to link the issue.
8. **Exit** — wait for feedback.

### Phase 2: Process Plan Feedback

1. Read all comments on the PR:
   ```bash
   gh pr view {num} --json comments --jq '.comments[] | "\(.author.login) (\(.createdAt)): \(.body)"'
   ```
2. Classify the feedback:

   | Signal | Action |
   |--------|--------|
   | Explicit approval ("LGTM", "looks good", "approved", "ship it") | Relabel → **Phase 4** |
   | Approval with conditions ("looks good, but change X") | → **Phase 3** |
   | Rejection / major rethink | Rewrite plan, update PR body, post comment explaining, **exit** |
   | Questions / clarification needed | Answer in PR comment, **exit** |
   | No actionable feedback (reactions only, unrelated chatter) | **exit** |

3. To route to Phase 4:
   ```bash
   gh pr edit {num} --remove-label "bot:plan-proposed" --add-label "bot:plan-accepted"
   ```

### Phase 3: Revise Plan

1. Identify all requested changes from feedback
2. Incorporate changes into the plan
3. Update the PR body with the revised plan:
   ```bash
   gh pr edit {num} --body "{updated_plan}"
   ```
4. Post a comment summarizing what changed and why
5. Decision:
   - Changes were minor AND reviewer pre-approved → relabel `bot:plan-accepted` → **Phase 4**
   - Changes were substantial → **exit** and wait for re-review

### Phase 4: Implement

Load the plan, then invoke superpowers skills in sequence:

1. **Read the plan:**
   ```bash
   gh pr view {num} --json body -q .body
   ```
2. **Isolation** — invoke `using-git-worktrees` skill to create a worktree on the PR branch
3. **Execution** — invoke `executing-plans` with the plan from the PR body. If the plan has parallelizable tasks, use `subagent-driven-development` instead.
4. **TDD** — invoke `test-driven-development` for each component
5. **Debug** — invoke `systematic-debugging` if any tests fail
6. **Verify** — invoke `verification-before-completion` before declaring done
7. **Push from worktree** — push commits to the remote BEFORE exiting the worktree. Worktree cleanup may delete the local branch and its commits.
   ```bash
   git push origin HEAD:bot/{issue_num}-{slug}
   ```

After all steps pass → **Phase 5**

### Phase 5: Post Implementation

1. Push all commits to the PR branch (if not already pushed from worktree in Phase 4 step 7)
2. **Verify push landed** — confirm the implementation commits exist on the remote before proceeding. **Do not continue if this check fails.**
   ```bash
   gh pr view {num} --json commits --jq '.commits[-1].oid'
   ```
   Compare the latest commit SHA against what you pushed. Also verify changed files are present:
   ```bash
   gh pr view {num} --json changedFiles --jq '.changedFiles'
   ```
   If `changedFiles` is 0 or the commit SHA doesn't match, the push failed — diagnose and retry. **Never mark a PR ready with 0 changed files.**
3. Convert from draft and relabel:
   ```bash
   gh pr ready {num}
   gh pr edit {num} --remove-label "bot:plan-accepted" --add-label "bot:review-requested"
   ```
4. Post a summary comment:
   - What was implemented
   - Any deviations from the plan (and why)
   - Test results / CI status
5. **Exit** — wait for review.

### Phase 6: Process Code Review

1. Read all review comments:
   ```bash
   gh pr view {num} --json reviews,reviewThreads
   ```
2. Classify:

   | Signal | Action |
   |--------|--------|
   | Approved (no changes needed) | Merge: `gh pr merge {num} --squash --delete-branch` |
   | Changes requested (specific fixes) | Apply fixes, push, reply to each thread, **exit** |
   | Fundamental design objection | Revise plan → relabel `bot:plan-proposed`, update PR body, **exit** |

3. For each inline review comment:
   - Apply the fix, OR
   - Reply with reasoning for why no change is needed
4. After pushing fixes:
   ```bash
   # Re-request review from the reviewer
   gh pr edit {num} --add-reviewer {reviewer_login}
   ```
5. **Exit** — wait for re-review.

## Tips

- Always re-read `CONTRIBUTING.md` before planning — conventions change.
- Keep plans to bullet points. Reviewers skim.
- When feedback is ambiguous, ask for clarification rather than guessing intent.
- Check CI status (`gh pr checks {num}`) before declaring implementation done.
- Prefer the repo's existing test framework and patterns over introducing new ones.

## Common Mistakes

- **Implementing before plan acceptance** — never skip the plan review phase. The PR exists for human oversight.
- **Claiming multiple issues** — one issue per invocation. The router picks the highest-priority item.
- **Ignoring repo conventions** — every repo has its own style. Read `CONTRIBUTING.md` and recent merged PRs before planning.
- **Not linking PR to issue** — always include `Closes #{num}` so the issue auto-closes on merge.
- **Force-pushing** — never force-push to a PR branch with review comments. It destroys review context.
- **Treating silence as approval** — "no new comments" does not mean approved. Only explicit approval triggers Phase 4.
- **Skipping verification** — always run `verification-before-completion` before posting. Don't claim "tests pass" without evidence.
- **Marking PR ready before verifying push** — always confirm commits exist on the remote (`gh pr view --json changedFiles`) before calling `gh pr ready`. Worktree cleanup can destroy unpushed commits.
- **Merging without explicit approval** — only merge when the reviewer has approved via GitHub's review system, not just a comment.
