#!/usr/bin/env bash
# Make the code-factory skill loadable by every harness on this machine by
# symlinking this directory into their global skill locations:
#   ~/.claude/skills   Claude Code, OpenCode
#   ~/.agents/skills   Codex CLI, GitHub Copilot, OpenCode
# Symlinks (not copies) so the skill tracks the checkout and scripts/cf can
# find code_factory.py. Re-run safely; pass --uninstall to remove the links.
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
NAME="$(basename "$SKILL_DIR")"
for base in "$HOME/.claude/skills" "$HOME/.agents/skills"; do
  link="$base/$NAME"
  if [ "${1:-}" = "--uninstall" ]; then
    [ -L "$link" ] && rm "$link" && echo "removed $link"
    continue
  fi
  mkdir -p "$base"
  if [ -L "$link" ]; then
    ln -sfn "$SKILL_DIR" "$link"
  elif [ -e "$link" ]; then
    echo "skip $link: exists and is not a symlink" >&2; continue
  else
    ln -s "$SKILL_DIR" "$link"
  fi
  echo "linked $link"
done
