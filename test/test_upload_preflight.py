import json
from pathlib import Path

import pytest

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
            'destinations': {'archive': {'kind': 's3', 'bucket': 'archive'}},
            'uploads': [
                {
                    'name': 'archive',
                    'match': 'True',
                    'encoding': {'format': 'source'},
                    'destination': 'archive',
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
