<p align="center">
  <img src="code_factory.png" alt="Code Factory" width="100%">
</p>

# Code Factory

Automation that turns GitHub issues into merged PRs with human oversight, powered by [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

## The Problem

Most of the time, you already know what needs to change — the bug, the feature, the refactor. What you don't have is the hour to sit at a terminal and shepherd an AI through it. Code Factory moves the entire interaction to GitHub: describe the work in an issue, review an AI-generated plan on a draft PR, leave comments, and merge when you're satisfied. GitHub becomes the interface — no terminal session required.

Issues in, merged PRs out, with you in the driver's seat the entire time.

## Quick Start

```bash
# Continuous polling (runs until stopped — best in a tmux/screen session)
tmux new -s factory
python3 code_factory.py --repo owner/repo

# Single pass (process one item and exit)
python3 code_factory.py --once --repo owner/repo

# Auto-detect repo from current directory
cd /path/to/your-repo
python3 /path/to/code_factory/code_factory.py --once
```

## How It Works

A single Python script checks a GitHub repo for actionable work, then orchestrates Claude Code to handle it. Every change goes through a **plan-first workflow** — Claude proposes a plan as a draft PR, waits for human review, and only implements after approval.

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

## Usage

```bash
# Continuous loop — polls every 5 minutes, dispatches work automatically
python3 code_factory.py --repo owner/repo

# Single pass — find one item, process it, exit
python3 code_factory.py --once --repo owner/repo

# Auto-detect repo from current directory
python3 code_factory.py --once
```

## Project Structure

```
code_factory.py           # Orchestrator: poll, route, manage phases, invoke Claude Code
prompts/                  # Prompt templates for LLM-dependent phases
  phase1_claim_and_plan.md
  phase2_process_feedback.md
  phase4_implement.md
  phase6_process_review.md
  phase6_apply_fixes.md
skills/
  git-contribute/
    SKILL.md              # Claude Code skill for interactive invocation
    TROUBLESHOOTING.md    # Diagnostics and manual fix recipes
docs/                     # Design specs and implementation plans
tests/
  test_code_factory.py    # Unit tests
```

## Labels

The workflow uses these GitHub labels (created automatically on first run):

- `bot:plan-proposed` — draft PR with a plan awaiting human review
- `bot:plan-accepted` — plan approved, ready for implementation
- `bot:in-progress` — PR currently being processed
- `bot:review-requested` — implementation complete, awaiting code review
