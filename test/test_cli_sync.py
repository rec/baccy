from pathlib import Path
from types import SimpleNamespace

from pytest import CaptureFixture, MonkeyPatch

from baccy.cli import main
from baccy.models import BackupSummary


def test_daemon_sync_requests_daemon(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    metadata = tmp_path / 'daemon.json'
    metadata.write_text('{"argv": ["watch", "--config", "' + str(config) + '"]}')
    endpoint = tmp_path / 'gui.sock'
    monkeypatch.setattr(
        'baccy.cli.Application',
        lambda: SimpleNamespace(
            paths=SimpleNamespace(metadata=metadata), control_endpoint=endpoint
        ),
    )
    calls: list[tuple[Path, str, str]] = []

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            calls.append((value, role, 'created'))

        def call(self, command: str) -> dict[str, bool]:
            calls.append((endpoint, 'baccy-cli', command))
            return {'scheduled': True}

    monkeypatch.setattr('baccy.service_cli.rpc.Client', Client)

    assert main(['--daemon', 'sync']) == 0
    assert capsys.readouterr().out == ''
    assert calls == [
        (endpoint, 'baccy-cli', 'created'),
        (endpoint, 'baccy-cli', 'sync'),
    ]


def test_daemon_sync_rejects_directories(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    metadata = tmp_path / 'daemon.json'
    metadata.write_text('{"argv": ["watch", "--config", "' + str(tmp_path) + '"]}')
    monkeypatch.setattr(
        'baccy.cli.Application',
        lambda: SimpleNamespace(paths=SimpleNamespace(metadata=metadata)),
    )

    assert main(['--daemon', 'sync', 'totm']) == 2
    assert capsys.readouterr().err == '--daemon sync does not accept directories\n'


def test_daemon_sync_waits_for_newly_installed_daemon(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    metadata = tmp_path / 'daemon.json'
    metadata.write_text('{"argv": ["watch", "--config", "' + str(config) + '"]}')
    endpoint = tmp_path / 'gui.sock'
    monkeypatch.setattr(
        'baccy.cli.Application',
        lambda: SimpleNamespace(
            paths=SimpleNamespace(metadata=metadata), control_endpoint=endpoint
        ),
    )
    attempts = 0
    sleeps: list[float] = []

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            pass

        def call(self, command: str) -> dict[str, bool]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise FileNotFoundError()
            return {'scheduled': True}

    monkeypatch.setattr('baccy.service_cli.rpc.Client', Client)
    monkeypatch.setattr('baccy.service_cli.time.sleep', sleeps.append)

    assert main(['--daemon', 'sync']) == 0
    assert attempts == 2
    assert sleeps == [0.1]


def test_global_dry_run_reaches_sync(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{tmp_path / "backup"}"\n')
    received: list[bool] = []
    monkeypatch.setattr(
        'baccy.cli.sync',
        lambda directories, settings, dry_run, verify: (
            received.append(dry_run) or BackupSummary()
        ),
    )

    assert main(['-d', '--config', str(config), 'sync']) == 0

    assert received == [True]
    assert capsys.readouterr().out == '(no files)\n'


def test_verified_sync_runs_foreground_with_daemon_configuration(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{tmp_path / "backup"}"\n')
    received: list[bool] = []
    monkeypatch.setattr('baccy.cli._daemon_config', lambda: config)
    monkeypatch.setattr(
        'baccy.cli.sync',
        lambda directories, settings, dry_run, verify: (
            received.append(verify) or BackupSummary()
        ),
    )

    assert main(['sync', '--verify']) == 0
    assert received == [True]
