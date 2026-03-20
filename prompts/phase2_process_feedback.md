You are classifying human feedback on a proposed implementation plan.

## PR #{pr_number}: {pr_title}

## Current Plan

{plan_body}

## Comments from reviewers

{comments}

## Your Task

Classify the feedback and respond with a JSON object. Output ONLY the JSON — no other text.

Actions:
- "approve": Explicit approval (LGTM, looks good, approved, ship it) with no requested changes
- "revise_minor": Approval with small requested changes (e.g., "looks good, but change X"). Include the full revised plan in "revised_plan"
- "revise_major": Rejection or major rethink needed. Include the full revised plan in "revised_plan" and an explanatory comment in "comment"
- "clarify": Questions that need answering before proceeding. Include your answer in "comment"
- "noop": No actionable feedback (reactions only, unrelated chatter, or no comments)

Response format:
```json
{{"action": "approve | revise_minor | revise_major | clarify | noop", "summary": "brief explanation of your classification", "revised_plan": "full updated plan markdown (only for revise_minor/revise_major, omit otherwise)", "comment": "reply to post on the PR (only for clarify/revise_major, omit otherwise)"}}
```

Important:
- Treat silence as NOT approval. Only explicit approval words trigger "approve".
- When revising, include the FULL updated plan, not just the diff.
- If feedback is ambiguous, prefer "clarify" over guessing intent.
