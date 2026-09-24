import json
import tomllib
from pathlib import Path

import pytest
from pytest import CaptureFixture, MonkeyPatch
from reccy.services.models import StatusResult

from baccy.cli import main
from baccy.models import BackupSummary, ResolvedSource, SourceSelection, VolumeSource


class NoNetworkDiscovery:
    new_machines: list[object] = []

    def discover(self) -> list[object]:
        return []


@pytest.fixture(autouse=True)
def disable_network_discovery(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr('baccy.backup.NetworkDiscovery', NoNetworkDiscovery)


def test_backup_command_runs_one_pass(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'recording.toml').write_text('format = "recs"\n')
    config = tmp_path / 'baccy.toml'
    config.write_text(
        f'backup_root = "{tmp_path / "backup"}"\n'
        'stability_seconds = 0\n'
        '[[sources]]\n'
        'kind = "path"\n'
        'name = "source"\n'
        f'path = "{source}"\n'
    )

    exit_code = main(['backup', '--config', str(config)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output['copied'] == 1

    exit_code = main(['backup', '--config', str(config)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output['unchanged'] == 1
    assert output['results'][0]['status'] == 'unchanged'


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
        'verbose = true\n'
        '[[sources]]\n'
        'kind = "path"\n'
        'name = "source"\n'
        f'path = "{source}"\n'
    )

    main(['backup', '--config', str(config)])
    capsys.readouterr()
    main(['backup', '--config', str(config)])

    output = json.loads(capsys.readouterr().out)
    assert output['unchanged'] == 1
    assert output['results'][0]['status'] == 'unchanged'


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
        report(BackupSummary(copied=1))

    monkeypatch.setattr('baccy.cli.watch', run_watch)

    assert main(['watch', '--config', str(config)]) == 0

    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        BackupSummary().model_dump(mode='json'),
        BackupSummary(copied=1).model_dump(mode='json'),
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
        '[[sources]]\n'
        'kind = "path"\n'
        'name = "source"\n'
        f'path = "{source}"\n'
    )

    exit_code = main(['backup', flag, '--config', str(config)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output['would_copy'] == 1
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

    output = json.loads(capsys.readouterr().out)
    destination = tmp_path / 'baccy' / 'audio' / 'recs-session' / 'session-record.jsonl'
    assert exit_code == 0
    assert output['copied'] == 1
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

    output = json.loads(capsys.readouterr().out)
    destination = backup / 'audio' / 'concert' / '2026' / '09' / '24' / '20-00-00'
    assert output['copied'] == 1
    assert (destination / 'session-record.jsonl').exists()
    assert not session.exists()


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
