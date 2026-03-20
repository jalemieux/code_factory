You are applying code review fixes to a pull request.

## PR #{pr_number}: {pr_title}

## Branch: {branch}

## Review Feedback

{reviews}

## Your Task

Apply the requested changes from the code review above. You have full access to the codebase and all tools.

For each review comment or thread:
1. Read the comment and understand what change is requested
2. Apply the fix
3. If you disagree with a suggestion, leave a reply explaining your reasoning instead of changing the code

After applying all fixes:
1. Run the test suite to ensure nothing is broken
2. Commit with a clear message referencing the review (e.g., "fix: address review feedback on PR #{pr_number}")
3. Push to the branch:
   ```
   git push origin {branch}
   ```

Important:
- Do not force-push — it destroys review context
- Keep changes focused on what was requested — do not refactor or "improve" unrelated code
- Every commit should leave the repo in a working state
