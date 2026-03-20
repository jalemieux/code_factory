# Git Contribute — Troubleshooting

State machine reference, diagnostic flows, and manual fix recipes for the git-contribute lifecycle. Useful for both the bot (self-diagnosis during routing) and human operators inspecting stuck work.

## State Machine Reference

Expected state at each lifecycle point. Use this to answer "is this PR in the right state?"

| Phase | Label | Draft? | Assignee | Branch | Key signals |
|-------|-------|--------|----------|--------|-------------|
| Unclaimed issue | (none) | n/a | nobody | none | Open issue, no linked PRs |
| Phase 1 complete | `bot:plan-proposed` | yes | @me | `bot/{num}-*` | Empty commit, plan in PR body, `Closes #N` |
| Phase 2 → accepted | `bot:plan-accepted` | yes | @me | `bot/{num}-*` | Human comment with approval |
| Phase 4 complete | `bot:plan-accepted` | yes | @me | `bot/{num}-*` | Implementation commits, tests passing |
| Phase 5 complete | `bot:review-requested` | **no** | @me | `bot/{num}-*` | changedFiles > 0, PR is ready |
| Phase 6 → merged | (removed) | no | @me | deleted | PR merged & closed |

Quick check for any PR:
```bash
gh pr view {num} --repo {repo} --json labels,isDraft,author,assignees,state,changedFiles
```

## "Nothing Picked Up" Diagnostic

Walk through this when routing returns "No actionable work found."

### Pre-flight Checks

Run these first — they catch the most common root causes:

```bash
# Who is the bot authenticated as?
gh auth status

# Can the bot access the repo?
gh repo view {repo}

# Do bot labels exist?
gh label list --repo {repo} --search "bot:"
```

### Priority 1: Review Feedback

```bash
gh pr list --repo {repo} --author @me --label "bot:review-requested" --json number,title
```
- **PRs exist but skipped:** Last review is older than last commit — no new feedback yet.
- **Common issue:** Reviewer commented but didn't submit a formal GitHub review. Comments alone don't trigger Priority 1 — only `reviews` do.

### Priority 2: Plan Feedback

```bash
gh pr list --repo {repo} --author @me --draft --label "bot:plan-proposed" --json number,title
```
- **PRs exist but skipped:** No comments on the PR — human hasn't responded yet.
- **Common issue:** PR authored by wrong GitHub account. If `gh auth status` shows a different user than the PR author, `--author @me` won't match. This is the #1 cause of "nothing picked up" when work clearly exists.

### Priority 3: Accepted Plans

```bash
gh pr list --repo {repo} --author @me --label "bot:plan-accepted" --json number,title
```
- **Common issue:** Label never swapped — still says `bot:plan-proposed`. Check Phase 2 ran and classified feedback as approval.

### Priority 4: Unclaimed Issues

```bash
gh issue list --repo {repo} --state open --json number,title,assignees --limit 20
```
- **Issues exist but skipped:** Already assigned, or an open PR links to the issue.
- **Common issue:** Issue assigned to wrong account, or old PR still linking.

Check for linked PRs:
```bash
gh pr list --repo {repo} --search "#{issue_num}" --state open --json number,state,title
```

## "Stuck in Phase X" Diagnostics

### Stuck after Phase 1 — Plan proposed, no response

- Verify PR is draft with `bot:plan-proposed` label
- Verify PR body contains `Closes #N`
- Check PR was actually created: `gh pr list --repo {repo} --author @me --draft`
- **Likely not stuck** — just waiting for human review

### Stuck after Phase 2 — Feedback exists, not processed

- Check: does comment contain a clear approval signal? Bot looks for "LGTM", "looks good", "approved", "ship it"
- Check: was feedback ambiguous? Bot may have exited waiting for clarification
- Check: label still `bot:plan-proposed`? If it was swapped to `bot:plan-accepted` but nothing happened, routing failed on the next invocation

### Stuck after Phase 4 — Implemented but not marked ready

- Check: did push land? If `changedFiles` is 0, commits didn't make it to remote:
  ```bash
  gh pr view {num} --repo {repo} --json changedFiles --jq '.changedFiles'
  ```
- Check: worktree cleanup may have deleted unpushed commits
- Check: CI failing? Bot won't proceed if verification fails

### Stuck after Phase 5 — Ready for review, no response

- Verify PR is **not** draft and has `bot:review-requested` label
- **Likely not stuck** — waiting for human code review
- Check: reviewer may have commented but not submitted a formal GitHub review (comments != reviews in the GitHub API)

### Stuck after Phase 6 — Review addressed, still waiting

- Check: did bot re-request review?
  ```bash
  gh pr view {num} --repo {repo} --json reviewRequests
  ```
- Check: reviewer approved but bot didn't merge — bot only merges on formal GitHub approval (via reviews API), not comment approval

## Manual Fix Recipes

### Wrong author — PR created under wrong GitHub account

```bash
gh pr close {num} --repo {repo}
gh issue edit {issue_num} --repo {repo} --remove-assignee {wrong_account}
# Bot picks up the issue fresh on next loop
```

### Label stuck in wrong state

```bash
# Force-advance to accepted
gh pr edit {num} --repo {repo} --remove-label "bot:plan-proposed" --add-label "bot:plan-accepted"

# Reset back to plan review
gh pr edit {num} --repo {repo} --remove-label "bot:plan-accepted" --add-label "bot:plan-proposed"
```

### Push didn't land (0 changed files on ready PR)

```bash
gh pr ready {num} --repo {repo} --undo
gh pr edit {num} --repo {repo} --remove-label "bot:review-requested" --add-label "bot:plan-accepted"
# Bot will retry implementation on next loop
```

### Issue won't get picked up — old PR still linking

```bash
# Check what's linking
gh pr list --repo {repo} --search "#{issue_num}" --state open --json number,state,title
# Only open PRs block pickup — closed PRs are ignored
```

### Bot labels don't exist

```bash
for label in "bot:plan-proposed" "bot:plan-accepted" "bot:review-requested"; do
  gh label create "$label" --repo {repo} --description "Managed by git-contribute" --color "0E8A16" --force
done
```

### Reset an issue completely (start over)

```bash
gh pr close {num} --repo {repo}
gh issue edit {issue_num} --repo {repo} --remove-assignee @me
# Bot picks it up fresh on next loop
```

## Pre-flight Checklist

Run before first deployment or after environment changes:

1. **Auth identity:** `gh auth status` — confirm the bot account, not your personal account
2. **Repo access:** `gh repo view {repo}` — confirm the bot can see the repo
3. **Labels:** `gh label list --repo {repo} --search "bot:"` — all three labels exist
4. **Permissions:** bot account can push branches, create PRs, edit labels, assign issues
5. **Claude Code:** `claude --version` — installed and accessible from the run loop
6. **Skill available:** the `git-contribute` skill is loadable by Claude Code
