<p align="center">
  <img src="code_factory.png" alt="Code Factory" width="100%">
</p>

# Code Factory

Automation that turns GitHub issues into merged PRs with human oversight, powered by [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

## The Problem

Most of the time, you already know what needs to change — the bug, the feature, the refactor. What you don't have is the hour to sit at a terminal and shepherd an AI through it. Code Factory moves the entire interaction to GitHub: describe the work in an issue, review an AI-generated plan on a draft PR, leave comments, and merge when you're satisfied. GitHub becomes the interface — no terminal session required.

Issues in, merged PRs out, with you in the driver's seat the entire time.

## Quick Start

1. Copy `.github/workflows/plan.yml` and `.github/workflows/implement.yml` into your repository
2. Add repository secrets ([see Setup](#setup))
3. Create an issue — the bot opens a draft PR with an implementation plan
4. Comment on the PR to give feedback or approve
5. On approval, the bot implements the plan and marks the PR ready for review
6. You review and merge

## How It Works

Two GitHub Actions workflows respond to events in your repo. No polling, no local process, no terminal.

### Lifecycle

1. **Issue opened** — Claude reads the issue, explores the codebase, and opens a draft PR with a structured implementation plan
2. **You review the plan** — leave a comment on the PR ([see below](#reviewing-plans))
3. **Feedback loop** — Claude classifies your comment: if it's feedback, the plan is revised; if it's approval, implementation begins
4. **Implementation** — Claude implements the plan using TDD, pushes code, and marks the PR ready for review
5. **You merge** — review the code and merge at your discretion

The PR is the single conversation thread. All interaction happens there.

### Reviewing Plans

When the bot creates a draft PR with a plan, **leave a comment on the PR** to provide feedback. Do **not** use GitHub's "Approve" review button — the bot reads PR comments, not review approvals.

| What you want | What to comment |
|---------------|-----------------|
| Approve the plan | `LGTM`, `looks good`, `ship it`, `approved` |
| Approve with tweaks | `Looks good, but also add tests for X` |
| Request major changes | `This approach won't work because X. Instead, try Y.` |
| Ask a question | `What about edge case X?` |

The bot will classify your comment and either start implementing, revise the plan, or reply with clarification.

### Reviewing Code

After implementation, the bot marks the PR ready for review. Use GitHub's normal code review workflow — leave review comments, request changes, or approve. You merge when satisfied.

## Setup

### 1. Add Repository Secrets

Go to **Settings > Secrets and variables > Actions** and add:

| Secret | Purpose |
|--------|---------|
| `ANTHROPIC_API_KEY` | Claude API access |
| `PAT_TOKEN` | Personal access token for triggering workflows (see below) |

### 2. Create the PAT Token

GitHub's built-in `GITHUB_TOKEN` prevents workflows from triggering other workflows (anti-loop protection). A PAT bypasses this so that approving a plan can trigger the implement workflow.

Create a [fine-grained personal access token](https://github.com/settings/tokens?type=beta) with these repository permissions:
- **Contents**: Read and write
- **Pull requests**: Read and write
- **Issues**: Read and write

Alternatively, use a GitHub App installation token with the same permissions.

### 3. Copy Workflow Files

```bash
# From your target repository
mkdir -p .github/workflows
cp path/to/code_factory/.github/workflows/plan.yml .github/workflows/
cp path/to/code_factory/.github/workflows/implement.yml .github/workflows/
git add .github/workflows
git commit -m "ci: add Code Factory workflows"
git push
```

### 4. Create Labels (optional)

Claude will create labels automatically if missing, but you can pre-create:

- `bot:plan-accepted` — triggers the implement workflow when a plan is approved

## Project Structure

```
.github/workflows/
  plan.yml                # Issue opened → draft PR with plan; PR comments → feedback loop
  implement.yml           # Plan approved → TDD implementation, push, mark ready
code_factory.py           # Legacy local script (polling-based alternative)
prompts/                  # Prompt templates (used by local script)
tests/
  test_code_factory.py    # Unit tests
```

## Legacy: Local Script

An older polling-based approach is also available via `code_factory.py`. It requires the Claude Code CLI, `gh` CLI, and Python 3 installed locally.

```bash
python3 code_factory.py --repo owner/repo       # Continuous polling
python3 code_factory.py --once --repo owner/repo # Single pass
```
