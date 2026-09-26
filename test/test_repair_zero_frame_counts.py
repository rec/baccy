import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'repair_zero_frame_counts.py'


def test_repair_zero_frame_counts_updates_flac_session_records(tmp_path: Path) -> None:
    session = tmp_path / 'project' / 'session'
    audio = session / 'audio' / 'recording.flac'
    audio.parent.mkdir(parents=True)
    _write_flac(audio, 48_000)
    journal = session / 'session-record.jsonl'
    journal.write_text(
        '{"type":"header"}\n'
        '{"type":"file_finished","media_type":"audio",'
        '"stream_id":"audio:device:track","path":"audio/recording.flac",'
        '"frame_count":0}\n'
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == f'{journal}: repaired 1 frame counts\n'
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert records[0] == {'type': 'header'}
    assert records[1]['frame_count'] == 48_000


def _write_flac(path: Path, frames: int) -> None:
    properties = (48_000 << 44) | (1 << 41) | (15 << 36) | frames
    stream_info = b'\0' * 10 + properties.to_bytes(8, 'big') + b'\0' * 16
    path.write_bytes(b'fLaC' + b'\x80\0\0\x22' + stream_info)
