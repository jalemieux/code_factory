# Code Factory Orchestrator — Design Spec

## Problem

The `git-contribute` skill is a 260-line prompt (SKILL.md) that encodes a state machine workflow. The routing logic, label management, and phase transitions are deterministic — they don't need LLM reasoning — but they're expressed as natural language instructions that Claude interprets on every invocation. This leads to fragile behavior: recent commits show a stream of bug fixes for duplicate processing, label races, push verification, and assignment filtering — all issues that would be caught by tests in code.

Meanwhile, `check_actionable.py` already implements the routing logic in Python, and `run_loop.sh` handles polling in bash. The workflow is split across three artifacts (bash, Python, markdown) with no shared error handling or testing.

## Goal

Replace all three artifacts with a single Python script (`code_factory.py`) that orchestrates the full issue-to-merge lifecycle. The script handles all deterministic logic (polling, routing, label management, phase transitions) in code and shells out to the `claude` CLI only for tasks that require LLM reasoning (plan generation, feedback classification, code implementation, review processing).

## Architecture

### Entry Point & Modes

`code_factory.py` is the single entry point with two modes:

```
python3 code_factory.py                    # continuous polling loop
python3 code_factory.py --once             # single pass, then exit
python3 code_factory.py --repo owner/repo  # target a specific repo
```

The `/git-contribute` Claude Code skill becomes a thin SKILL.md that invokes `python3 code_factory.py --once`.

### File Layout

```
code_factory.py
prompts/
  phase1_claim_and_plan.md
  phase2_process_feedback.md
  phase4_implement.md
  phase6_process_review.md
skills/
  git-contribute/
    SKILL.md              # slim wrapper invoking code_factory.py
    TROUBLESHOOTING.md    # unchanged
```

Deleted:
- `check-actionable-issues/check_actionable.py` (absorbed)
- `check-actionable-issues/run_loop.sh` (absorbed)

### Components

All components live in `code_factory.py` (~400-500 lines):

**1. `gh()` / `gh_json()` — GitHub CLI wrapper**
Ported from `check_actionable.py`. Runs `gh` commands with rate limit retries (4 attempts, exponential backoff: 15s, 30s, 60s).

**2. `git()` — Git CLI wrapper**
Runs git commands, raises `RuntimeError` on failure.

**3. Router — `route(repo) -> (phase_name, context) | None`**
Queries GitHub in priority order, returns the first actionable item:

| Priority | Query | Phase |
|----------|-------|-------|
| 1 | Own PRs with `bot:review-requested` + review newer than last commit | `phase6_process_review` |
| 2 | Own draft PRs with `bot:plan-proposed` + human comments | `phase2_process_feedback` |
| 3 | Own PRs with `bot:plan-accepted` | `phase4_implement` |
| 4 | Unassigned open issues with no linked open PRs (prefer `good-first-issue`, `help-wanted` labels) | `phase1_claim_and_plan` |

PRs labeled `bot:in-progress` are excluded from all PR queries. For Priority 4 (unclaimed issues), self-assignment acts as the concurrency guard — issues already assigned to `@me` are skipped.

Priority 4 uses `gh issue list` with assignee filtering in Python (matching `check_actionable.py`), not `gh search issues` (which SKILL.md used). It uses `--state open` when checking for linked PRs (not `--state all`). This is an intentional change from the original SKILL.md, which blocked pickup if *any* PR (including closed ones) linked to the issue. The fix in commit `66638d8` established that closed PRs should not block re-claiming.

If `--repo` is omitted, the repo is derived from the current directory via `gh repo view --json nameWithOwner -q .nameWithOwner`.

**4. Phase functions**
Each phase function has the signature `fn(**ctx) -> (phase_name, ctx) | None`. Returning a tuple chains to the next phase within the same invocation (keeping `bot:in-progress` on). Returning `None` exits to wait for human input.

**5. Claude invocation — two modes**

- `claude(prompt)` — for reasoning tasks (plan generation, feedback classification). Runs `claude -p "..." --print`. Output-only, no tool access. Used in Phases 1, 2, and 6 (classification only).
- `claude_interactive(prompt, workdir)` — for implementation and code changes. Runs `claude --dangerously-skip-permissions -p "..." --print` with `subprocess.run(cwd=workdir)` to set the working directory. Full tool access. Used in Phase 4 (implementation) and Phase 6 (applying review fixes).

For classification phases (2 and 6), `claude()` output is parsed as JSON. If the output is malformed (not valid JSON or missing required fields), the phase logs the error, removes `bot:in-progress`, and returns `None` — the next loop iteration will retry.

For Phase 4, the single `claude_interactive()` call replaces the six sub-skill invocations from SKILL.md (worktrees, executing-plans, TDD, debugging, verification, push). The prompt template carries all necessary behavioral instructions for the full implementation cycle. Claude handles the sub-task orchestration internally.

**6. Prompt template loading — `load_prompt(phase, **kwargs)`**
Reads `prompts/{phase}.md`, interpolates variables with `str.format()`. Templates use `{{` / `}}` for literal braces.

### Repo Bootstrap

Before any phase runs, the orchestrator ensures the repo is available locally:

1. If `--repo` is provided and we're not already in that repo, clone it: `gh repo clone {owner/repo}`
2. Sync the default branch: `git checkout {default_branch} && git pull --ff-only`

The `repo` string is threaded through all functions as a parameter (not a global).

### Phase Consolidation

SKILL.md defines 6 phases. The orchestrator consolidates to 5 phase functions:

- **Phase 3 (Revise Plan) is absorbed into Phase 2.** In SKILL.md, Phase 3 was a separate step for incorporating feedback into the plan. In the orchestrator, the Phase 2 classifier prompt handles both classification and revision in a single Claude call. The `revise_minor` action (reviewer pre-approved with small changes) and `revise_major` action (substantial rethink) cover what Phase 3 did.

### Phase Details

**Phase 1: Claim & Plan** (LLM-dependent)
1. Self-assign the issue
2. Ensure bot labels exist on the repo
3. Gather context: issue body, repo conventions (CONTRIBUTING.md, CLAUDE.md, etc.), recent merged PRs
4. Create branch `bot/{num}-{slug}` with empty commit, push
5. Call `claude()` with context to generate implementation plan
6. Create draft PR with plan as body, label `bot:plan-proposed`, include `Closes #{num}`
7. Return `None` — wait for feedback

**Phase 2: Process Feedback** (LLM-dependent for classification)
1. Add `bot:in-progress`
2. Read all PR comments
3. Call `claude()` to classify feedback — returns JSON with action: `approve`, `revise_minor`, `revise_major`, `clarify`, `noop`
4. Route based on action:
   - `approve` → swap label to `bot:plan-accepted`, chain to Phase 4
   - `revise_minor` with pre-approval → update PR body, swap label, chain to Phase 4
   - `revise_major` → update PR body, post comment, remove `bot:in-progress`, return `None`
   - `clarify` → post answer as comment, remove `bot:in-progress`, return `None`
   - `noop` → remove `bot:in-progress`, return `None`

**Phase 4: Implement** (heavy LLM work)
1. Add `bot:in-progress` (if not already on from Phase 2 chain)
2. Read plan from PR body
3. Get PR branch name
4. Call `claude_interactive()` with plan + instructions to implement using TDD, debug failures, verify, and push
5. Chain to Phase 5

**Phase 5: Post Implementation** (no LLM needed)
1. Verify push landed: check `changedFiles > 0` and commit SHA matches
2. If verification fails, raise error (retry on next loop iteration). SKILL.md had a post-`gh pr ready` rollback step; this is intentionally simplified to a pre-gate — if the push didn't land, we never call `gh pr ready` in the first place.
3. Convert PR from draft: `gh pr ready`
4. Swap label to `bot:review-requested`
5. Remove `bot:in-progress`
6. Return `None` — wait for code review

**Phase 6: Process Code Review** (LLM-dependent)
1. Add `bot:in-progress`
2. Read reviews and review threads
3. Call `claude()` to classify — returns action: `approved`, `changes_requested`, `design_objection`
4. Route:
   - `approved` → merge with `--squash --delete-branch`
   - `changes_requested` → call `claude_interactive()` to apply fixes, push, re-request review, remove `bot:in-progress`, return `None`. Label stays as `bot:review-requested`.
   - `design_objection` → revise plan, relabel `bot:plan-proposed`, remove `bot:in-progress`, return `None`

### Prompt Templates

Each template receives specific variables and (for classification phases) must produce structured output.

**`phase1_claim_and_plan.md`**
- Variables: `{issue_number}`, `{issue_title}`, `{issue_body}`, `{conventions}`, `{recent_prs}`
- Output: Markdown plan body (free-form). Must include `Closes #{issue_number}`.
- Content: Instructions to read the issue, understand the codebase context, and produce a structured plan with sections: Problem, Approach, Planned Changes, Test Strategy, Risks/Open Questions.

**`phase2_process_feedback.md`**
- Variables: `{pr_number}`, `{pr_title}`, `{plan_body}`, `{comments}`
- Output: JSON object:
  ```json
  {{
    "action": "approve | revise_minor | revise_major | clarify | noop",
    "summary": "brief explanation of the classification",
    "revised_plan": "updated plan markdown (only for revise_minor/revise_major)",
    "comment": "reply to post on the PR (only for clarify/revise_major)"
  }}
  ```
- Content: Instructions to classify human feedback against the plan. Maps to SKILL.md's Phase 2 signal table: explicit approval → `approve`, approval with conditions → `revise_minor` (implies pre-approval), rejection/major rethink → `revise_major`, questions → `clarify`, no actionable feedback → `noop`.

**`phase4_implement.md`**
- Variables: `{pr_number}`, `{plan}`, `{repo}`, `{branch}`
- Output: Free-form (Claude works interactively — writes code, runs tests, commits, pushes).
- Content: The full behavioral instructions for implementation. This replaces the six sub-skill invocations from SKILL.md Phase 4. Claude has full tool access and reads repo conventions (CONTRIBUTING.md, CLAUDE.md, etc.) itself during the session. The prompt must instruct Claude to:
  1. Check out the PR branch and sync
  2. Read repo conventions (CONTRIBUTING.md, CLAUDE.md, etc.)
  3. Write tests first (TDD), then implement
  4. Debug any test failures
  5. Run full verification before declaring done
  6. Commit and push all changes to the PR branch

**`phase6_process_review.md`**
- Variables: `{pr_number}`, `{pr_title}`, `{reviews}`, `{review_threads}`
- Output: JSON object:
  ```json
  {{
    "action": "approved | changes_requested | design_objection",
    "summary": "brief explanation"
  }}
  ```
- Content: Instructions to classify code review feedback. Only formal GitHub review approvals count as `approved` — not comment-only approval.

### Main Loop

```python
while True:
    result = route(repo)
    if result:
        try:
            while result:
                phase_name, ctx = result
                result = PHASES[phase_name](**ctx)
        except Exception as e:
            log error, clean up bot:in-progress label
        sleep(5) if not --once
    else:
        sleep(300) if not --once
    if --once: break
```

### Error Handling

- Phase failures log the error (to stderr with timestamps) and clean up `bot:in-progress` so work isn't permanently stuck
- The next loop iteration picks the work back up
- `gh()` retries rate-limited requests with exponential backoff
- Phase 5 raises on push verification failure rather than silently proceeding
- Malformed JSON from classification prompts (Phases 2 and 6) is treated as a phase failure — log, clean up label, retry next iteration

### State Management

GitHub labels are the sole source of truth. No local state files. The label scheme is unchanged:

- `bot:plan-proposed` — draft PR awaiting plan review
- `bot:plan-accepted` — plan approved, ready for implementation
- `bot:in-progress` — PR currently being processed (prevents concurrent pickup)
- `bot:review-requested` — implementation complete, awaiting code review

### Migration

1. Implement `code_factory.py` and `prompts/`
2. Slim down `skills/git-contribute/SKILL.md` to a thin wrapper
3. Update `README.md` for new usage
4. Delete `check-actionable-issues/` directory
5. Fix TROUBLESHOOTING.md label creation recipe to include `bot:in-progress` (pre-existing bug)

### Concurrency

Only one instance of `code_factory.py` should run per repo at a time. The `bot:in-progress` label prevents concurrent processing of the same PR, but Phase 1 (claiming an issue) has a window between self-assignment and PR creation where no label exists. Self-assignment (`--add-assignee @me`) acts as the lock for Phase 1 — the router skips issues already assigned to `@me`.

### What's NOT Changing

- The lifecycle: claim → plan → review → implement → verify → merge
- The label scheme
- The `gh` CLI patterns
- TROUBLESHOOTING.md
- The principle of human oversight before implementation

### Dependencies

Runtime: Python 3, `gh` CLI (authenticated), `claude` CLI, `git`. No pip packages — standard library only.
