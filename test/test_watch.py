import threading
from pathlib import Path

from baccy.models import BackupSummary, PathSource, Settings
from baccy.watch import watch


def test_watch_runs_one_pass_before_stopping(tmp_path: Path) -> None:
    stop = threading.Event()
    settings = Settings(
        backup_root=tmp_path / 'backup',
        poll_seconds=1,
        sources=[PathSource(kind='path', name='source', path=tmp_path / 'source')],
    )
    summaries: list[BackupSummary] = []

    def action(value: Settings) -> BackupSummary:
        stop.set()
        return BackupSummary(copied=1)

    watch(settings, stop=stop, action=action, report=summaries.append)

    assert summaries == [BackupSummary(copied=1)]
