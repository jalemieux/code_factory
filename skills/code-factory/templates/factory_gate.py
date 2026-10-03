#!/usr/bin/env python3
"""factory-gate: classify a pull request into a risk tier.

Reads `.codefactory.yml` from the checked-out BASE branch (the workflow
never checks out the PR head), lists the PR's changed files through the
API, and writes the tier to the job summary and to the `tier` output.

Tiers: A (critical path, always human-reviewed), B (human-merged until
evals exist), C (everything else). PLAN is the sentinel for an empty diff
and fails the check: a PR with no changes must not be mergeable.

The classification rules are vendored from jalemieux/code_factory
(spine.classify) so this repo's CI has no dependency on the factory.
Keep the two in step; the factory's tests pin the same cases.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from fnmatch import fnmatchcase

CONFIG_PATH = ".codefactory.yml"
TIERS = ("A", "B", "C")
PLAN = "PLAN"


class ConfigError(RuntimeError):
    pass


def load_tiers(text: str) -> dict:
    """Parse the `tiers:` mapping. Only A and B take globs; keys are
    case-insensitive; a single string is a one-item list."""
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{CONFIG_PATH}: invalid YAML: {e}") from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_PATH}: top level must be a mapping")
    tiers = data.get("tiers") or {}
    if not isinstance(tiers, dict):
        raise ConfigError(f"{CONFIG_PATH}: `tiers` must be a mapping of tier -> list of globs")
    out = {"A": (), "B": ()}
    for tier, globs in tiers.items():
        tier = str(tier).upper()
        if tier not in ("A", "B"):
            raise ConfigError(f"{CONFIG_PATH}: unknown tier {tier!r} (only A and B take globs; the rest is C)")
        if globs is None:
            globs = []
        if isinstance(globs, str):
            globs = [globs]
        if not isinstance(globs, list) or not all(isinstance(g, str) and g for g in globs):
            raise ConfigError(f"{CONFIG_PATH}: tiers.{tier} must be a list of non-empty glob strings")
        out[tier] = tuple(globs)
    return out


def is_security_flagged(title, labels) -> bool:
    if title and "[security]" in title.lower():
        return True
    return any("security" in str(name).lower() for name in labels or ())


def tier_of(path: str, globs: dict) -> str:
    if any(fnmatchcase(path, g) for g in globs.get("A", ())):
        return "A"
    if any(fnmatchcase(path, g) for g in globs.get("B", ())):
        return "B"
    return "C"


def classify(changed_files, globs, title=None, labels=None) -> str:
    """Strictest tier of any touched file. Security flag forces A; an
    empty diff is PLAN, never a tier."""
    if is_security_flagged(title, labels):
        return "A"
    if not changed_files:
        return PLAN
    return min((tier_of(p, globs) for p in changed_files), key=TIERS.index)


def changed_files(repo: str, pr: int) -> list:
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr}/files", "--paginate", "--jq", ".[].filename"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def render_summary(tier: str, files, globs: dict, title, labels) -> str:
    lines = [f"## factory-gate: tier **{tier}**", ""]
    if tier == PLAN:
        lines.append("Empty diff: this PR is a plan draft, not mergeable code.")
    elif is_security_flagged(title, labels):
        lines.append("Security-flagged title or label: tier A regardless of paths.")
    else:
        lines += ["| file | tier |", "|---|---|"]
        for p in files:
            lines.append(f"| `{p}` | {tier_of(p, globs)} |")
    lines += ["", f"Rules: `{CONFIG_PATH}` on the base branch. A = human review, never auto-merged. "
                  "B = human-merged. C = everything else."]
    return "\n".join(lines) + "\n"


def main() -> int:
    repo = os.environ["REPO"]
    pr = int(os.environ["PR_NUMBER"])
    title = os.environ.get("PR_TITLE", "")
    labels = json.loads(os.environ.get("PR_LABELS") or "[]")

    if not os.path.exists(CONFIG_PATH):
        print(f"::error::{CONFIG_PATH} is missing on the base branch; the gate cannot classify.")
        return 1
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            globs = load_tiers(fh.read())
    except ConfigError as e:
        print(f"::error::{e}")
        return 1

    files = changed_files(repo, pr)
    tier = classify(files, globs, title, labels)
    summary = render_summary(tier, files, globs, title, labels)

    print(summary)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(summary)
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"tier={tier}\n")

    if tier == PLAN:
        print("::error::Empty diff. Push the implementation before this PR can merge.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
