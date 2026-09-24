import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from baccy.match import MatchExpression
from baccy.models import PathSource, ResolvedSource, Settings
from baccy.sync import sync
from baccy.upload import publish_sessions


def test_upload_rules_select_main_channels_and_skip_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
                    'source_channels': [channel],
                    'path': path,
                },
                {
                    'type': 'file_finished',
                    'media_type': 'audio',
                    'stream_id': str(channel),
                    'path': path,
                    'frame_count': 5_808_000,
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
            'destinations': {'server': {'kind': 'ssh', 'url': 'user@host:/srv/recs'}},
            'access': {'listeners': {'ssh_mode': '0644'}},
            'projects': {
                'project': {
                    'uploads': [
                        {
                            'name': 'main',
                            'match': 'main and duration > 120',
                            'encoding': {'format': 'source'},
                            'filename': '{channels}/{timestamp}.{extension}',
                            'destination': 'server',
                            'access': {'profile': 'listeners'},
                        }
                    ]
                }
            },
        }
    )

    first = publish_sessions([source], settings, False)
    second = publish_sessions([source], settings, False)

    assert [result.status for result in first] == ['uploaded'] * 2
    assert [result.status for result in second] == ['unchanged'] * 2
    assert [call[0] for call in calls].count('scp') == 2
    assert [call[0] for call in calls].count('ssh') == 4
    events = [
        json.loads(line)
        for line in (tmp_path / 'backup' / 'events.jsonl').read_text().splitlines()
    ]
    assert {event['rule'] for event in events} == {'main'}
    assert {event['access_profile'] for event in events} == {'listeners'}


def test_upload_rules_defer_player_access_and_never_write_on_dry_run(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'recs'
    session = root / 'project' / 'session'
    session.mkdir(parents=True)
    (session / 'audio.wav').write_bytes(b'audio')
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
                        'path': 'audio.wav',
                    }
                ),
                json.dumps(
                    {
                        'type': 'file_finished',
                        'media_type': 'audio',
                        'stream_id': 'mic',
                        'path': 'audio.wav',
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
            'destinations': {'server': {'kind': 'ssh', 'url': 'host:/srv/recs'}},
            'projects': {
                'project': {
                    'uploads': [
                        {
                            'name': 'player',
                            'match': 'True',
                            'encoding': {'format': 'mp3', 'bitrate_kbps': 128},
                            'filename': '{timestamp}.{extension}',
                            'destination': 'server',
                            'access': {'from': 'player'},
                        }
                    ]
                }
            },
        }
    )
    source = ResolvedSource(
        source=PathSource(kind='path', name='recs', path=root), root=root
    )

    results = publish_sessions([source], settings, True)

    assert [result.status for result in results] == ['deferred']
    assert results[0].detail == 'player access metadata is missing'
    assert not (tmp_path / 'backup').exists()


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
                'projects': {'project': {'ssh_url': 'host:/srv/recs'}},
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
            'destinations': {'server': {'kind': 'ssh', 'url': 'host:/srv/recs'}},
            'access': {'private': {}},
            'projects': {
                'concert': {
                    'uploads': [
                        {
                            'name': 'archive',
                            'match': 'True',
                            'encoding': {'format': 'source'},
                            'filename': '{timestamp}.{extension}',
                            'destination': 'server',
                            'access': {'profile': 'private'},
                        }
                    ]
                }
            },
        }
    )
    monkeypatch.setattr(
        'baccy.upload._remote_targets',
        lambda destination: {'2026-09-24T20-00-00.000000Z.flac'},
    )
    monkeypatch.setattr(
        'baccy.upload._source_hash',
        lambda path: pytest.fail('sync must not hash sources'),
    )

    result = sync([Path('concert')], settings)

    assert result.unchanged == 1
    assert result.uploaded == 0
