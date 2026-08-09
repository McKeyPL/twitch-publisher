from pathlib import Path

import pytest

from youtube_copyright.process_lock import GuardAlreadyRunning, SingleInstanceLock


def test_single_instance_lock_rejects_second_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "guard.lock"
    with SingleInstanceLock(lock_path):
        with pytest.raises(GuardAlreadyRunning):
            with SingleInstanceLock(lock_path):
                pass
    with SingleInstanceLock(lock_path):
        pass
