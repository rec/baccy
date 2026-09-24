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
    assert '"source": "device"' in report['proposed_journal']
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


def test_repair_discards_missing_audio_when_requested(tmp_path: Path) -> None:
    session = _session(tmp_path)
    (session / 'audio').mkdir()

    result = _run(session, '--discard-missing-audio')

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report['report'] == [
        {'path': 'old-layout/recording.flac', 'status': 'discarded'}
    ]


def test_repair_matches_channels_and_adds_unrecorded_audio(tmp_path: Path) -> None:
    session = tmp_path / 'session'
    (session / 'audio').mkdir(parents=True)
    (session / 'session-record.jsonl').write_text('{"type":"import"}\n')
    (session / 'evidence').mkdir()
    (session / 'evidence' / 'session-record-v3.jsonl').write_text(
        '{"type":"file_started","media_type":"audio",'
        '"stream_id":"audio:LiveTrak L-12:TRACK01",'
        '"path":"old/LiveTrak L-12 + TRACK01 + 20170101-000002.WAV",'
        '"source_channels":[1],"timestamp":"2017-01-01T00:00:02Z",'
        '"format":"wav","frame_count":0,"sample_rate":48000}\n'
        '{"type":"file_finished","stream_id":"audio:LiveTrak L-12:TRACK01",'
        '"path":"old/LiveTrak L-12 + TRACK01 + 20170101-000002.WAV",'
        '"frame_count":1,"sample_rate":48000}\n'
    )
    (session / 'audio' / '1 + 20170101-000002.flac').touch()
    (session / 'audio' / 'master + 20170101-000002.flac').touch()

    result = _run(session, '--include-unrecorded-audio')

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report['report'] == [
        {
            'path': 'old/LiveTrak L-12 + TRACK01 + 20170101-000002.WAV',
            'status': 'repaired',
            'resolved_path': 'audio/1 + 20170101-000002.flac',
        },
        {
            'path': 'audio/master + 20170101-000002.flac',
            'status': 'added',
            'resolved_path': 'audio/master + 20170101-000002.flac',
        },
    ]


def _session(root: Path) -> Path:
    session = root / 'session'
    (session / 'evidence').mkdir(parents=True)
    (session / 'session-record.jsonl').write_text('{"type":"import"}\n')
    (session / 'evidence' / 'session-record-v3.jsonl').write_text(
        '{"type":"file_started","media_type":"audio",'
        '"stream_id":"audio:device:1",'
        '"path":"old-layout/recording.flac"}\n'
        '{"type":"file_finished","stream_id":"audio:device:1",'
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
