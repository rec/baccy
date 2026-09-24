import json
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from baccy.cli import main
from baccy.config import load
from baccy.models import S3Destination


def test_axto_config_dry_run_syncs_recs_results_layout(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    backup = tmp_path / 'baccy'
    _write_session(backup / 'audio')
    config = Path(__file__).parent / 'axto.toml'

    exit_code = main(['--dry-run', 'sync', '--config', str(config)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    settings = load(config)
    destination = settings.destinations['axto']
    assert isinstance(destination, S3Destination)
    assert destination.endpoint_url is None
    assert settings.backup_root == backup
    assert output['would_upload'] == 3
    assert [Path(result['relative_path']) for result in output['results']] == [
        Path(
            'project/2026/09/04/15-01-57/'
            'FLOW 8 (Recording)/1-2/2026-09-04T13-01-58.000000Z.flac'
        ),
        Path('project/2026/09/04/15-01-57/2026-09-04T13-02-06.000000Z.mp3'),
        Path(
            'project/2026/09/04/15-01-57/'
            'FLOW 8 (Recording)/9-10/2026-09-04T13-02-06.000000Z.flac'
        ),
    ]
    assert not (backup / 'events.jsonl').exists()


def _write_session(root: Path) -> None:
    session = root / 'project' / '2026' / '09' / '04' / '15-01-57'
    session.mkdir(parents=True)
    (session / 'recording.toml').write_text('format = "recs"\n')
    (session / 'evidence').mkdir()
    (session / 'evidence' / 'recording-v3.toml').write_text('version = 3\n')
    (session / 'evidence' / 'session-record-v3.jsonl').write_text('{}\n')
    (session / 'midi').mkdir()
    (session / 'midi' / 'FLOW 8-20260904-150157.mid').write_bytes(b'midi')
    paths = [
        Path('audio/FLOW 8 (Recording) + 1-2 + 20260904-150158.flac'),
        Path('audio/FLOW 8 (Recording) + 9-10 + 20260904-150206.flac'),
    ]
    records = [
        _audio_records(
            paths[0].as_posix(),
            '2026-09-04T13:01:58Z',
            [1, 2],
        ),
        _audio_records(
            paths[1].as_posix(),
            '2026-09-04T13:02:06Z',
            [9, 10],
        ),
    ]
    for path in paths:
        audio = session / path
        audio.parent.mkdir(exist_ok=True)
        audio.write_bytes(b'flac')
    (session / 'session-record.jsonl').write_text(
        ''.join(json.dumps(value) + '\n' for pair in records for value in pair)
    )


def _audio_records(
    path: str, timestamp: str, channels: list[int]
) -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            'type': 'file_started',
            'media_type': 'audio',
            'stream_id': path,
            'timestamp': timestamp,
            'format': 'flac',
            'source': 'FLOW 8 (Recording)',
            'source_channels': channels,
            'path': path,
        },
        {
            'type': 'file_finished',
            'media_type': 'audio',
            'stream_id': path,
            'path': path,
            'frame_count': 5_808_000,
            'sample_rate': 48_000,
        },
    )
