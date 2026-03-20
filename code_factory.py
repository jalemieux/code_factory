#!/usr/bin/env python3
"""Code Factory — autonomous GitHub contribution orchestrator."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} — {msg}", file=sys.stderr)


def gh(*args: str) -> str:
    """Run a gh CLI command and return stdout, retrying on rate limits."""
    for attempt in range(4):
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
        if "rate limit" in result.stderr.lower() and attempt < 3:
            wait = 2 ** attempt * 15
            log(f"Rate limited, retrying in {wait}s...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return ""


def gh_json(*args: str) -> list | dict:
    return json.loads(gh(*args) or "[]")


def git(*args: str) -> str:
    """Run a git command, raise on failure."""
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]


def ensure_labels(repo: str) -> None:
    for label in (
        "bot:plan-proposed",
        "bot:plan-accepted",
        "bot:in-progress",
        "bot:review-requested",
    ):
        gh(
            "label", "create", label,
            "--repo", repo,
            "--description", "Managed by git-contribute",
            "--color", "0E8A16",
            "--force",
        )


def add_in_progress(repo: str, num: int) -> None:
    gh("pr", "edit", str(num), "--repo", repo, "--add-label", "bot:in-progress")


def remove_in_progress(repo: str, num: int) -> None:
    gh("pr", "edit", str(num), "--repo", repo, "--remove-label", "bot:in-progress")


def swap_label(repo: str, num: int, old: str, new: str) -> None:
    gh("pr", "edit", str(num), "--repo", repo, "--remove-label", old, "--add-label", new)


def get_repo(repo: str | None = None) -> str:
    if repo:
        return repo
    return gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")


def read_repo_conventions(repo: str) -> str:
    conventions = []
    for fname in ("CONTRIBUTING.md", "CLAUDE.md", "AGENTS.md", "CODING_GUIDELINES.md"):
        try:
            content = git("show", f"HEAD:{fname}")
            conventions.append(f"## {fname}\n{content}")
        except RuntimeError:
            pass
    return "\n\n".join(conventions) or "(no convention files found)"


def load_prompt(phase: str, **kwargs: str) -> str:
    template_path = PROMPTS_DIR / f"{phase}.md"
    template = template_path.read_text()
    return template.format(**kwargs)


def claude(prompt: str) -> str:
    """Run claude CLI for reasoning tasks. Output-only, no tool access."""
    result = subprocess.run(
        ["claude", "-p", prompt, "--print"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr.strip()}")
    return result.stdout.strip()


def claude_interactive(prompt: str, workdir: str) -> str:
    """Run claude with full tool access for implementation work."""
    result = subprocess.run(
        ["claude", "--dangerously-skip-permissions", "-p", prompt, "--print"],
        capture_output=True, text=True,
        cwd=workdir,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr.strip()}")
    return result.stdout.strip()
