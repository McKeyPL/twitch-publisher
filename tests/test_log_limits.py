import logging
from types import SimpleNamespace

import log_limits


def test_log_rolls_over_and_keeps_only_five_backups(tmp_path):
    path = tmp_path / "publisher.log"
    handler = log_limits.BoundedLogHandler(path)
    handler.maxBytes = 80
    try:
        for index in range(12):
            handler.handle(logging.LogRecord("test", logging.INFO, "", 0, "%s %s", (index, "x" * 55), None))
    finally:
        handler.close()
    assert len(list(tmp_path.glob("publisher.log.*"))) == 5
    assert path.read_text().startswith("11 ")


def test_disk_space_pauses_and_resumes(monkeypatch, tmp_path, caplog):
    monitor = log_limits.DiskSpaceMonitor()
    usage = SimpleNamespace(free=9 * log_limits.GIB)
    monkeypatch.setattr(log_limits.shutil, "disk_usage", lambda _: usage)
    assert not monitor.allows_large_writes(tmp_path)
    assert not monitor.allows_large_writes(tmp_path)
    assert len(caplog.records) == 1
    usage.free = 21 * log_limits.GIB
    assert monitor.allows_large_writes(tmp_path)
