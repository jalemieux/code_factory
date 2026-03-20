You are implementing a plan that has been reviewed and approved by a human.

## PR #{pr_number} in {repo}

## Branch: {branch}

## Approved Plan

{plan}

## Your Task

Implement the plan above. You have full access to the codebase and all tools.

Follow this process:

1. **Read conventions first** — Check for CONTRIBUTING.md, CLAUDE.md, AGENTS.md, or CODING_GUIDELINES.md in the repo root. Follow their instructions.

2. **Write tests first (TDD)** — For each component in the plan:
   - Write the failing test
   - Run it to confirm it fails
   - Write the minimal implementation to make it pass
   - Run it to confirm it passes

3. **Debug failures** — If any test fails unexpectedly:
   - Read the error message carefully
   - Check your assumptions
   - Fix the root cause, don't patch symptoms

4. **Verify before declaring done** — Run the full test suite. Check that all tests pass. If the repo has a linter or formatter, run those too.

5. **Commit and push** — Make focused commits with clear messages. Push all commits to the branch:
   ```
   git push origin {branch}
   ```

Important:
- Follow the repo's existing patterns and test framework
- Do not introduce new dependencies unless the plan explicitly calls for them
- Do not force-push — it destroys review context
- Every commit should leave the repo in a working state
