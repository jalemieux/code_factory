# Onboarding a repo to Code Factory

Goal: `cf doctor owner/repo` shows no FAIL and no warn. Steps 1 to 3 are
ordinary PRs on the target repo. Step 4 changes repo settings: show the
commands to the repo owner and get an explicit yes before running them.

## 1. Access

The bot account (the `GH_TOKEN` in `.env`) needs write access: Contents,
Pull requests, Issues. Add it as a collaborator. Issues and comments only
count when they come from collaborators, so the humans who file and approve
work must be collaborators too.

## 2. Declare risk tiers

Add `.codefactory.yml` at the repo root, starting from
`templates/codefactory.yml` in this skill. To choose globs, read the repo:

- Tier A: entrypoints, auth, anything that handles untrusted input or
  network transport, core loops, migrations, CI and deploy config.
- Tier B: plugins, extensions, content that changes behaviour but is
  isolated.
- Leave docs, tests and tooling as C.

fnmatch syntax: `*` crosses slashes, so `dir/**` and `dir/*` both match
nested paths. When unsure, choose A. Open it as a PR; it takes effect only
once merged to the default branch.

## 3. Install the gate check

Copy into the target repo and open a PR:

```
templates/factory-gate.yml  ->  .github/workflows/factory-gate.yml
templates/factory_gate.py   ->  .github/scripts/factory_gate.py
```

The workflow runs on `pull_request_target` and takes the workflow, script
and tier rules from the base branch, so a PR cannot edit the gate that
judges it. Consequence: the PR that adds the gate does not run it; the
next PR does. It fails on an empty diff and on a missing or malformed
`.codefactory.yml`.

The repo also needs a test workflow whose job is named `tests`, with no
path filters (a required check that never reports blocks every merge).

## 4. Settings (repo owner decides)

Read the current protection first and preserve what is there; the PUT
below replaces the whole rule. Adjust the review block to match.

```bash
gh api repos/OWNER/REPO/branches/main/protection   # read first

gh api -X PUT repos/OWNER/REPO/branches/main/protection --input - <<'JSON'
{"required_status_checks":{"strict":false,"checks":[{"context":"tests"},{"context":"factory-gate"}]},
 "enforce_admins":false,
 "required_pull_request_reviews":{"required_approving_review_count":1},
 "restrictions":null}
JSON

gh api -X PATCH repos/OWNER/REPO -F allow_auto_merge=true
```

With one required approval, a tier C PR still waits for a human to approve
before GitHub merges it. Without any required review, tier C merges on
green checks alone. Make sure the owner knows which one they are choosing.

## 5. Verify

```bash
cf doctor owner/repo
cf queue owner/repo
```

Then run one small tier C issue end to end before trusting it with more.
