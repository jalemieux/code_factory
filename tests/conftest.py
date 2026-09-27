"""Suite-wide isolation: no test may touch the real `logs/` state.

Failure counts persist between `run` invocations by design, so a test that
drives a failing chain without redirecting FAILURE_STATE_PATH poisons the
real counter (and, past the threshold, reaches a live `gh pr comment`).
"""

import pytest

import code_factory


@pytest.fixture(autouse=True)
def _isolated_failure_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        code_factory, "FAILURE_STATE_PATH", tmp_path / "failure_counts.json"
    )
