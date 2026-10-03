# Troubleshooting

All paths are relative to `cf root` unless stated.

## Where to look

- `logs/<unit>-<phase>.log`: streamed worker output, for example
  `logs/pr-123-phase4_implement.log` or `logs/issue-45-phase1_claim_and_plan.log`.
- `logs/failure_counts.json`: consecutive failures per PR.
- The PR itself: the factory comments when it parks a PR, with the log tail.
- `workspace/<repo>/.worktrees/`: one directory per in-flight branch. Empty
  when nothing is running.

## A PR is `bot:failed`

Two consecutive failures park a PR. Read the factory's comment and the log,
fix the cause, then the human removes the label to retry. Do not remove it
yourself. A timeout (30 min default) counts as a failure; rerun with
`--timeout-minutes 45` if the work is simply large.

## A PR is `bot:in-progress` and nothing is running

A worker died. Claims older than 2 hours are cleared automatically the next
time anything routes work. Before then, confirm no `code_factory.py`
process is alive, then the human may remove the label.

## Worktree cleanup refused

The run ends with `WorktreeCleanupRefused` and the PR goes straight to
`bot:failed`. The worktree has uncommitted changes or unpushed commits and
was left in place on purpose. In `workspace/<repo>/.worktrees/<branch>`:

```bash
git status
git log --oneline @{u}..   # unpushed commits, if the branch has an upstream
```

Push or discard deliberately, then `git worktree remove <path>` without
`--force`. Deleting the branch is the destructive step; do it only after
the commits are on the remote.

## Phase 5 says "0 changed files"

The implement phase did not push. Check the phase 4 log for the push error
(often branch protection or a token without Contents write).

## "auto-merge could not be queued" comment

The repo has auto-merge disabled, or the PR is already mergeable with no
required checks (GitHub refuses to queue what it could merge now). See
step 4 of `onboard-repo.md`.

## `RepoConfigError`

`.codefactory.yml` exists on the default branch but is invalid: only `A`
and `B` are valid keys under `tiers`, each a list of glob strings. Fix it
with a PR; until then nothing classifies.

## Phase 2 keeps answering noop or clarify

There is no clear approval from a collaborator. The human should comment
an explicit "approve" (or the requested changes) on the plan PR.

## gh errors

- 404 on a repo the token should see: the token lacks access or is a
  fine-grained PAT not granted this repo.
- Rate limit and 5xx are retried automatically with backoff.
