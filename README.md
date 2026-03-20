# Code Factory

Automation that turns GitHub issues into merged PRs with human oversight, powered by [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and the `git-contribute` skill.

## Quick Start

```bash
cd /path/to/your-repo
/path/to/code_factory/check-actionable-issues/run_loop.sh
```

That's it. The loop will poll for open issues, propose plans as draft PRs, and implement after you approve.

## How It Works

A polling loop checks a GitHub repo for actionable work, then dispatches Claude Code to handle it. Every change goes through a **plan-first workflow** — Claude proposes a plan as a draft PR, waits for human review, and only implements after approval.

```
┌─────────────┐     ┌──────────────────┐     ┌────────────────┐
│  run_loop.sh │────▶│ check_actionable │────▶│ git-contribute │
│  (poll every │     │  (find work via  │     │  (claim, plan, │
│   5 minutes) │     │   gh CLI)        │     │  implement, PR)│
└─────────────┘     └──────────────────┘     └────────────────┘
```

### Lifecycle

1. **Claim** — picks an unassigned issue and self-assigns
2. **Plan** — creates a draft PR with an implementation plan
3. **Review** — waits for human feedback on the plan
4. **Implement** — writes the code using TDD after plan approval
5. **Verify** — runs tests and CI checks
6. **Merge** — after human code review approval

### Priority Order

Existing work is always finished before new work is started:

| Priority | What | Action |
|----------|------|--------|
| 1 | PRs with code review feedback | Address reviewer comments |
| 2 | PRs with plan feedback | Incorporate feedback or proceed |
| 3 | Accepted plans | Implement the approved plan |
| 4 | Unclaimed issues | Claim and propose a plan |

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed
- [`gh` CLI](https://cli.github.com/) authenticated with repo permissions
- Python 3
- The `git-contribute` skill installed in Claude Code (see `skills/git-contribute/SKILL.md`)

## Usage

### Check for actionable work (one-shot)

```bash
# Current repo (run from inside a cloned repo)
python3 check-actionable-issues/check_actionable.py

# Specific repo
python3 check-actionable-issues/check_actionable.py owner/repo
```

Exit code `0` means work was found, `1` means nothing actionable.

### Run the continuous loop

```bash
# Current repo
./check-actionable-issues/run_loop.sh

# Specific repo
./check-actionable-issues/run_loop.sh owner/repo
```

This will:
- Poll every **5 minutes** for actionable work
- When work is found, launch `claude --dangerously-skip-permissions` to run the `git-contribute` skill
- Sleep **5 seconds** between dispatches when work exists, **5 minutes** when idle

### Run git-contribute directly

```bash
claude /git-contribute
```

## Project Structure

```
check-actionable-issues/
  check_actionable.py   # Queries GitHub for actionable issues/PRs
  run_loop.sh           # Polling loop that dispatches git-contribute

skills/
  git-contribute/
    SKILL.md            # Full skill definition (claim → plan → implement → merge)
```

## Labels

The workflow uses these GitHub labels (created automatically on first run):

- `bot:plan-proposed` — draft PR with a plan awaiting human review
- `bot:plan-accepted` — plan approved, ready for implementation
- `bot:review-requested` — implementation complete, awaiting code review
