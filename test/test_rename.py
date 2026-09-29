import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from pytest import CaptureFixture, MonkeyPatch

from baccy import rename
from baccy.models import Encoding, S3Destination, Settings, UploadRule
from baccy.rename import RemoteRename, RenameFile, _log, rename_files, renamed_files
from baccy.sync import sync
from baccy.upload_plan import ArtifactPlan, Segment


def test_rename_lists_only_direct_s3_uploads(monkeypatch: MonkeyPatch) -> None:
    source = ArtifactPlan(
        project='totm',
        source_name='backup',
        session=Path('totm/session'),
        segment=Segment(
            path=Path('audio/MacBook.flac'),
            timestamp='2026-09-26T10:40:08Z',
            source='device',
            channels=[1],
            frame_count=48_000,
            sample_rate=48_000,
            track='main',
            format='flac',
        ),
        rule=UploadRule(
            name='source',
            match='True',
            encoding=Encoding(format='source'),
            destination='s3:archive',
        ),
        destination=S3Destination(kind='s3', bucket='archive'),
        target=Path('totm/session/audio/MacBook.flac'),
        identity='source',
        source_hash='source',
    )
    monkeypatch.setattr(
        'baccy.rename.planned_source_uploads', lambda settings: [source]
    )
    monkeypatch.setattr('baccy.rename.s3_endpoint_url', lambda destination: None)

    files = renamed_files(Settings(), 'MacBook', 'Mic', False)

    assert [value.model_dump() for value in files] == [
        {
            'session': Path('totm/session'),
            'source': Path('totm/session/audio/MacBook.flac'),
            'replacement': Path('totm/session/audio/Mic.flac'),
            'targets': [
                {
                    'destination': {
                        'kind': 's3',
                        'bucket': 'archive',
                        'endpoint_url': None,
                        'prefix': '',
                        'max_bandwidth': 1_000_000,
                    },
                    'source_key': 'totm/session/audio/MacBook.flac',
                    'replacement_key': 'totm/session/audio/Mic.flac',
                }
            ],
        }
    ]


def test_rename_uses_regular_expressions_only_when_requested(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        'baccy.rename.planned_source_uploads',
        lambda settings: [
            ArtifactPlan(
                project='totm',
                source_name='backup',
                session=Path('totm/session'),
                segment=Segment(
                    path=Path('audio/Microphone + 1.flac'),
                    timestamp='2026-09-26T10:40:08Z',
                    source='device',
                    channels=[1],
                    frame_count=48_000,
                    sample_rate=48_000,
                    track='main',
                    format='flac',
                ),
                rule=UploadRule(
                    name='source',
                    match='True',
                    encoding=Encoding(format='source'),
                    destination='s3:archive',
                ),
                destination=S3Destination(kind='s3', bucket='archive'),
                target=Path('totm/session/audio/Microphone + 1.flac'),
                identity='source',
                source_hash='source',
            )
        ],
    )
    monkeypatch.setattr('baccy.rename.s3_endpoint_url', lambda destination: None)

    files = renamed_files(Settings(), 'Microphone + 1', 'Mic', False)

    assert files[0].replacement.name == 'Mic.flac'
    assert renamed_files(Settings(), 'Microphone + 1', 'Mic', True) == []


def test_rename_logs_without_writing_to_standard_output(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    _log(tmp_path, {'ok': True})

    assert capsys.readouterr().out == ''
    assert json.loads((tmp_path / 'events.jsonl').read_text())['ok'] is True


def test_rename_rejects_a_file_missing_from_its_session(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    session = Path('totm/session')
    source = session / 'audio/old.flac'
    replacement = session / 'audio/new.flac'
    root = tmp_path / 'audio'
    (root / source).parent.mkdir(parents=True)
    (root / source).write_text('audio')
    (root / session / 'session-record.jsonl').write_text(
        '{"path":"audio/other.flac"}\n'
    )
    with pytest.raises(ValueError, match='session does not name the file'):
        rename_files(
            Settings(backup_root=tmp_path),
            [
                RenameFile(
                    session=session,
                    source=source,
                    replacement=replacement,
                    targets=[],
                )
            ],
            'old',
            'new',
        )

    assert not (tmp_path / 'events.jsonl').exists()


def test_rename_uses_planned_web_safe_key_and_prefix(monkeypatch: MonkeyPatch) -> None:
    plan = ArtifactPlan(
        project='totm',
        source_name='backup',
        session=Path('totm/session'),
        segment=Segment(
            path=Path('audio/MacBook.flac'),
            timestamp='2026-09-26T10:40:08Z',
            source='device',
            channels=[1],
            frame_count=48_000,
            sample_rate=48_000,
            track='main',
            format='flac',
        ),
        rule=UploadRule(
            name='source',
            match='True',
            encoding=Encoding(format='source'),
            destination='s3:archive',
        ),
        destination=S3Destination(kind='s3', bucket='archive', prefix='backups'),
        target=Path('totm/session/audio/MacBook-legal.flac'),
        identity='source',
        source_hash='source',
    )
    monkeypatch.setattr('baccy.rename.planned_source_uploads', lambda settings: [plan])
    monkeypatch.setattr('baccy.rename.s3_endpoint_url', lambda destination: None)

    files = renamed_files(Settings(), 'MacBook', 'Mic', False)

    assert files[0].targets[0].source_key == (
        'backups/totm/session/audio/MacBook-legal.flac'
    )
    assert files[0].targets[0].replacement_key == (
        'backups/totm/session/audio/Mic.flac'
    )


def test_rename_resumes_after_progress_write_fails(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    session = Path('totm/session')
    source = session / 'audio/old.flac'
    replacement = session / 'audio/new.flac'
    root = tmp_path / 'audio'
    (root / source).parent.mkdir(parents=True)
    (root / source).write_bytes(b'audio')
    journal = root / session / 'session-record.jsonl'
    journal.write_text('{"type":"file_finished","path":"audio/old.flac"}\n')
    destination = S3Destination(kind='s3', bucket='archive')
    file = RenameFile(
        session=session,
        source=source,
        replacement=replacement,
        targets=[
            RemoteRename(
                destination=destination,
                source_key=source.as_posix(),
                replacement_key=replacement.as_posix(),
            )
        ],
    )

    class FakeS3:
        def __init__(self) -> None:
            self.objects = {
                source.as_posix(): {
                    'ContentLength': 5,
                    'Metadata': {'baccy-identity': 'original'},
                }
            }
            self.fail_copy = False

        def head_object(self, Bucket: str, Key: str) -> dict[str, object]:
            if Key not in self.objects:
                raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
            return self.objects[Key]

        def copy(
            self,
            Bucket: str,
            Key: str,
            CopySource: dict[str, str],
            Config: object,
        ) -> None:
            if self.fail_copy:
                raise ClientError({'Error': {'Code': 'ServiceUnavailable'}}, 'Copy')
            self.objects[Key] = self.objects[CopySource['Key']].copy()

        def delete_object(self, Bucket: str, Key: str) -> None:
            assert (root / replacement).is_file()
            assert 'audio/new.flac' in journal.read_text()
            del self.objects[Key]

    client = FakeS3()
    monkeypatch.setattr('baccy.rename.s3_client', lambda destination: client)
    client.objects[source.as_posix()]['ContentLength'] = 4
    assert rename_files(Settings(backup_root=tmp_path), [file], 'old', 'new') is False
    assert not (tmp_path / 'rename-progress.json').exists()
    assert 'S3 source size differs' in (tmp_path / 'events.jsonl').read_text()
    client.objects[source.as_posix()]['ContentLength'] = 5
    client.fail_copy = True
    assert rename_files(Settings(backup_root=tmp_path), [file], 'old', 'new') is False
    assert (root / source).is_file()
    assert 'audio/old.flac' in journal.read_text()
    assert source.as_posix() in client.objects
    assert replacement.as_posix() not in client.objects
    client.fail_copy = False
    original_write = rename._write_progress
    failed = False

    def interrupted_write(root: Path, progress: rename.RenameProgress) -> None:
        nonlocal failed
        if progress.copied and not failed:
            failed = True
            raise OSError('disk temporarily unavailable')
        original_write(root, progress)

    monkeypatch.setattr('baccy.rename._write_progress', interrupted_write)

    assert rename_files(Settings(backup_root=tmp_path), [file], 'old', 'new') is False
    assert (tmp_path / 'rename-progress.json').is_file()
    assert source.as_posix() in client.objects
    assert replacement.as_posix() in client.objects
    assert renamed_files(Settings(backup_root=tmp_path), 'old', 'new', False) == [file]
    with pytest.raises(ValueError, match='rename is pending'):
        sync([], Settings(backup_root=tmp_path))

    assert rename_files(Settings(backup_root=tmp_path), [file], 'old', 'new') is True
    assert not (tmp_path / 'rename-progress.json').exists()
    assert not (root / source).exists()
    assert (root / replacement).is_file()
    assert source.as_posix() not in client.objects
