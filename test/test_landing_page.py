import json
import subprocess
from pathlib import Path

import pytest

from baccy.models import PathSource, ResolvedSource, Settings
from baccy.upload import publish_sessions


def test_landing_page_upload_uses_default_template_and_mp3_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'recs'
    session = root / 'project' / 'session'
    audio = session / 'audio' / 'master + 20260920-120000.flac'
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b'audio')
    (session / 'session-record.jsonl').write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'file_started',
                        'media_type': 'audio',
                        'stream_id': 'master',
                        'timestamp': '2026-09-20T12:00:00Z',
                        'format': 'flac',
                        'source': 'device',
                        'track_name': 'master',
                        'source_channels': [1],
                        'path': 'audio/master + 20260920-120000.flac',
                    }
                ),
                json.dumps(
                    {
                        'type': 'file_finished',
                        'media_type': 'audio',
                        'stream_id': 'master',
                        'path': 'audio/master + 20260920-120000.flac',
                        'frame_count': 5_808_000,
                        'sample_rate': 48_000,
                    }
                ),
            ]
        )
        + '\n'
    )
    settings = Settings.model_validate(
        {
            'backup_root': tmp_path / 'backup',
            'uploads': [
                {
                    'name': 'main-mp3',
                    'match': 'main and duration > 120',
                    'encoding': {'format': 'mp3', 'bitrate_kbps': 128},
                    'destination': 'ssh:host:/srv/site',
                }
            ],
            'landing_pages': [
                {
                    'upload': 'main-mp3',
                    'destination': 'ssh:host:/srv/site',
                }
            ],
        }
    )
    source = ResolvedSource(
        source=PathSource(kind='path', name='recs', path=root), root=root
    )
    monkeypatch.setattr(
        'baccy.upload_plan._load_project',
        lambda name: (_ for _ in ()).throw(AssertionError(name)),
    )
    encoded: list[Path] = []

    def encode(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        output = Path(command[-1])
        output.write_bytes(b'mp3')
        encoded.append(output)
        return subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr('baccy.upload.subprocess.run', encode)
    monkeypatch.setattr('baccy.upload._run', lambda command: None)

    results = publish_sessions([source], settings, dry_run=False)

    assert [(result.relative_path, result.status) for result in results] == [
        (Path('project/20260920-120000.mp3'), 'uploaded'),
        (Path('project/index.html'), 'uploaded'),
    ]
    assert encoded[0].parent == Path('/tmp')
    assert not encoded[0].exists()
    page = next((tmp_path / 'backup' / 'artifacts').glob('*/index.html'))
    expected = Path(__file__).parent / 'fixtures' / 'landing.html'
    assert page.read_text() == expected.read_text()
