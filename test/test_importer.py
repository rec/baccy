from pathlib import Path

import pytest

from baccy.importer import import_recs
from baccy.models import Settings


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('project', ['../escape', '/outside', '.', '..'])
def test_import_rejects_project_paths(
    tmp_path: Path, dry_run: bool, project: str
) -> None:
    settings = Settings(backup_root=tmp_path / 'backup')

    with pytest.raises(ValueError, match='single path component'):
        import_recs(
            [], settings, copy_directories=False, project=project, dry_run=dry_run
        )

    assert not settings.backup_root.exists()


def test_failed_import_does_not_publish_partial_session_or_remove_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / 'input' / 'session'
    session.mkdir(parents=True)
    (session / 'session-record.jsonl').write_text(
        '{"type":"header","project_name":"project",'
        '"started_at":"2026-09-29T12:00:00Z"}\n'
    )
    (session / 'audio.flac').write_bytes(b'audio')
    settings = Settings(backup_root=tmp_path / 'backup')

    def partial_copy(source: Path, destination: Path, **kwargs: object) -> None:
        destination.mkdir()
        (destination / 'audio.flac').write_bytes(b'partial')
        raise OSError('copy interrupted')

    monkeypatch.setattr('baccy.importer.shutil.copytree', partial_copy)

    with pytest.raises(OSError, match='copy interrupted'):
        import_recs([session], settings, copy_directories=False, project=None)

    assert (session / 'audio.flac').read_bytes() == b'audio'
    assert not (
        settings.backup_root / 'audio' / 'project' / '2026/09/29/session'
    ).exists()
    assert not list(settings.backup_root.rglob('.baccy-import-*'))


def test_import_rejects_incomplete_staged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / 'input' / 'session'
    session.mkdir(parents=True)
    (session / 'session-record.jsonl').write_text(
        '{"type":"header","project_name":"project",'
        '"started_at":"2026-09-29T12:00:00Z"}\n'
    )
    (session / 'audio.flac').write_bytes(b'audio')
    settings = Settings(backup_root=tmp_path / 'backup')

    def incomplete_copy(source: Path, destination: Path, **kwargs: object) -> None:
        destination.mkdir()
        (destination / 'audio.flac').write_bytes(b'aud')

    monkeypatch.setattr('baccy.importer.shutil.copytree', incomplete_copy)

    with pytest.raises(OSError, match='differs from source'):
        import_recs([session], settings, copy_directories=False, project=None)

    assert (session / 'audio.flac').read_bytes() == b'audio'
    assert not (
        settings.backup_root / 'audio' / 'project' / '2026/09/29/session'
    ).exists()
