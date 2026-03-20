---
name: git-contribute
description: "Autonomous bug fix and feature implementation lifecycle for GitHub codebases — picks up open issues, proposes an implementation plan via draft PR for human review, incorporates feedback, implements the fix or feature using TDD, and shepherds the PR through code review to merge."
---

# Git Contribute

Run the Code Factory orchestrator for a single pass:

```bash
python3 code_factory.py --once
```

To target a specific repo:

```bash
python3 code_factory.py --once --repo {repo}
```

For continuous polling:

```bash
python3 code_factory.py --repo {repo}
```

See `TROUBLESHOOTING.md` for diagnostics.
