from pathlib import Path

import pytest

from codex_forge.utils import ControllerAlreadyRunning, controller_lock


def test_controller_lock_rejects_duplicate_process_session(tmp_path: Path):
    path = tmp_path / "controller.lock"
    with controller_lock(path):
        with pytest.raises(ControllerAlreadyRunning):
            with controller_lock(path):
                raise AssertionError("duplicate lock was acquired")
