---
name: code-factory
description: "Use when asked to run Code Factory or 'the factory bot' on a GitHub repo: pick up an issue and plan it, process plan feedback, implement an accepted plan, apply review fixes, see what factory work is waiting, check why a bot PR is stuck or bot:failed, find a PR's risk tier, or set up a new repo (.codefactory.yml, factory-gate check) so the factory can work on it."
---

# Code Factory

Code Factory moves a GitHub issue to a merged PR in phases, with a human
approving the plan and the merge. GitHub labels are the only state. You
drive it one unit of work at a time through `scripts/cf` in this skill's
directory (called `cf` below; run it by its path). It works on any repo
the bot token can reach.

`cf` spawns its own worker agent (`claude` or `codex`) for the actual
planning and coding. Your job is to choose the unit, launch it, and report
what happened. Do not do the phase's work yourself in the target repo.

## Usage

```bash
cf doctor owner/repo        # prerequisites + is this repo set up? (read-only)
cf queue owner/repo         # actionable work, each with its exact command (read-only)
cf tier owner/repo 123      # risk tier of one PR (read-only)

cf run --repo owner/repo --issue 45            # phase 1: claim issue, open draft plan PR
cf run --repo owner/repo --pr 123 --phase 2    # read plan feedback; on approval chains 4 then 5
cf run --repo owner/repo --pr 123 --phase 4    # implement accepted plan, then 5
cf run --repo owner/repo --pr 123 --phase 6    # act on code review: apply fixes, or merge if approved
```

Options for `run`: `--agent claude|codex` (default claude),
`--timeout-minutes N` (default 30 per agent invocation). Exit 0 is success,
1 is a failed phase. A run takes minutes; start it in the background or
with a long timeout and read `logs/<unit>-<phase>.log` under `cf root`.

Start every session with `cf doctor owner/repo`, then `cf queue`. If doctor
reports the repo is not set up, read `references/onboard-repo.md`.

## The lifecycle

| Label on the PR | Meaning | Who moves it | Command |
|---|---|---|---|
| (issue, no PR) | unclaimed work | factory | `--issue N` |
| `bot:plan-proposed` | draft PR, plan in the body, empty diff | human comments, then factory | `--pr N --phase 2` |
| `bot:plan-accepted` | plan approved | factory | `--pr N --phase 4` |
| `bot:review-requested` | implemented, ready for review | human reviews, then factory | `--pr N --phase 6` |
| `bot:in-progress` | a worker holds this PR | cleared on exit; stale after 2h | wait |
| `bot:failed` | parked after 2 failures | human removes label to retry | see troubleshooting |

Phase 5 runs automatically after phase 4: it marks the PR ready, classifies
the diff, and queues a squash auto-merge for tier C only.

## Risk tiers

Each target repo declares tiers in `.codefactory.yml` on its default branch.

- **A** critical path: always human-reviewed, never auto-merged.
- **B** medium: human-merged.
- **C** everything else: auto-merge is queued; GitHub merges once required
  checks pass and branch protection is satisfied.
- A diff takes the strictest tier of any file it touches.
- `[security]` in the title or a security label forces A.
- An empty diff is `PLAN`, never a tier, and can never merge.
- No `.codefactory.yml` means "unconfigured": nothing auto-merges.

## Examples

Pick up whatever is ready on a repo:

```bash
cf doctor acme/api && cf queue acme/api
# [plan-feedback] #212 Add retry to webhook sender
#     cf run --repo acme/api --pr 212 --phase 2
cf run --repo acme/api --pr 212 --phase 2
cf tier acme/api 212      # report the tier and whether auto-merge was queued
```

A PR is stuck:

```bash
cf queue acme/api         # shows in-progress and failed PR numbers
# then follow references/troubleshooting.md for that PR
```

## Tips

- Order of preference when several units are ready: review fixes (6), plan
  feedback (2), accepted plans (4), new issues (1). `cf queue` lists them
  in that order.
- When open plans are at the WIP limit, do not plan new issues; a human has
  a review backlog. Say so instead.
- Only issues, comments and reviews from repo collaborators count. A
  stranger's "LGTM" is ignored by design.
- Units on different PRs can run concurrently; each works in its own git
  worktree. Never run two units on the same PR.
- The factory acts as the identity in `.env` at `cf root` (`GH_TOKEN`,
  `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`). Without it, it acts as your own
  `gh` login, and then it cannot tell its comments from the human's.

## Common Mistakes

- **Approving on the human's behalf.** Never comment "approve", submit a
  review, or remove `bot:failed` to push work through. Those are the human
  checkpoints. Report what is waiting and stop.
- **Changing repo settings unasked.** Branch protection, required checks
  and the auto-merge switch belong to the repo owner. Show the commands
  from `references/onboard-repo.md` and let them run or approve them.
- **Running phase 2 with no new feedback.** It spends an agent run to
  conclude "noop". Trust `cf queue`: if a PR is not listed, nothing new
  from a collaborator is waiting on it.
- **Editing `.codefactory.yml` in a PR to change that PR's tier.** The file
  is read from the default branch only; the change has no effect until
  merged, and the merge itself needs review.
- **Forcing a refused worktree cleanup.** A leftover directory under
  `.worktrees/` in the target clone means unpushed or dirty work. Inspect
  it; never `git worktree remove --force` or `git branch -D` it blind.
- **Working in the factory's clone.** Target repos are cloned under
  `workspace/` at `cf root`. Do not check out branches or edit files there.
