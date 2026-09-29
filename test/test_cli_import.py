from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from baccy.cli import main


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

    assert main(['--config', str(config), 'import', str(source)]) == 0

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

    assert main(['--config', str(config), 'import', str(source)]) == 0


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
                '--config',
                str(config),
                'import',
                str(session),
                '--copy',
                '--project',
                'concert',
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

    assert main(['--dry-run', '--config', str(config), 'import', str(source)]) == 0

    assert capsys.readouterr().out == 'audio/concert/2026/09/24/20-00-00\n'
    assert session.exists()
    assert not backup.exists()
