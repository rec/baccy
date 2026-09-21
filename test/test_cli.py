import json
from pathlib import Path

import pytest
from pytest import CaptureFixture, MonkeyPatch

from baccy.cli import main
from baccy.models import ResolvedSource, SourceSelection, VolumeSource


class NoNetworkDiscovery:
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
    destination = (
        tmp_path
        / 'Backups'
        / 'baccy'
        / 'sources'
        / 'removable-test-uuid'
        / 'recs-session'
        / 'session-record.jsonl'
    )
    assert exit_code == 0
    assert output['copied'] == 1
    assert destination.read_text() == '{"type":"header"}\n'
