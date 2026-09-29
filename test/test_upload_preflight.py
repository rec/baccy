import json
import subprocess
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from baccy.catalog import Catalog
from baccy.models import PathSource, ResolvedSource, Settings
from baccy.upload import publish_sessions


def test_upload_reports_every_missing_source_before_contacting_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    session = root / 'project' / 'session'
    session.mkdir(parents=True)
    records = []
    for name in ['first.flac', 'second.flac']:
        records.extend(
            [
                {
                    'type': 'file_started',
                    'media_type': 'audio',
                    'stream_id': name,
                    'timestamp': '2026-09-24T20:00:00Z',
                    'format': 'flac',
                    'source': 'device',
                    'source_channels': [1],
                    'path': f'audio/{name}',
                },
                {
                    'type': 'file_finished',
                    'media_type': 'audio',
                    'stream_id': name,
                    'path': f'audio/{name}',
                    'frame_count': 48_000,
                    'sample_rate': 48_000,
                },
            ]
        )
    (session / 'session-record.jsonl').write_text(
        ''.join(json.dumps(record) + '\n' for record in records)
    )
    settings = Settings.model_validate(
        {
            'backup_root': tmp_path / 'backup',
            'uploads': [
                {
                    'name': 'archive',
                    'match': 'True',
                    'encoding': {'format': 'source'},
                    'destination': 's3:archive',
                }
            ],
        }
    )
    monkeypatch.setattr(
        'baccy.upload._upload',
        lambda plan, path: pytest.fail('preflight must not upload'),
    )

    source = ResolvedSource(
        source=PathSource(kind='path', name='audio', path=root), root=root
    )
    results = publish_sessions([source], settings, dry_run=False)

    assert [result.relative_path for result in results] == [
        Path('project/session/audio/first.flac'),
        Path('project/session/audio/second.flac'),
    ]
    assert [result.status for result in results] == ['failed', 'failed']


def test_unchanged_publication_reuses_recorded_source_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    session = root / 'project' / 'session'
    _session(session, 'audio.flac')
    (session / 'audio.flac').write_bytes(b'audio')
    monkeypatch.setattr('baccy.upload._upload', lambda plan, path: True)
    settings = _settings(tmp_path)
    source = _source(root)

    assert publish_sessions([source], settings, dry_run=False)[0].status == 'uploaded'
    monkeypatch.setattr(
        'baccy.upload._source_hash',
        lambda path: pytest.fail('unchanged source should not be rehashed'),
    )

    assert publish_sessions([source], settings, dry_run=False)[0].status == 'unchanged'


def test_missing_source_does_not_block_another_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    missing = root / 'project' / 'missing'
    present = root / 'project' / 'present'
    _session(missing, 'missing.flac')
    _session(present, 'present.flac')
    (present / 'present.flac').write_bytes(b'audio')
    settings = _settings(tmp_path)
    monkeypatch.setattr('baccy.upload._upload', lambda plan, path: True)

    results = publish_sessions([_source(root)], settings, dry_run=False)

    assert [(result.relative_path, result.status) for result in results] == [
        (Path('project/missing/missing.flac'), 'failed'),
        (Path('project/present/present.flac'), 'uploaded'),
    ]


def test_upload_rejects_collisions_between_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    for name in ('first', 'second'):
        session = root / 'project' / name
        _session(session, f'{name} + 20260924-200000.flac')
        (session / f'{name} + 20260924-200000.flac').write_bytes(b'audio')
    settings = Settings.model_validate(
        {
            'backup_root': tmp_path / 'backup',
            'uploads': [
                {
                    'name': 'mp3',
                    'match': 'True',
                    'encoding': {'format': 'mp3', 'bitrate_kbps': 128},
                    'destination': 's3:archive',
                }
            ],
        }
    )
    monkeypatch.setattr(
        'baccy.upload._upload', lambda plan, path: pytest.fail('colliding upload')
    )

    results = publish_sessions([_source(root)], settings, dry_run=True, sync=True)

    assert [result.status for result in results] == ['deferred', 'deferred']
    assert {result.relative_path for result in results} == {
        Path('project/20260924-200000.mp3')
    }


def test_upload_rejects_a_landing_page_target_collision(tmp_path: Path) -> None:
    root = tmp_path / 'audio'
    session = root / 'project' / 'session'
    _session(session, 'index.html')
    (session / 'index.html').write_bytes(b'audio')
    settings = Settings.model_validate(
        {
            'backup_root': tmp_path / 'backup',
            'uploads': [
                {
                    'name': 'archive',
                    'match': 'True',
                    'encoding': {'format': 'source'},
                    'destination': 's3:archive',
                }
            ],
            'landing_pages': [{'upload': 'archive', 'destination': 's3:archive'}],
        }
    )

    results = publish_sessions([_source(root)], settings, dry_run=True, sync=True)

    assert [result.status for result in results] == ['deferred', 'deferred']
    assert {result.relative_path for result in results} == {
        Path('project/session/index.html')
    }


def test_landing_pages_in_separate_directories_upload_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    for name in ('first', 'second'):
        session = root / 'project' / name
        _session(session, 'audio.flac')
        (session / 'audio.flac').write_bytes(b'audio')
    settings = Settings.model_validate(
        _settings(tmp_path).model_dump()
        | {'landing_pages': [{'upload': 'archive', 'destination': 's3:archive'}]}
    )
    monkeypatch.setattr('baccy.upload._upload', lambda plan, path: True)
    monkeypatch.setattr('baccy.upload._upload_landing_page', lambda plan, path: True)

    results = publish_sessions([_source(root)], settings, dry_run=False)

    assert [(result.relative_path, result.status) for result in results] == [
        (Path('project/first/audio.flac'), 'uploaded'),
        (Path('project/second/audio.flac'), 'uploaded'),
        (Path('project/first/index.html'), 'uploaded'),
        (Path('project/second/index.html'), 'uploaded'),
    ]


def test_landing_page_waits_for_failed_audio_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    session = root / 'project' / 'session'
    _session(session, 'audio.flac')
    (session / 'audio.flac').write_bytes(b'audio')
    settings = Settings.model_validate(
        _settings(tmp_path).model_dump()
        | {'landing_pages': [{'upload': 'archive', 'destination': 's3:archive'}]}
    )

    def failed_upload(plan: object, path: Path) -> bool:
        raise OSError('upload failed')

    monkeypatch.setattr('baccy.upload._upload', failed_upload)
    monkeypatch.setattr(
        'baccy.upload._upload_landing_page',
        lambda plan, path: pytest.fail('landing page must be deferred'),
    )

    results = publish_sessions([_source(root)], settings, dry_run=False)

    assert [result.status for result in results] == ['failed', 'deferred']
    assert results[1].detail == 'linked audio upload is incomplete'


def test_upload_reports_failed_catalog_write_after_remote_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    session = root / 'project' / 'session'
    _session(session, 'audio.flac')
    (session / 'audio.flac').write_bytes(b'audio')
    monkeypatch.setattr('baccy.upload._upload', lambda plan, path: True)

    def failed_append(self: Catalog, value: dict[str, object]) -> None:
        raise OSError('catalog unavailable')

    monkeypatch.setattr(Catalog, 'append', failed_append)

    results = publish_sessions([_source(root)], _settings(tmp_path), dry_run=False)

    assert [result.status for result in results] == ['failed']
    assert results[0].detail == (
        'remote file present; catalog update failed: catalog unavailable'
    )


def test_encoder_timeout_is_reported_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    session = root / 'project' / 'session'
    _session(session, 'audio.flac')
    (session / 'audio.flac').write_bytes(b'audio')
    settings = Settings.model_validate(
        {
            'backup_root': tmp_path / 'backup',
            'uploads': [
                {
                    'name': 'mp3',
                    'match': 'True',
                    'encoding': {'format': 'mp3', 'bitrate_kbps': 128},
                    'destination': 'ssh:host:/srv/audio',
                }
            ],
        }
    )

    def timeout(command: list[str], **kwargs: object) -> None:
        assert kwargs['timeout'] == 4 * 60 * 60
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr('baccy.upload.subprocess.run', timeout)

    results = publish_sessions([_source(root)], settings, dry_run=False)

    assert [result.status for result in results] == ['failed']
    assert 'timed out' in str(results[0].detail)


def test_failed_remote_listing_does_not_block_another_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    session = root / 'project' / 'session'
    _session(session, 'audio.flac')
    (session / 'audio.flac').write_bytes(b'audio')
    settings = Settings.model_validate(
        {
            'backup_root': tmp_path / 'backup',
            'uploads': [
                {
                    'name': 'bad',
                    'match': 'True',
                    'encoding': {'format': 'source'},
                    'destination': 'ssh:bad:/srv/audio',
                },
                {
                    'name': 'good',
                    'match': 'True',
                    'encoding': {'format': 'source'},
                    'destination': 'ssh:good:/srv/audio',
                },
            ],
        }
    )

    def targets(destination: object) -> dict[str, int]:
        if 'bad' in str(destination):
            raise OSError('remote listing failed')
        return {'project/session/audio.flac': 5}

    monkeypatch.setattr('baccy.upload._remote_targets', targets)

    results = publish_sessions([_source(root)], settings, dry_run=False, sync=True)

    assert [result.status for result in results] == ['failed', 'unchanged']


def test_ssh_listing_failure_is_not_treated_as_an_empty_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    session = root / 'project' / 'session'
    _session(session, 'audio.flac')
    (session / 'audio.flac').write_bytes(b'audio')
    settings = Settings.model_validate(
        {
            'backup_root': tmp_path / 'backup',
            'uploads': [
                {
                    'name': 'archive',
                    'match': 'True',
                    'encoding': {'format': 'source'},
                    'destination': 'ssh:host:/srv/audio',
                }
            ],
        }
    )
    monkeypatch.setattr(
        'baccy.upload.subprocess.run',
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 255, b'', b'host key verification failed'
        ),
    )
    monkeypatch.setattr(
        'baccy.upload._upload', lambda plan, path: pytest.fail('must not upload')
    )

    results = publish_sessions([_source(root)], settings, dry_run=False, sync=True)

    assert [result.status for result in results] == ['failed']
    assert results[0].detail == 'host key verification failed'


def test_s3_sync_uses_recorded_identity_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    session = root / 'project' / 'session'
    _session(session, 'audio.flac')
    (session / 'audio.flac').write_bytes(b'audio')
    settings = _settings(tmp_path)

    class FakeS3:
        def __init__(self) -> None:
            self.objects: dict[str, dict[str, object]] = {}
            self.uploads = 0

        def head_object(self, Bucket: str, Key: str) -> dict[str, object]:
            if Key not in self.objects:
                raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
            return self.objects[Key]

        def upload_file(
            self,
            Filename: str,
            Bucket: str,
            Key: str,
            Config: object,
            ExtraArgs: dict[str, object],
        ) -> None:
            self.uploads += 1
            self.objects[Key] = {
                'ContentLength': Path(Filename).stat().st_size,
                'Metadata': ExtraArgs['Metadata'],
            }

    client = FakeS3()
    monkeypatch.setattr('baccy.upload.s3_client', lambda destination: client)
    monkeypatch.setattr(
        'baccy.upload._remote_targets',
        lambda destination: {'project/session/audio.flac': 5},
    )
    source = _source(root)

    assert publish_sessions([source], settings, dry_run=False)[0].status == 'uploaded'
    assert publish_sessions([source], settings, dry_run=False, sync=True)[0].status == (
        'unchanged'
    )
    assert client.uploads == 1

    client.objects['project/session/audio.flac']['Metadata'] = {
        'baccy-identity': 'wrong'
    }
    assert publish_sessions([source], settings, dry_run=False, sync=True)[0].status == (
        'uploaded'
    )
    assert client.uploads == 2


def test_verified_sync_detects_same_size_remote_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    session = root / 'project' / 'session'
    _session(session, 'audio.flac')
    (session / 'audio.flac').write_bytes(b'audio')
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        'baccy.upload._remote_targets',
        lambda destination: {'project/session/audio.flac': 5},
    )
    uploads: list[Path] = []
    monkeypatch.setattr(
        'baccy.upload._upload',
        lambda plan, path: uploads.append(path) or True,
    )
    monkeypatch.setattr(
        'baccy.upload._remote_hash', lambda destination, target: '0' * 64
    )

    results = publish_sessions(
        [_source(root)], settings, dry_run=False, sync=True, verify=True
    )

    assert [result.status for result in results] == ['uploaded']
    assert uploads == [session / 'audio.flac']


def test_verified_sync_accepts_matching_remote_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'audio'
    session = root / 'project' / 'session'
    _session(session, 'audio.flac')
    (session / 'audio.flac').write_bytes(b'audio')
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        'baccy.upload._remote_targets',
        lambda destination: {'project/session/audio.flac': 5},
    )

    class Body:
        closed = False

        def iter_chunks(self, chunk_size: int) -> list[bytes]:
            return [b'aud', b'io']

        def close(self) -> None:
            self.closed = True

    body = Body()

    class Client:
        def get_object(self, Bucket: str, Key: str) -> dict[str, object]:
            assert (Bucket, Key) == ('archive', 'project/session/audio.flac')
            return {'Body': body}

    monkeypatch.setattr('baccy.upload.s3_client', lambda destination: Client())
    monkeypatch.setattr(
        'baccy.upload._upload', lambda plan, path: pytest.fail('must not upload')
    )

    results = publish_sessions(
        [_source(root)], settings, dry_run=False, sync=True, verify=True
    )

    assert [result.status for result in results] == ['unchanged']
    assert body.closed


def _session(path: Path, audio_name: str) -> None:
    path.mkdir(parents=True)
    records = [
        {
            'type': 'file_started',
            'media_type': 'audio',
            'stream_id': 'mic',
            'timestamp': '2026-09-24T20:00:00Z',
            'format': 'flac',
            'source': 'device',
            'source_channels': [1],
            'path': audio_name,
        },
        {
            'type': 'file_finished',
            'media_type': 'audio',
            'stream_id': 'mic',
            'path': audio_name,
            'frame_count': 48_000,
            'sample_rate': 48_000,
        },
    ]
    (path / 'session-record.jsonl').write_text(
        ''.join(json.dumps(record) + '\n' for record in records)
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            'backup_root': tmp_path / 'backup',
            'uploads': [
                {
                    'name': 'archive',
                    'match': 'True',
                    'encoding': {'format': 'source'},
                    'destination': 's3:archive',
                }
            ],
        }
    )


def _source(root: Path) -> ResolvedSource:
    return ResolvedSource(
        source=PathSource(kind='path', name='audio', path=root), root=root
    )
