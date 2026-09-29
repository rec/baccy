from pathlib import Path

import pytest

from baccy.models import Settings
from baccy.sync import sync


def test_sync_uses_remote_names_without_hashing_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = tmp_path / 'backup'
    session = backup / 'audio' / 'concert' / '2026' / '09' / '24' / '20-00-00'
    session.mkdir(parents=True)
    (session / 'audio.flac').write_bytes(b'audio')
    (session / 'session-record.jsonl').write_text(
        '{"type":"file_started","media_type":"audio","stream_id":"mic",'
        '"timestamp":"2026-09-24T20:00:00Z","format":"flac",'
        '"source":"device","source_channels":[1],"path":"audio.flac"}\n'
        '{"type":"file_finished","media_type":"audio","stream_id":"mic",'
        '"path":"audio.flac","frame_count":48000,"sample_rate":48000}\n'
    )
    settings = Settings.model_validate(
        {
            'backup_root': backup,
            'uploads': [
                {
                    'name': 'archive',
                    'match': 'True',
                    'encoding': {'format': 'source'},
                    'destination': 'ssh:host:/srv/recs',
                }
            ],
        }
    )
    monkeypatch.setattr(
        'baccy.upload._remote_targets',
        lambda destination: {'concert/2026/09/24/20-00-00/audio.flac': 5},
    )
    monkeypatch.setattr(
        'baccy.upload_plan._source_hash',
        lambda path: pytest.fail('sync must not hash sources'),
    )

    result = sync([Path('concert')], settings)

    assert result.unchanged == 1
    assert result.uploaded == 0

    monkeypatch.setattr(
        'baccy.upload._remote_targets',
        lambda destination: {'concert/2026/09/24/20-00-00/audio.flac': 3},
    )
    monkeypatch.setattr('baccy.upload._upload', lambda plan, path: True)

    mismatched = sync([Path('concert')], settings)

    assert mismatched.uploaded == 1

    monkeypatch.setattr(
        'baccy.upload._remote_targets',
        lambda destination: pytest.fail('dry-run sync must not contact destinations'),
    )

    dry_run = sync([Path('concert')], settings, dry_run=True)

    assert dry_run.would_upload == 1
