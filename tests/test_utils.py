from pathlib import Path

import pytest

from codex_forge.utils import ControllerAlreadyRunning, controller_lock


def test_tail_text_zero_is_empty_and_negative_is_rejected():
    from codex_forge.utils import tail_text

    assert tail_text("private output" * 100, 0) == ""
    with pytest.raises(ValueError):
        tail_text("output", -1)


def test_controller_lock_rejects_duplicate_process_session(tmp_path: Path):
    path = tmp_path / "controller.lock"
    with controller_lock(path):
        with pytest.raises(ControllerAlreadyRunning):
            with controller_lock(path):
                raise AssertionError("duplicate lock was acquired")
