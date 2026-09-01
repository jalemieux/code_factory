"""Worktree context-manager tests against a real local scratch repo.

Everything runs inside a tempdir: a local bare `origin.git`, one clone,
worktrees under the clone's `.worktrees/`. No network, no gh, no agent
CLIs, and the repo's `.env` is never loaded (these tests never call
`load_env` or `main`).
"""

import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import code_factory
from code_factory import Worktree, WorktreeCleanupRefused


def _git(cwd: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr.strip()}")
    return result.stdout.strip()


class WorktreeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.origin = str(base / "origin.git")
        self.clone = str(base / "clone")
        _git(self._tmp.name, "init", "--bare", "-b", "main", self.origin)
        _git(self._tmp.name, "clone", self.origin, self.clone)
        _git(self.clone, "config", "user.name", "Test Bot")
        _git(self.clone, "config", "user.email", "bot@example.invalid")
        (Path(self.clone) / "README.md").write_text("seed\n")
        _git(self.clone, "add", "README.md")
        _git(self.clone, "commit", "-m", "seed")
        _git(self.clone, "push", "origin", "main")

    def _commit_file(self, wt_path: str, name: str, content: str = "x\n") -> None:
        (Path(wt_path) / name).write_text(content)
        _git(wt_path, "add", name)
        _git(wt_path, "commit", "-m", f"add {name}")

    def _branch_exists(self, branch: str) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.clone, capture_output=True, text=True,
        )
        return result.returncode == 0

    # --- enter ---

    def test_enter_creates_worktree_under_dot_worktrees(self):
        with Worktree(self.clone, "bot/1-alpha") as wt:
            self.assertTrue(os.path.isdir(wt.path))
            self.assertTrue(
                wt.path.startswith(os.path.join(self.clone, ".worktrees") + os.sep)
            )
            self.assertEqual(_git(wt.path, "branch", "--show-current"), "bot/1-alpha")
            # Refuse to exit cleanly with an unpushed empty branch (it has the
            # seed commit but no remote ref) — push so exit passes the gate.
            _git(wt.path, "push", "origin", "HEAD:bot/1-alpha")

    def test_enter_reuses_remote_branch(self):
        # Create + push a branch, delete locally, then enter — must start
        # from origin/<branch>, not a fresh HEAD branch.
        _git(self.clone, "checkout", "-b", "bot/2-beta")
        self._commit_file(self.clone, "beta.txt")
        _git(self.clone, "push", "origin", "HEAD:bot/2-beta")
        remote_sha = _git(self.clone, "rev-parse", "HEAD")
        _git(self.clone, "checkout", "main")
        _git(self.clone, "branch", "-D", "bot/2-beta")

        with Worktree(self.clone, "bot/2-beta") as wt:
            self.assertEqual(_git(wt.path, "rev-parse", "HEAD"), remote_sha)

    # --- exit gates (spike 0.1) ---

    def test_unpushed_commit_refuses_branch_deletion(self):
        with self.assertRaises(WorktreeCleanupRefused) as ctx:
            with Worktree(self.clone, "bot/3-gamma") as wt:
                self._commit_file(wt.path, "gamma.txt")
                path = wt.path
        self.assertIn("unpushed", str(ctx.exception))
        # Everything left in place: worktree dir, branch ref, the commit.
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(self._branch_exists("bot/3-gamma"))

    def test_no_remote_ref_treated_as_unpushed(self):
        # Branch created fresh from HEAD, nothing ever pushed: no upstream
        # AND no origin/<branch> — must refuse, not crash or delete.
        with self.assertRaises(WorktreeCleanupRefused):
            with Worktree(self.clone, "bot/4-delta"):
                pass
        self.assertTrue(self._branch_exists("bot/4-delta"))

    def test_pushed_branch_cleanup_proceeds(self):
        with Worktree(self.clone, "bot/5-epsilon") as wt:
            self._commit_file(wt.path, "epsilon.txt")
            # Explicit refspec, no -u — the push updates
            # refs/remotes/origin/<branch>, which the gate falls back to.
            _git(wt.path, "push", "origin", "HEAD:bot/5-epsilon")
            path = wt.path
        self.assertFalse(os.path.exists(path))
        self.assertFalse(self._branch_exists("bot/5-epsilon"))
        # The pushed commit survives in origin.
        sha = _git(self.clone, "ls-remote", self.origin, "refs/heads/bot/5-epsilon")
        self.assertTrue(sha)

    def test_upstream_tracking_branch_gate_uses_upstream(self):
        with Worktree(self.clone, "bot/6-zeta") as wt:
            self._commit_file(wt.path, "zeta.txt")
            _git(wt.path, "push", "-u", "origin", "bot/6-zeta")
            path = wt.path
        self.assertFalse(os.path.exists(path))
        self.assertFalse(self._branch_exists("bot/6-zeta"))

    def test_dirty_tree_refuses_removal_without_force(self):
        with self.assertRaises(WorktreeCleanupRefused) as ctx:
            with Worktree(self.clone, "bot/7-eta") as wt:
                self._commit_file(wt.path, "eta.txt")
                _git(wt.path, "push", "origin", "HEAD:bot/7-eta")
                # Dirty the tree after pushing — gate must still refuse.
                (Path(wt.path) / "scratch.txt").write_text("uncommitted\n")
                path = wt.path
        self.assertIn("uncommitted", str(ctx.exception))
        self.assertTrue(os.path.isdir(path))
        self.assertTrue((Path(path) / "scratch.txt").exists())
        self.assertTrue(self._branch_exists("bot/7-eta"))

    def test_body_exception_leaves_worktree_and_propagates(self):
        with self.assertRaises(ValueError):
            with Worktree(self.clone, "bot/8-theta") as wt:
                self._commit_file(wt.path, "theta.txt")
                path = wt.path
                raise ValueError("phase blew up")
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(self._branch_exists("bot/8-theta"))

    def test_reenter_after_refused_cleanup_of_same_branch_fails_loudly(self):
        # A refused cleanup leaves the branch checked out in .worktrees/;
        # a second Worktree on the same branch must not silently steal it.
        with self.assertRaises(WorktreeCleanupRefused):
            with Worktree(self.clone, "bot/9-iota") as wt:
                self._commit_file(wt.path, "iota.txt")
        with self.assertRaises(RuntimeError):
            Worktree(self.clone, "bot/9-iota").__enter__()

    def test_main_clone_checkout_untouched(self):
        before = _git(self.clone, "rev-parse", "HEAD")
        with Worktree(self.clone, "bot/10-kappa") as wt:
            self._commit_file(wt.path, "kappa.txt")
            _git(wt.path, "push", "origin", "HEAD:bot/10-kappa")
        self.assertEqual(_git(self.clone, "rev-parse", "HEAD"), before)
        self.assertEqual(_git(self.clone, "branch", "--show-current"), "main")
        self.assertEqual(_git(self.clone, "status", "--porcelain"), "")


class GitRetryTestCase(unittest.TestCase):
    def test_retries_on_lock_contention_then_succeeds(self):
        calls = []

        def fake_git(*args, cwd=None):
            calls.append(args)
            if len(calls) < 3:
                raise RuntimeError("error: could not lock config file .git/config")
            return "ok"

        with unittest.mock.patch.object(code_factory, "git", side_effect=fake_git):
            with unittest.mock.patch.object(code_factory.time, "sleep"):
                result = code_factory.git_retry("config", "x", "y")
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3)

    def test_non_lock_error_raises_immediately(self):
        with unittest.mock.patch.object(
            code_factory, "git",
            side_effect=RuntimeError("fatal: not a git repository"),
        ) as mock_git:
            with self.assertRaises(RuntimeError):
                code_factory.git_retry("status")
        self.assertEqual(mock_git.call_count, 1)

    def test_lock_error_exhausts_attempts_then_raises(self):
        with unittest.mock.patch.object(
            code_factory, "git",
            side_effect=RuntimeError("could not lock config file"),
        ) as mock_git:
            with unittest.mock.patch.object(code_factory.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    code_factory.git_retry("push", attempts=3)
        self.assertEqual(mock_git.call_count, 3)


if __name__ == "__main__":
    unittest.main()
