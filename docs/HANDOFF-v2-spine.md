# Handoff: branch `v2-spine`

Written 2026-09-03 so this work can be resumed from another machine or
session. Everything below is verifiable from the branch itself; the plan
it executes lives in a sibling repo.

## What this branch is

Code Factory is being reworked into "agentic stack v2". The approved spec
and the step-by-step plan are in the `dev_stack` repo (a sibling checkout,
docs only):

- `dev_stack/agentic-stack-v2.md` — the spec
- `dev_stack/agentic-stack-v2-plan.md` — the numbered task plan (written 2026-09-01)
- `dev_stack/notes/backlog-classification.md` — spike 0.2 output (curunir backlog tiering)
- `dev_stack/notes/spike-01-worktree-concurrency.md` — spike 0.1 output (worktree cleanup hazards)
- `dev_stack/notes/spike-03-codex-waiting.md` — spike 0.3 output (codex waiting-state detection)

The repo being operated on is `jalemieux/curunir` (120 open PRs at spike
time, 67 of them empty-diff plan drafts).

`v2-spine` is 6 commits ahead of `main` and is pushed to `origin/v2-spine`.

## Commits on the branch, in plan order

| Commit | Plan task | What it does |
|---|---|---|
| `5367b56` | Phase 1 | Extracts `spine.py`: the GitHub state-machine client (labels, legal transitions, WIP limit, branch naming, per-repo tier globs, queue queries). A test enforces that spine never imports `code_factory`. |
| `448f583` | 2.1 | `git(cwd=)`: every git call in every phase passes an explicit working directory. No more implicit `os.chdir` reliance in phases. |
| `c48b9bd` | 2.2 | `Worktree` context manager with a hard cleanup gate. Phases 4 (implement) and 6 (review fixes) run inside `<repo_root>/.worktrees/<branch>`. The main clone is never checked out, cleaned, or pulled by a phase. |
| `45273d1` | 2.3 | Run-one-unit entrypoint: `run --pr N --phase X` and `run --issue N`. `--loop` still exists but is marked deprecated. |
| `9a1c8cd` | 2.4 | Per-phase wall-clock timeout, two-strikes failure protection with `bot:failed`, janitor for stale `bot:in-progress` claims, streamed agent logs, WIP limit on phase 1 routing. |
| `e8b65da` | 0.2 follow-up | Widens curunir tier-A globs, adds the `PLAN` sentinel for empty diffs, adds the security override in `classify()`. |

## Key design decisions that are easy to lose

**Spine is a library, not a process.** GitHub remains the only
coordinator. `spine.SCHEMA` is the one place the state machine is written
down. If you change a label or transition, change it there.

**Worktree cleanup refuses rather than forces.** From spike 0.1: `git
worktree remove` alone is recoverable, the destroyer is the follow-up
`git branch -D`. So the context manager refuses to remove a dirty tree
(never passes `--force`) and refuses to delete a branch with unpushed
commits. On refusal it raises `WorktreeCleanupRefused`, leaves everything
in place, and the phase marks the PR `bot:failed` with an explanatory
comment. If the phase body itself raised, no cleanup is attempted at all.

**Empty diffs are never a tier.** 56% of the curunir backlog is one empty
commit carrying a plan in the PR body. A glob classifier would default
those to tier C and auto-merge an empty commit on green tests. So
`classify([])` returns the `PLAN` sentinel, not `"C"`.

**Security override in classify.** A `[security]` title or a security
label forces tier A regardless of globs. Spike 0.2 found three security
fixes that would otherwise have classified as C.

**Tier globs live in the target repo, not in spine.** Since 2026-10-02
`spine.repo_config(repo)` reads `.codefactory.yml` from the repo's default
branch (contents API, no `ref=`, so a PR cannot demote itself) and
`classify()` takes its globs from there. Curunir's set (widened after the
spike to include `src/channels/**`, `src/tools/schemas.py`,
`src/tools/delegate.py`, and `run.py`) now lives in curunir's own
`.codefactory.yml`. A repo without the file classifies everything as C;
`spine.tiers_configured(repo)` is False in that case and task 3.3 must
refuse to auto-merge on it. A present-but-broken file raises
`RepoConfigError` rather than silently classifying as C.

**Mixed-tier diffs take the strictest tier.** Order is A, B, C.

## Runtime behaviour as of this branch

- Config constants at the top of `code_factory.py`: phase timeout 30 min
  (override with `--timeout-minutes`), failure threshold 2, stale claim
  age 2 hours, WIP limit 10 (from spine).
- `logs/` is gitignored. Streamed agent output goes to
  `logs/<unit>-<phase>.log`; failure counts persist in
  `logs/failure_counts.json`.
- `.env` next to `code_factory.py` holds the bot identity (`GH_TOKEN`,
  `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`). See `.env.example`. `.env`
  overrides the shell environment.
- `run` mode calls `bootstrap_repo(sync=False)`: it clones or enters the
  target repo but does not move its checkout. If you run from inside
  `code_factory/`, the target repo is cloned to `code_factory/<repo_name>/`.
- Routing priority: phase 6 review fixes, then phase 2 plan feedback,
  then phase 4 implement, then phase 1 new issues (only if under the WIP
  limit). `bot:in-progress` and `bot:failed` PRs are skipped. A human
  removes `bot:failed` to retry.
- Phase 5 marks the PR ready, swaps labels, then (task 3.3) classifies
  the final diff with `spine.classify` over the paginated files endpoint
  and queues `gh pr merge --auto --squash` only for tier C on a repo that
  has a `.codefactory.yml`. A and B are left for a human. GitHub performs
  the merge only once branch protection is satisfied, so a required
  review still gates it. A failed queue is commented on the PR, not raised.

## How to run and test

```bash
# tests (135 passing as of e8b65da); scope to tests/ on purpose, see gotchas
python3 -m pytest -q tests

# one unit of work
python3 code_factory.py run --repo jalemieux/curunir --pr 123 --phase 4
python3 code_factory.py run --repo jalemieux/curunir --issue 45
python3 code_factory.py run --repo jalemieux/curunir --pr 123 --phase 6 --agent codex --timeout-minutes 45
```

Phase spellings accepted by `--phase`: `4`, `phase4`, or
`phase4_implement`.

## Gotchas in the current working copy

- **Untracked nested clones.** `curunir/` and `curunir-evals/` are sitting
  inside the code_factory checkout as their own git repos. `curunir/` is
  the bot's main clone, currently on the branch for issue 509 with a
  dirty file. They are not in `.gitignore`, so a bare `pytest` from the
  repo root tries to collect their tests and fails (Python 3.9 vs 3.10+
  syntax, missing `pytest_asyncio`). Either add them to `.gitignore` or
  clone the target repo somewhere else. Nothing in the branch depends on
  them being here.
- The `logs/failure_counts.json` file carries state between runs. Delete
  it for a clean slate.
- `.worktrees/` is gitignored and was empty at handoff. If you find
  leftovers, that is a refused cleanup: inspect before removing.

## Update 2026-09-27

Three commits added after the handoff (`d172de4`, `8815863`, `63c4c41`):
test isolation and pytest scoping, and trust filters so the bot only acts
on reviews, comments and issues from repo collaborators. The first two
gotchas below (nested clones breaking bare `pytest`, stale
`failure_counts.json`) are fixed.

**Task 2.5 is done, on a throwaway repo.** Curunir had no genuine work
queued, so the smoke ran against the private repo
`toddsmartlayer-byte/factory-smoke` (safe to delete). Two `run --pr N
--phase 2` workers ran at the same time; each chained phase 2, 4 and 5.

- Both exited 0 in about 80 seconds, with both worktrees live at once.
- Labels moved `bot:plan-proposed` to `bot:review-requested`; both PRs
  left draft; no `bot:in-progress` or `bot:failed` left behind.
- No cross-talk: each PR holds only its own feature, each log mentions
  only its own feature, and tests pass on both branches.
- The main clone sat on a side branch with a modified file and an
  untracked file throughout. HEAD, branch and every file hash were
  identical afterwards. `.worktrees/` was empty.

Findings from the run:

- **Phase 1 was not isolated; now it is.** It used to check out and pull
  the default branch in the main clone and leave the clone on the new
  `bot/` branch. It now fetches the default branch and cuts the plan
  branch from `origin/<default>` inside a worktree (`Worktree(base=...)`),
  and the planner runs there. Verified with two concurrent `run --issue`
  workers on the smoke repo: both exit 0, main clone byte-identical.
  A retry after a failed planner clears the leftover worktree through the
  usual cleanup gate.
- Worktree cleanup deletes the local `bot/` branches from the main clone
  once the push is verified. This is by design, but it is a change to the
  clone's refs.
- The curunir run is still owed: repeat the smoke there once two plans
  have real approvals.

Curunir itself now has an interaction limit of collaborators only
(expires 2027-03-27), and `main` has branch protection requiring one
approval from a user with write access, admins exempt. The bot token
cannot read that protection and gets a 404.

## Update 2026-10-02

`v2-spine` merged into `main` as PR #2 on 2026-09-27; this document now
describes `main`. Branch `repo-config` (after it) removes the last
repo-specific data from spine: tier globs moved to a `.codefactory.yml` in
each target repo, so the factory can work on any repo its token reaches.
The two-worker smoke on curunir and tasks 3.2 onward are still owed; 3.2
(`factory-gate`) should read the same file.

## Update 2026-10-02, later

- 3.1 merged (curunir #559, the `tests` check).
- 3.2 merged (curunir #564): `factory-gate` runs on `pull_request_target`,
  reads `.codefactory.yml` and the script from the base branch, fails on
  an empty diff or a broken config. Rules vendored from `spine.classify`.
- 3.3 is on branch `auto-merge` in this repo (see runtime behaviour above).
- **Still owed by the operator**, both one API call: require `tests` and
  `factory-gate` as status checks on curunir `main`, and set
  `allow_auto_merge=true` on the repo (it is off, so the queue call fails
  with a comment until then).

## Next steps, per the plan

1. **Task 2.5, two-worker smoke.** With the bot `.env` in place, run two
   `run` invocations concurrently on two different actionable curunir
   units (17 had feedback waiting at spike time). Verify both complete,
   labels move correctly, no cross-talk between worktrees, main clone
   untouched.
2. **3.1** Tests CI on curunir: `.github/workflows/tests.yml`, check named
   `tests`.
3. **3.2** `factory-gate` v0: a GitHub Action that classifies changed
   files with the spine globs and emits a check with the tier in the
   summary. Branch protection on main requires `tests` and `factory-gate`.
4. **3.3** Auto-merge wiring in phase 5: `gh pr merge --auto --squash`
   only when the final-diff tier is C. Never for A. B stays human-merged
   until evals exist.
5. **3.4** One-time backlog triage: clear the 2 stuck `bot:in-progress`
   PRs (#458, #392), close the 67 empty-diff plan drafts with a re-file
   comment (confirm with the human first), batch the 53 diffed PRs by
   tier with security-labeled ones first (#93, #254, #257).
6. **3.5** Run for one week. Success criteria fixed in advance: at least
   10 merges, plan queue stays at or under 10, and you voluntarily open
   the repo. If it fails, stop and re-diagnose before Phase 5 of the plan.

Phases 4 through 6 of the plan (first box, factory dispatcher and web UI,
human-side finishers) wait on the one-week result.
