import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest import CaptureFixture, MonkeyPatch
from reccy.services.models import StatusResult

from baccy.cli import main
from baccy.models import (
    BackupSummary,
    FileResult,
    ResolvedSource,
    Settings,
    SourceSelection,
    VolumeSource,
)


class NoNetworkDiscovery:
    new_machines: list[object] = []

    def discover(self) -> list[object]:
        return []


@pytest.fixture(autouse=True)
def disable_network_discovery(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr('baccy.backup.NetworkDiscovery', NoNetworkDiscovery)


def test_global_config_selects_backup_settings(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'recording.toml').write_text('format = "recs"\n')
    config = tmp_path / 'baccy.toml'
    config.write_text(
        f'backup_root = "{tmp_path / "backup"}"\n'
        'stability_seconds = 0\n'
        'discover_removable = false\n'
        'verbose = false\n'
        '[[sources]]\n'
        'kind = "path"\n'
        'name = "source"\n'
        f'path = "{source}"\n'
    )

    exit_code = main(['--config', str(config), 'backup'])

    assert exit_code == 0
    assert capsys.readouterr().out == 'recording.toml\n'

    exit_code = main(['--config', str(config), 'backup'])

    assert exit_code == 0
    assert capsys.readouterr().out == '(no files)\n'


def test_daemon_uses_its_recorded_configuration(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{tmp_path / "backup"}"\n')
    metadata = tmp_path / 'daemon.json'
    metadata.write_text('{"argv": ["watch", "--config", "' + str(config) + '"]}')
    monkeypatch.setattr(
        'baccy.cli.Application',
        lambda: SimpleNamespace(paths=SimpleNamespace(metadata=metadata)),
    )
    received: list[Path] = []

    def backup(settings: Settings, dry_run: bool) -> BackupSummary:
        received.append(settings.backup_root)
        return BackupSummary()

    monkeypatch.setattr('baccy.cli.run_backup', backup)

    assert main(['--daemon', 'backup']) == 0
    assert received == [tmp_path / 'backup']


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

    monkeypatch.setattr('baccy.cli.rpc.Client', Client)

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

    monkeypatch.setattr('baccy.cli.rpc.Client', Client)
    monkeypatch.setattr('baccy.cli.time.sleep', sleeps.append)

    assert main(['--daemon', 'sync']) == 0
    assert attempts == 2
    assert sleeps == [0.1]


def test_daemon_and_config_are_mutually_exclusive(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    assert main(['--daemon', '--config', str(tmp_path / 'baccy.toml'), 'backup']) == 2
    assert capsys.readouterr().err == '--daemon cannot be used with --config\n'


def test_backup_command_verbose_includes_unchanged_files(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'recording.toml').write_text('format = "recs"\n')
    config = tmp_path / 'baccy.toml'
    config.write_text(
        f'backup_root = "{tmp_path / "backup"}"\n'
        'stability_seconds = 0\n'
        'discover_removable = false\n'
        'verbose = true\n'
        '[[sources]]\n'
        'kind = "path"\n'
        'name = "source"\n'
        f'path = "{source}"\n'
    )

    main(['backup', '--config', str(config)])
    capsys.readouterr()
    main(['backup', '--config', str(config)])

    assert capsys.readouterr().out == 'recording.toml\n'


def test_watch_command_prints_only_changed_summaries(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{tmp_path / "backup"}"\n')

    def run_watch(settings: object, **kwargs: object) -> None:
        report = kwargs['report']
        assert callable(report)
        report(BackupSummary())
        report(BackupSummary())
        report(
            BackupSummary().with_result(
                FileResult(
                    source='source',
                    relative_path=Path('recording.toml'),
                    status='copied',
                )
            )
        )

    monkeypatch.setattr('baccy.cli.watch', run_watch)

    assert main(['watch', '--config', str(config)]) == 0

    assert capsys.readouterr().out == '(no files)\nrecording.toml\n'


def test_daemon_watch_logs_each_file_as_json(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{tmp_path / "backup"}"\n')
    summaries: list[BackupSummary] = []

    class DaemonApplication:
        sync_requested = None

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def record_summary(self, summary: BackupSummary) -> None:
            summaries.append(summary)

    def run_watch(settings: object, **kwargs: object) -> None:
        report = kwargs['report']
        assert callable(report)
        report(
            BackupSummary(
                results=[
                    FileResult(
                        source='source',
                        relative_path=Path('unchanged.wav'),
                        status='unchanged',
                    ),
                    FileResult(
                        source='source', relative_path=Path('one.wav'), status='copied'
                    ),
                    FileResult(
                        source='source',
                        relative_path=Path('two.wav'),
                        status='uploaded',
                    ),
                ]
            )
        )
        report(
            BackupSummary(
                results=[
                    FileResult(
                        source='source',
                        relative_path=Path('unchanged.wav'),
                        status='unchanged',
                    )
                ]
            )
        )

    monkeypatch.setenv('BACCY_DAEMON', '1')
    monkeypatch.setattr('baccy.cli.DaemonApplication', DaemonApplication)
    monkeypatch.setattr('baccy.cli._configure_daemon_logging', lambda: None)
    monkeypatch.setattr('baccy.cli.watch', run_watch)

    assert main(['--config', str(config), 'watch']) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    for value in output:
        assert value.pop('timestamp').endswith('Z')
    assert output == [
        {'source': 'source', 'relative_path': 'one.wav', 'status': 'copied'},
        {'source': 'source', 'relative_path': 'two.wav', 'status': 'uploaded'},
    ]
    assert summaries == [
        BackupSummary(
            results=[
                FileResult(
                    source='source', relative_path=Path('one.wav'), status='copied'
                ),
                FileResult(
                    source='source', relative_path=Path('two.wav'), status='uploaded'
                ),
            ]
        ),
        BackupSummary(),
    ]


def test_service_commands_print_toml(
    capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    class ServiceApplication:
        def service_status(self) -> StatusResult:
            return StatusResult(installed=True, running=True, details='ready')

    monkeypatch.setattr('baccy.cli.Application', ServiceApplication)

    exit_code = main(['service', 'status'])

    assert exit_code == 0
    assert tomllib.loads(capsys.readouterr().out) == {
        'installed': True,
        'running': True,
        'details': 'ready',
    }


def test_service_install_waits_for_daemon_and_schedules_sync(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'
    installed: list[list[str]] = []
    calls: list[str] = []

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> StatusResult:
            installed.append(arguments)
            return StatusResult(installed=True)

        def service_status(self) -> StatusResult:
            raise AssertionError('running daemon should not need a status check')

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            assert value == endpoint
            assert role == 'baccy-cli'

        def call(self, command: str) -> dict[str, bool]:
            calls.append(command)
            return {'running': True} if command == 'status' else {'scheduled': True}

    monkeypatch.setattr('baccy.cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.cli.rpc.Client', Client)

    assert main(['--config', str(tmp_path / 'baccy.toml'), 'service', 'install']) == 0
    assert installed == [['watch', '--config', str(tmp_path / 'baccy.toml')]]
    assert calls == ['status', 'sync']
    assert tomllib.loads(capsys.readouterr().out) == {
        'installed': True,
        'details': '',
    }


def test_service_install_reports_daemon_start_failure(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> StatusResult:
            return StatusResult(installed=True)

        def service_status(self) -> StatusResult:
            return StatusResult(installed=True, running=False, details='exited')

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            pass

        def call(self, command: str) -> dict[str, bool]:
            raise FileNotFoundError('socket missing')

    monkeypatch.setattr('baccy.cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.cli.rpc.Client', Client)

    assert main(['--config', str(tmp_path / 'baccy.toml'), 'service', 'install']) == 1
    assert capsys.readouterr().err == 'baccy daemon failed to start: exited\n'


def test_service_install_can_skip_sync(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'
    calls: list[str] = []

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> StatusResult:
            return StatusResult(installed=True)

        def service_status(self) -> StatusResult:
            raise AssertionError('running daemon should not need a status check')

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            pass

        def call(self, command: str) -> dict[str, bool]:
            calls.append(command)
            return {'running': True}

    monkeypatch.setattr('baccy.cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.cli.rpc.Client', Client)

    assert (
        main(
            [
                '--config',
                str(tmp_path / 'baccy.toml'),
                'service',
                'install',
                '--no-sync',
            ]
        )
        == 0
    )
    assert calls == ['status']


def test_test_command_prints_ok_for_reachable_destinations(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{tmp_path / "backup"}"\n')
    monkeypatch.setattr('baccy.cli.test_destinations', lambda settings: [])

    assert main(['test', '--config', str(config)]) == 0

    captured = capsys.readouterr()
    assert captured.out == 'ok\n'
    assert captured.err == ''


def test_test_command_reports_unreachable_destinations(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{tmp_path / "backup"}"\n')
    monkeypatch.setattr(
        'baccy.cli.test_destinations', lambda settings: ['archive: access denied']
    )

    assert main(['test', '--config', str(config)]) == -1

    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'archive: access denied\n'


@pytest.mark.parametrize('flag', ['-d', '--dry-run'])
def test_backup_command_dry_run_does_not_write(
    tmp_path: Path, capsys: CaptureFixture[str], flag: str
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'recording.toml').write_text('format = "recs"\n')
    destination = tmp_path / 'backup'
    config = tmp_path / 'baccy.toml'
    config.write_text(
        f'backup_root = "{destination}"\n'
        'stability_seconds = 0\n'
        'discover_removable = false\n'
        '[[sources]]\n'
        'kind = "path"\n'
        'name = "source"\n'
        f'path = "{source}"\n'
    )

    exit_code = main(['backup', flag, '--config', str(config)])

    assert exit_code == 0
    assert capsys.readouterr().out == 'recording.toml\n'
    assert not destination.exists()


def test_backup_command_without_configuration_uses_main_drive(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    session = tmp_path / 'card' / 'recs-session'
    session.mkdir(parents=True)
    (session / 'session-record.jsonl').write_text('{"type":"header"}\n')
    source = VolumeSource(kind='volume', name='removable-test-uuid', uuid='test-uuid')
    resolved = ResolvedSource(
        source=source,
        root=tmp_path / 'card',
        selections=[SourceSelection(relative_root=Path('recs-session'))],
    )
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(
        'baccy.backup.discover_removable_sources',
        lambda backup_root, configured: [resolved],
    )

    exit_code = main(['backup'])

    destination = tmp_path / 'baccy' / 'audio' / 'recs-session' / 'session-record.jsonl'
    assert exit_code == 0
    assert capsys.readouterr().out == 'recs-session/session-record.jsonl\n'
    assert destination.read_text() == '{"type":"header"}\n'


def test_import_command_moves_project_sessions_from_their_header(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    source = tmp_path / 'incoming-project'
    session = source / '2026' / '09' / '24' / '20-00-00'
    session.mkdir(parents=True)
    (session / 'session-record.jsonl').write_text(
        '{"type":"header","project_name":"concert"}\n'
    )
    backup = tmp_path / 'backup'
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{backup}"\n')

    assert main(['import', str(source), '--config', str(config)]) == 0

    destination = backup / 'audio' / 'concert' / '2026' / '09' / '24' / '20-00-00'
    assert capsys.readouterr().out == 'audio/concert/2026/09/24/20-00-00\n'
    assert (destination / 'session-record.jsonl').exists()
    assert not session.exists()


def test_import_command_does_not_publish_sessions(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    source = tmp_path / 'incoming-project'
    session = source / '2026' / '09' / '24' / '20-00-00'
    session.mkdir(parents=True)
    (session / 'session-record.jsonl').write_text(
        '{"type":"header","project_name":"concert"}\n'
    )
    config = tmp_path / 'baccy.toml'
    config.write_text(
        f'backup_root = "{tmp_path / "backup"}"\n'
        '[[uploads]]\n'
        'name = "archive"\n'
        'match = "True"\n'
        'encoding = { format = "flac" }\n'
        'destination = "s3:archive"\n'
    )

    def publish_sessions(*args: object, **kwargs: object) -> None:
        raise AssertionError('import must not publish')

    monkeypatch.setattr(
        'baccy.importer.publish_sessions', publish_sessions, raising=False
    )

    assert main(['import', str(source), '--config', str(config)]) == 0


def test_import_command_copies_direct_session_with_project_override(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    session = tmp_path / '20-00-00'
    session.mkdir()
    (session / 'session-record.jsonl').write_text(
        '{"type":"header","started_at":"2026-09-24T20:00:00Z"}\n'
    )
    backup = tmp_path / 'backup'
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{backup}"\n')

    assert (
        main(
            [
                'import',
                str(session),
                '--copy',
                '--project',
                'concert',
                '--config',
                str(config),
            ]
        )
        == 0
    )

    assert session.exists()
    assert (
        backup
        / 'audio'
        / 'concert'
        / '2026'
        / '09'
        / '24'
        / '20-00-00'
        / 'session-record.jsonl'
    ).exists()


def test_global_dry_run_previews_import_without_writing(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    source = tmp_path / 'incoming-project'
    session = source / '2026' / '09' / '24' / '20-00-00'
    session.mkdir(parents=True)
    (session / 'session-record.jsonl').write_text(
        '{"type":"header","project_name":"concert"}\n'
    )
    backup = tmp_path / 'backup'
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{backup}"\n')

    assert main(['--dry-run', 'import', str(source), '--config', str(config)]) == 0

    assert capsys.readouterr().out == 'audio/concert/2026/09/24/20-00-00\n'
    assert session.exists()
    assert not backup.exists()


def test_global_dry_run_reaches_sync(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{tmp_path / "backup"}"\n')
    received: list[bool] = []
    monkeypatch.setattr(
        'baccy.cli.sync',
        lambda directories, settings, dry_run: (
            received.append(dry_run) or BackupSummary()
        ),
    )

    assert main(['-d', 'sync', '--config', str(config)]) == 0

    assert received == [True]
    assert capsys.readouterr().out == '(no files)\n'
