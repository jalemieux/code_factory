# Lessons Learned: Building Code Factory

Two days. One Python file. An autonomous agent that turns GitHub issues into merged PRs. Here's what I learned building it with Claude Code.

## The Build Order That Worked

**Plan → Skill → Test → Script → Test**

This wasn't the obvious path. The obvious path was "write a script." Instead:

1. **Plan** — Wrote a design spec before touching code. The spec forced decisions about what belongs in deterministic Python vs. what needs LLM reasoning. That single distinction — code for routing, LLM for judgment — shaped the entire architecture.

2. **Skill** — Built a Claude Code skill (`SKILL.md`) first. The skill was the working prototype: a prompt that Claude Code could execute interactively. This proved the workflow end-to-end before any orchestration code existed. It also became the living spec — when the Python orchestrator disagreed with the skill, the skill was right.

3. **Test** — Deployed the skill against a real repo. Every bug found here was a bug the orchestrator wouldn't have to rediscover. Rate limits, label ordering, push verification — all surfaced during skill testing.

4. **Script** — Only then wrote `code_factory.py`. By this point, the phases were proven, the edge cases were catalogued, and the implementation plan wrote itself (literally — Claude generated a 1,483-line plan from the spec).

5. **Test** — Unit tests for every deterministic function. The LLM-dependent phases are tested at integration boundaries (correct prompts assembled, correct CLI calls made), not by mocking Claude's judgment.

## Insights from the Git Log

### Every "silent success" API is lying to you

`gh pr create --label bot:plan-proposed` returns success even when the label doesn't exist. The PR gets created, the label silently doesn't. Three commits and a production incident before we learned: **create labels before the PR, not after** (`b9ae7a9`).

The same pattern appeared with `gh issue list --assignee ""` — it doesn't filter for unassigned issues, it just ignores the flag. We had to fetch assignees in JSON and filter ourselves (`9051c5d`).

Lesson: never trust a CLI flag you haven't verified with your own eyes. The happy path lies.

### Concurrency bugs arrive immediately

The moment you have a polling loop, you have concurrency. The bot would pick up the same PR twice in consecutive polls because processing took longer than the poll interval. The fix was a `bot:in-progress` label as a distributed lock — simple, visible, and debuggable from the GitHub UI (`6e6f90c`).

### Rate limits are the first production bug

The GitHub Search API has aggressive secondary rate limits that don't show up in documentation. Our polling loop hit 403s within minutes. Fix: replace `gh search issues` with `gh issue list` and add exponential backoff with jitter (`8105724`). The lesson isn't "add retry logic" — it's "use the least powerful API that gets the job done."

### Push verification is non-negotiable

The worst bug: the bot marked a PR "ready for review" with zero changed files. Git worktree cleanup had destroyed unpushed commits. The fix required a hard verification gate — check `changedFiles > 0` and refuse to proceed otherwise (`b9eff40`, `b320dd6`). Never trust that a side effect happened. Verify.

### Sharing accounts reveals hidden assumptions

When the bot and human use the same GitHub account, the author filter on PR comments excludes the human's feedback. The fix was simple (any comment on a `bot:plan-proposed` PR is feedback, since the bot writes plans in the PR body, not as comments), but the assumption was invisible until it broke (`8c74d13`).

## Architecture Decisions That Paid Off

### Deterministic code + LLM for judgment

The router, label management, git operations, and phase transitions are all plain Python. Claude is only called for tasks that genuinely require judgment: writing plans, classifying feedback, deciding how to address review comments. This means the orchestrator is testable, debuggable, and predictable. When something goes wrong, `git log` tells you exactly which phase failed and why.

### Phase chaining with early exit

Each phase returns either `(next_phase, context)` to chain or `None` to stop. This made the "approve feedback → implement immediately" flow trivial while keeping each phase independently testable. The main loop is seven lines.

### Labels as state machine

GitHub labels are the entire state machine: `plan-proposed → plan-accepted → in-progress → review-requested`. No database, no external state. Anyone can look at a PR's labels and know exactly where it is in the pipeline. When something gets stuck, a human can fix it by moving a label.

### Prompts as templates

Prompt templates live in `prompts/*.md`, not inline in Python. This means you can iterate on prompts without touching orchestration logic, and the templates are readable on their own. `str.format()` over f-strings — simpler and no risk of accidental variable capture.

## What I'd Do Differently

### Start with the troubleshooting guide

We wrote `TROUBLESHOOTING.md` after the sixth bug. Should have written it after the first. The act of documenting "what can go wrong" forces you to think about failure modes before they bite. The troubleshooting guide became the most useful file in the repo.

### Smaller blast radius for first deployment

Testing against a repo you care about on the first run is stressful. A throwaway test repo with synthetic issues would have let us iterate faster on the early bugs (label ordering, rate limits, assignee filtering) without the anxiety.

## The Meta-Lesson

The skill-first approach worked because it separated two problems: "does this workflow make sense?" and "can I automate it reliably?" The skill answered the first question. The script answered the second. Trying to answer both at once — the natural instinct — would have been slower and buggier.

Plan the work. Prove the concept. Then automate it.
