import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'repair_imported_session.py'


def test_repair_reports_a_unique_audio_basename(tmp_path: Path) -> None:
    session = _session(tmp_path)
    (session / 'audio').mkdir()
    (session / 'audio' / 'recording.flac').write_bytes(b'flac')

    result = _run(session)

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report['report'] == [
        {
            'path': 'old-layout/recording.flac',
            'status': 'repaired',
            'resolved_path': 'audio/recording.flac',
        }
    ]
    assert '"path": "audio/recording.flac"' in report['proposed_journal']
    assert (session / 'session-record.jsonl').read_text() == '{"type":"import"}\n'


def test_repair_apply_keeps_the_import_stub_as_a_backup(tmp_path: Path) -> None:
    session = _session(tmp_path)
    (session / 'audio').mkdir()
    (session / 'audio' / 'recording.flac').write_bytes(b'flac')

    result = _run(session, '--apply')

    assert result.returncode == 0
    assert (session / 'session-record.import-stub.jsonl').read_text() == (
        '{"type":"import"}\n'
    )
    journal = (session / 'session-record.jsonl').read_text()
    assert '"path": "audio/recording.flac"' in journal


def test_repair_apply_refuses_a_missing_audio_file(tmp_path: Path) -> None:
    session = _session(tmp_path)
    (session / 'audio').mkdir()

    result = _run(session, '--apply')

    assert result.returncode == 1
    assert 'refusing to apply unresolved repair' in result.stderr
    assert (session / 'session-record.jsonl').read_text() == '{"type":"import"}\n'


def _session(root: Path) -> Path:
    session = root / 'session'
    (session / 'evidence').mkdir(parents=True)
    (session / 'session-record.jsonl').write_text('{"type":"import"}\n')
    (session / 'evidence' / 'session-record-v3.jsonl').write_text(
        '{"type":"file_started","media_type":"audio",'
        '"stream_id":"stream","path":"old-layout/recording.flac"}\n'
        '{"type":"file_finished","stream_id":"stream",'
        '"path":"old-layout/recording.flac",'
        '"frame_count":1,"sample_rate":1}\n'
    )
    return session


def _run(session: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(session), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
