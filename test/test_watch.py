import signal
import threading
import time
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


def test_watch_runs_when_triggered(tmp_path: Path) -> None:
    stop = threading.Event()
    trigger = threading.Event()
    settings = Settings(backup_root=tmp_path / 'backup', poll_seconds=10)
    summaries: list[BackupSummary] = []

    def action(value: Settings) -> BackupSummary:
        summaries.append(BackupSummary())
        if len(summaries) == 2:
            stop.set()
        return summaries[-1]

    threading.Timer(0.01, trigger.set).start()

    watch(settings, stop=stop, trigger=trigger, action=action)

    assert summaries == [BackupSummary(), BackupSummary()]


def test_watch_preserves_trigger_received_during_a_pass(tmp_path: Path) -> None:
    stop = threading.Event()
    trigger = threading.Event()
    calls = 0

    def action(value: Settings) -> BackupSummary:
        nonlocal calls
        calls += 1
        if calls == 1:
            trigger.set()
        else:
            stop.set()
        return BackupSummary()

    watch(
        Settings(backup_root=tmp_path, poll_seconds=10),
        stop=stop,
        trigger=trigger,
        action=action,
    )

    assert calls == 2


def test_watch_stops_promptly_while_waiting_for_trigger(tmp_path: Path) -> None:
    stop = threading.Event()

    def action(value: Settings) -> BackupSummary:
        stop.set()
        return BackupSummary()

    started = time.monotonic()
    watch(
        Settings(backup_root=tmp_path, poll_seconds=10),
        stop=stop,
        trigger=threading.Event(),
        action=action,
    )

    assert time.monotonic() - started < 1


def test_watch_reports_transient_failure_and_runs_another_pass(tmp_path: Path) -> None:
    stop = threading.Event()
    summaries: list[BackupSummary] = []
    attempts = 0

    def action(value: Settings) -> BackupSummary:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError('network unavailable')
        stop.set()
        return BackupSummary()

    watch(
        Settings(backup_root=tmp_path, poll_seconds=0.01),
        stop=stop,
        action=action,
        report=summaries.append,
    )

    assert attempts == 2
    assert summaries[0].results[0].detail == 'network unavailable'


def test_watch_stops_after_signal_during_transfer(tmp_path: Path) -> None:
    calls = 0

    def transfer(settings: Settings) -> BackupSummary:
        nonlocal calls
        calls += 1
        signal.raise_signal(signal.SIGTERM)
        return BackupSummary(copied=1)

    watch(Settings(backup_root=tmp_path, poll_seconds=10), action=transfer)

    assert calls == 1
