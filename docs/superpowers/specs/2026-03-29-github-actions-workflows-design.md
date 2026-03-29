# GitHub Actions Workflows for Code Factory

**Date:** 2026-03-29
**Issue:** jalemieux/code_factory#1 — Add intelligent PR triage to replace rigid label-based routing

## Problem

The current `route()` function in `code_factory.py` uses a rigid checklist of label + state queries to find actionable work. When a PR doesn't exactly match one of the expected states, the script silently reports "No actionable work found" and sleeps. Rather than adding more rigid edge-case handling, we replace the deterministic routing model with event-driven GitHub Actions workflows where Claude Code assesses each situation using judgment.

## Solution

Two GitHub Actions workflows that replace the polling model with a push-based, event-driven architecture. The existing `code_factory.py` script remains untouched.

### Core Workflow

1. Human creates a GitHub issue
2. Bot creates a draft PR with an implementation plan
3. Human comments on the PR with feedback or approval
4. If feedback: bot revises the plan, waits for more input
5. If approved: bot implements the plan, pushes code to the PR
6. Human reviews and merges at their discretion

The PR is the single conversation thread. All interaction happens there.

## Architecture

### Workflow 1: `plan.yml`

**Triggers:**
- `issues` — type `opened`
- `issue_comment` — type `created`

**Permissions:**
- `contents: write`
- `pull-requests: write`
- `issues: write`
- `id-token: write`

**On issue opened:**
Claude Code Action runs in automation mode with a prompt to:
1. Read the issue title and body
2. Read repo conventions (CONTRIBUTING.md, CLAUDE.md, etc.)
3. Create a branch (`bot/{issue_number}-{slug}`)
4. Open a draft PR with the implementation plan as the body
5. Link the PR to the issue with `Closes #{issue_number}`

**On issue comment (PR context):**
Filtered to only run when:
- The comment is on a pull request
- The PR was authored by the bot
- The commenter is not a bot

Claude Code Action runs in automation mode with a prompt to:
1. Read the PR body (current plan) and all comments
2. Assess the latest comment: approval, feedback, or noise
3. If feedback: revise the plan in the PR body, reply acknowledging changes
4. If approval: add `bot:plan-accepted` label using a PAT token (to bypass GitHub's anti-loop protection and trigger the implement workflow)
5. If noise: do nothing

### Workflow 2: `implement.yml`

**Trigger:**
- `pull_request` — type `labeled`, filtered to `bot:plan-accepted`

**Permissions:**
- `contents: write`
- `pull-requests: write`
- `id-token: write`

**On label applied:**
Claude Code Action runs in automation mode with Bash tools enabled. Prompt instructs it to:
1. Read the approved plan from the PR body
2. Check out the PR branch
3. Implement the plan: write tests first, then code, run the test suite
4. Commit and push to the PR branch
5. Mark the PR as ready for review (no longer draft)

Bot's job ends here. Human reviews and merges.

## Authentication & Secrets

| Secret | Purpose |
|--------|---------|
| `ANTHROPIC_API_KEY` | Claude Code API access |
| `PAT_TOKEN` | Personal access token with `contents: write` and `pull_requests: write` — used by `plan.yml` to add labels that trigger `implement.yml` (bypasses GitHub's anti-loop protection for `github-actions` user) |

Alternative to PAT: a GitHub App installation token works the same way.

## Prompt Strategy

Each workflow passes a focused prompt inline in the YAML. Claude Code Action handles context gathering natively (reading files, checking repo state), so prompts are concise:

- **Plan creation:** Read the issue, read repo conventions, create a draft PR with a structured plan (Problem, Approach, Planned Changes, Test Strategy)
- **Feedback assessment:** Read the PR plan and comments, classify the latest comment (approval/feedback/noise), take the appropriate action
- **Implementation:** Read the approved plan, implement it with TDD, commit, push, mark PR ready

## What This Does NOT Include

- No changes to the existing `code_factory.py` script
- No code review feedback loop (human handles post-implementation review)
- No polling or sleep logic
- No Python orchestrator code
- No custom label management code (Claude uses built-in GitHub MCP tools)

## Files to Create

```
.github/workflows/plan.yml
.github/workflows/implement.yml
```

## Success Criteria (from issue #1)

- Script never silently skips work that a human would recognize as actionable — replaced by event-driven triggers, no polling to miss events
- Triage uses Claude's judgment — Claude assesses each event and decides what to do, no rigid label matching
- Actions are visible — all interaction happens in the PR thread, logged as comments and commits
