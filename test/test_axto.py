import json
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from baccy.cli import main
from baccy.config import load
from baccy.models import PathSource, ResolvedSource
from baccy.upload import publish_sessions


class NoNetworkDiscovery:
    new_machines: list[object] = []

    def discover(self) -> list[object]:
        return []


def test_axto_config_dry_run_mirrors_recs_results_layout(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    results = tmp_path / 'results'
    _write_session(results)
    backup = tmp_path / 'baccy'
    config = _write_config(tmp_path, results, backup)
    monkeypatch.setattr('baccy.backup.NetworkDiscovery', NoNetworkDiscovery)

    exit_code = main(['backup', '--dry-run', '--config', str(config)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output['would_copy'] == 7
    assert output['would_upload'] == 0
    assert not backup.exists()


def test_axto_config_dry_run_plans_main_mp3_and_all_flac(tmp_path: Path) -> None:
    backup = tmp_path / 'baccy'
    _write_session(backup / 'audio')
    config = _write_config(tmp_path, tmp_path / 'results', backup)
    settings = load(config)
    source = ResolvedSource(
        source=PathSource(kind='path', name='audio', path=backup / 'audio'),
        root=backup / 'audio',
    )

    results = publish_sessions([source], settings, dry_run=True)

    assert [result.status for result in results] == ['would_upload'] * 3
    assert [result.relative_path for result in results] == [
        Path(
            'oderg in duo/2026/09/04/15-01-57/'
            'FLOW 8 (Recording)/1-2/2026-09-04T13-01-58.000000Z.flac'
        ),
        Path('oderg in duo/2026/09/04/15-01-57/2026-09-04T13-02-06.000000Z.mp3'),
        Path(
            'oderg in duo/2026/09/04/15-01-57/'
            'FLOW 8 (Recording)/9-10/2026-09-04T13-02-06.000000Z.flac'
        ),
    ]
    assert not (backup / 'events.jsonl').exists()


def _write_config(directory: Path, results: Path, backup: Path) -> Path:
    config = directory / 'axto.toml'
    text = (Path(__file__).parent / 'axto.toml').read_text()
    config.write_text(
        text.replace('TODO_RECS_RESULTS_PATH', str(results)).replace(
            'TODO_BACCY_ROOT', str(backup)
        )
    )
    return config


def _write_session(root: Path) -> None:
    session = root / 'oderg in duo' / '2026' / '09' / '04' / '15-01-57'
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
