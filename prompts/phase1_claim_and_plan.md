You are proposing an implementation plan for a GitHub issue.

## Issue #{issue_number}: {issue_title}

{issue_body}

## Existing Comments on the Issue

These comments were left on the issue before you began planning. Treat them as
authoritative input from reviewers — they refine, constrain, or override the
original issue body. If a comment proposes a different approach, default to it
unless it conflicts with the issue's stated goal.

{issue_comments}

## Repository Conventions

{conventions}

## Recent Merged PRs (for style reference)

{recent_prs}

## Your Task

Produce a structured implementation plan in markdown. The plan will become the body of a draft PR for human review.

Your plan MUST include the line `Closes #{issue_number}` to link the PR to the issue.

Use this structure:

## Problem
What the issue asks for, in your own words.

## Approach
High-level strategy — what changes, why this approach.

## Planned Changes
- `file/path`: what changes and why
- ...

## Test Strategy
How the implementation will be verified.

## Risks / Open Questions
- Anything uncertain or worth flagging.

Keep the plan concise — bullet points, not essays. Reviewers skim.
