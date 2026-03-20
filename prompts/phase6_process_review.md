You are classifying code review feedback on a pull request.

## PR #{pr_number}: {pr_title}

## Reviews and Threads

{reviews}

## Your Task

Classify the review feedback and respond with a JSON object. Output ONLY the JSON — no other text.

Actions:
- "approved": The PR has been formally approved via GitHub's review system (not just a comment saying "looks good")
- "changes_requested": Specific fixes or changes were requested. The review may include inline comments on specific lines
- "design_objection": Fundamental disagreement with the approach — the plan itself needs rethinking, not just the implementation

Response format:
```json
{{"action": "approved | changes_requested | design_objection", "summary": "brief explanation of the review state"}}
```

Important:
- Only "approved" if there is a formal GitHub review approval (state: APPROVED), not just a comment
- If multiple reviewers have reviewed, go with the most recent review state
- If there are unresolved review threads, prefer "changes_requested" even if the overall review is approved
