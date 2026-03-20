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
| 4 | Unassigned open issues with no linked open PRs | `phase1_claim_and_plan` |

PRs labeled `bot:in-progress` are excluded from all queries.

**4. Phase functions**
Each phase function has the signature `fn(**ctx) -> (phase_name, ctx) | None`. Returning a tuple chains to the next phase within the same invocation (keeping `bot:in-progress` on). Returning `None` exits to wait for human input.

**5. Claude invocation — two modes**

- `claude(prompt)` — for reasoning tasks (plan generation, feedback classification). Runs `claude -p "..." --print`. Output-only, no tool access.
- `claude_interactive(prompt, workdir)` — for Phase 4 implementation. Runs `claude --dangerously-skip-permissions -p "..." --print` in the repo directory. Full tool access.

**6. Prompt template loading — `load_prompt(phase, **kwargs)`**
Reads `prompts/{phase}.md`, interpolates variables with `str.format()`. Templates use `{{` / `}}` for literal braces.

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
2. If verification fails, raise error (retry on next loop iteration)
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
   - `changes_requested` → call `claude_interactive()` to apply fixes, push, re-request review, remove `bot:in-progress`, return `None`
   - `design_objection` → revise plan, relabel `bot:plan-proposed`, remove `bot:in-progress`, return `None`

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

- Phase failures log the error and clean up `bot:in-progress` so work isn't permanently stuck
- The next loop iteration picks the work back up
- `gh()` retries rate-limited requests with exponential backoff
- Phase 5 raises on push verification failure rather than silently proceeding

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
5. No changes to TROUBLESHOOTING.md — same state machine, same labels

### What's NOT Changing

- The lifecycle: claim → plan → review → implement → verify → merge
- The label scheme
- The `gh` CLI patterns
- TROUBLESHOOTING.md
- The principle of human oversight before implementation

### Dependencies

Runtime: Python 3, `gh` CLI (authenticated), `claude` CLI, `git`. No pip packages — standard library only.
