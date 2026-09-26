import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from baccy.match import MatchExpression
from baccy.models import PathSource, ResolvedSource, Settings
from baccy.sync import sync
from baccy.upload import publish_sessions


def test_upload_rules_prefer_named_main_track_and_skip_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'recs'
    session = root / 'project' / '2026-09-20' / '12-00-00'
    session.mkdir(parents=True)
    records: list[dict[str, object]] = []
    for channel in range(1, 5):
        path = f'audio/{channel}.flac'
        audio = session / path
        audio.parent.mkdir(exist_ok=True)
        audio.write_bytes(b'audio')
        records.extend(
            [
                {
                    'type': 'file_started',
                    'media_type': 'audio',
                    'stream_id': str(channel),
                    'timestamp': '2026-09-20T12:00:00Z',
                    'format': 'flac',
                    'source': 'device',
                    'track_name': 'master mix' if channel == 1 else f'track {channel}',
                    'source_channels': [channel],
                    'path': path,
                },
                {
                    'type': 'file_finished',
                    'media_type': 'audio',
                    'stream_id': str(channel),
                    'path': path,
                    'frame_count': 0 if channel == 1 else 5_808_000,
                    'sample_rate': 48_000,
                },
            ]
        )
    (session / 'session-record.jsonl').write_text(
        ''.join(json.dumps(record) + '\n' for record in records)
    )
    calls: list[list[str]] = []
    monkeypatch.setattr('baccy.upload._run', lambda command: calls.append(command))
    source = ResolvedSource(
        source=PathSource(kind='path', name='recs', path=root), root=root
    )
    settings = Settings.model_validate(
        {
            'backup_root': tmp_path / 'backup',
            'uploads': [
                {
                    'name': 'main',
                    'match': 'main and duration > 120',
                    'encoding': {'format': 'source'},
                    'destination': 'ssh:user@host:/srv/recs',
                }
            ],
        }
    )

    writes = []
    first = publish_sessions([source], settings, False, on_write=writes.append)
    second = publish_sessions([source], settings, False)

    assert [result.status for result in first] == ['uploaded']
    assert [result.status for result in second] == ['unchanged']
    assert [result.status for result in writes] == ['writing']
    assert [call[0] for call in calls].count('scp') == 1
    assert [call[0] for call in calls].count('ssh') == 1
    warning = (
        'warning: ignoring zero frame count in completed audio record: '
        f'{session / "audio/1.flac"}\n'
    )
    assert capsys.readouterr().err == warning * 2
    events = [
        json.loads(line)
        for line in (tmp_path / 'backup' / 'events.jsonl').read_text().splitlines()
    ]
    assert {event['rule'] for event in events} == {'main'}


def test_upload_rules_include_main_tracks_on_dry_run(tmp_path: Path) -> None:
    root = tmp_path / 'recs'
    session = root / 'project' / 'session'
    session.mkdir(parents=True)
    (session / 'audio/master + 20260920-120000.wav').parent.mkdir()
    (session / 'audio/master + 20260920-120000.wav').write_bytes(b'audio')
    (session / 'session-record.jsonl').write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'file_started',
                        'media_type': 'audio',
                        'stream_id': 'mic',
                        'timestamp': '2026-09-20T12:00:00Z',
                        'format': 'wav',
                        'source': 'device',
                        'source_channels': [1],
                        'path': 'audio/master + 20260920-120000.wav',
                    }
                ),
                json.dumps(
                    {
                        'type': 'file_finished',
                        'media_type': 'audio',
                        'stream_id': 'mic',
                        'path': 'audio/master + 20260920-120000.wav',
                        'frame_count': 48_000,
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
                    'name': 'main',
                    'match': 'True',
                    'encoding': {'format': 'mp3', 'bitrate_kbps': 128},
                    'destination': 'ssh:host:/srv/recs',
                }
            ],
        }
    )
    source = ResolvedSource(
        source=PathSource(kind='path', name='recs', path=root), root=root
    )

    results = publish_sessions([source], settings, True)

    assert [(result.relative_path, result.status) for result in results] == [
        (Path('project/20260920-120000.mp3'), 'would_upload')
    ]
    assert not (tmp_path / 'backup').exists()


def test_upload_accepts_compact_recs_v5_audio_records(tmp_path: Path) -> None:
    root = tmp_path / 'recs'
    session = root / 'project' / 'session'
    audio = session / 'audio' / 'mic.flac'
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b'audio')
    (session / 'session-record.jsonl').write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'source_online',
                        'timestamp': '2026-09-24T20:00:00Z',
                        'source': 'Mic',
                        'clock_id': 'mic-clock',
                        'channel_count': 1,
                        'sample_rate': 48_000,
                    }
                ),
                json.dumps(
                    {
                        'type': 'file_started',
                        'timestamp': '2026-09-24T20:00:00Z',
                        'stream_id': 'audio:Mic:1',
                        'clock_id': 'mic-clock',
                        'path': 'audio/mic.flac',
                        'source_channels': [1],
                    }
                ),
                json.dumps(
                    {
                        'type': 'file_finished',
                        'timestamp': '2026-09-24T20:01:00Z',
                        'stream_id': 'audio:Mic:1',
                        'clock_id': 'mic-clock',
                        'path': 'audio/mic.flac',
                        'source_channels': [1],
                        'frame_count': 48_000,
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
                    'name': 'archive',
                    'match': 'format == "flac" and device == "Mic"',
                    'encoding': {'format': 'source'},
                    'destination': 's3:archive',
                }
            ],
        }
    )
    source = ResolvedSource(
        source=PathSource(kind='path', name='recs', path=root), root=root
    )

    results = publish_sessions([source], settings, dry_run=True)

    assert [(result.relative_path, result.status) for result in results] == [
        (Path('project/session/audio/mic.flac'), 'would_upload')
    ]


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
        'baccy.upload._load_project',
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


@pytest.mark.parametrize(
    'expression',
    [
        'unknown',
        'duration + 1 > 2',
        'device.lower() == "device"',
        'channels[0] == 1',
        '[item for item in channels]',
        'x' * 513,
        "'x' * 257",
    ],
)
def test_match_expressions_reject_unsafe_syntax(expression: str) -> None:
    with pytest.raises(ValueError):
        MatchExpression(expression)


def test_match_expressions_support_boolean_composition_and_membership() -> None:
    expression = MatchExpression(
        "(main and duration > 120) or (format in ['wav', 'flac'] and not has_player)"
    )

    assert expression.matches(
        {
            'duration': 120,
            'main': False,
            'device': 'device',
            'channels': [1],
            'track': '',
            'format': 'flac',
            'player': None,
            'has_player': False,
        }
    )


def test_config_rejects_legacy_upload_policy(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match='ssh_url'):
        Settings.model_validate(
            {
                'backup_root': tmp_path / 'backup',
                'uploads': [{'ssh_url': 'host:/srv/recs'}],
            }
        )


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
        lambda destination: {'concert/2026/09/24/20-00-00/audio.flac'},
    )
    monkeypatch.setattr(
        'baccy.upload._source_hash',
        lambda path: pytest.fail('sync must not hash sources'),
    )

    result = sync([Path('concert')], settings)

    assert result.unchanged == 1
    assert result.uploaded == 0

    monkeypatch.setattr(
        'baccy.upload._remote_targets',
        lambda destination: pytest.fail('dry-run sync must not contact destinations'),
    )

    dry_run = sync([Path('concert')], settings, dry_run=True)

    assert dry_run.would_upload == 1
