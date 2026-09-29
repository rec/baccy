import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest import CaptureFixture, MonkeyPatch

from baccy.cli import main
from baccy.models import (
    BackupSummary,
    FileResult,
    ResolvedSource,
    Settings,
    SourceSelection,
    VolumeSource,
)
from baccy.rename import RenameFile


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

    assert main(['backup']) == 0
    assert received == [tmp_path / 'backup']


def test_help_does_not_require_daemon_metadata(
    capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(
        'baccy.cli.Application',
        lambda: pytest.fail('help must not inspect the installed daemon'),
    )

    assert main(['--help']) == 0
    assert capsys.readouterr().out.startswith('Usage: baccy ')


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

    main(['--config', str(config), 'backup'])
    capsys.readouterr()
    main(['--config', str(config), 'backup'])

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
            BackupSummary.from_results(
                [
                    FileResult(
                        source='source',
                        relative_path=Path('recording.toml'),
                        status='copied',
                    )
                ]
            )
        )

    monkeypatch.setattr('baccy.cli.watch', run_watch)

    assert main(['--config', str(config), 'watch']) == 0

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


def test_test_command_prints_ok_for_reachable_destinations(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{tmp_path / "backup"}"\n')
    monkeypatch.setattr('baccy.cli.test_destinations', lambda settings: [])

    assert main(['--config', str(config), 'test']) == 0

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

    assert main(['--config', str(config), 'test']) == -1

    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'archive: access denied\n'


def test_list_command_prints_uploaded_files(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text(f'backup_root = "{tmp_path / "backup"}"\n')
    monkeypatch.setattr(
        'baccy.cli.list_present_uploads',
        lambda settings: [
            's3:archive/totm/audio.flac',
            'ssh:user@example.org:/srv/a.html',
        ],
    )

    assert main(['--config', str(config), 'list']) == 0

    captured = capsys.readouterr()
    assert captured.out == (
        's3:archive/totm/audio.flac\nssh:user@example.org:/srv/a.html\n'
    )
    assert captured.err == ''


@pytest.mark.parametrize('command', ['list', 'test'])
def test_read_only_commands_reject_dry_run(
    tmp_path: Path, capsys: CaptureFixture[str], command: str
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text('')

    assert main(['--config', str(config), '--dry-run', command]) == 2
    assert capsys.readouterr().err == (f'--dry-run is not supported for {command}\n')


def test_list_reports_remote_failure_without_traceback(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text('')

    def unavailable(settings: Settings) -> list[str]:
        raise OSError('SSH unavailable')

    monkeypatch.setattr('baccy.cli.list_present_uploads', unavailable)

    assert main(['--config', str(config), 'list']) == 1
    assert capsys.readouterr().err == 'could not list uploads: SSH unavailable\n'


def test_backup_reports_deferred_work_with_distinct_exit_status(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text('')
    monkeypatch.setattr(
        'baccy.cli.run_backup',
        lambda settings, dry_run: BackupSummary.from_results(
            [
                FileResult(
                    source='studio',
                    relative_path=Path('audio.flac'),
                    status='deferred',
                    detail='still recording',
                )
            ]
        ),
    )

    assert main(['--config', str(config), 'backup']) == 3
    assert capsys.readouterr().out == ('deferred: audio.flac (still recording)\n')


def test_backup_reports_failure_detail(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text('')
    monkeypatch.setattr(
        'baccy.cli.run_backup',
        lambda settings, dry_run: BackupSummary.from_results(
            [FileResult(source='studio', status='failed', detail='disk full')]
        ),
    )

    assert main(['--config', str(config), 'backup']) == 1
    assert capsys.readouterr().out == 'failed: studio (disk full)\n'


def test_rename_dry_run_does_not_rename(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text('')
    file = RenameFile(
        session=Path('totm/session'),
        source=Path('totm/session/audio/old.flac'),
        replacement=Path('totm/session/audio/new.flac'),
        targets=[],
    )
    monkeypatch.setattr(
        'baccy.cli.renamed_files',
        lambda settings, pattern, replacement, regular_expression: [file],
    )
    monkeypatch.setattr(
        'baccy.cli.rename_files',
        lambda settings, files, pattern, replacement: (_ for _ in ()).throw(
            AssertionError()
        ),
    )

    assert main(['--config', str(config), '--dry-run', 'rename', 'old', 'new']) == 0

    captured = capsys.readouterr()
    assert captured.out == 'totm/session/audio/old.flac -> new.flac\n'
    assert captured.err == ''


def test_rename_keeps_global_flag_text_as_positional_argument(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text('')
    received: list[tuple[str, str]] = []

    def planned(
        settings: Settings, pattern: str, replacement: str, regular_expression: bool
    ) -> list[RenameFile]:
        received.append((pattern, replacement))
        return []

    monkeypatch.setattr('baccy.cli.renamed_files', planned)

    assert (
        main(['--config', str(config), '--dry-run', 'rename', '--', '--config', 'new'])
        == 0
    )
    assert received == [('--config', 'new')]


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

    exit_code = main(['--config', str(config), flag, 'backup'])

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
    config = tmp_path / 'baccy.toml'
    config.write_text('')

    exit_code = main(['--config', str(config), 'backup'])

    destination = tmp_path / 'baccy' / 'audio' / 'recs-session' / 'session-record.jsonl'
    assert exit_code == 0
    assert capsys.readouterr().out == 'recs-session/session-record.jsonl\n'
    assert destination.read_text() == '{"type":"header"}\n'
