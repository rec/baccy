import json
import subprocess
from pathlib import Path

from baccy.models import PathSource, ProjectUpload, ResolvedSource
from baccy.upload import publish_sessions


def test_upload_publishes_last_pair_and_skips_unchanged_files(tmp_path: Path) -> None:
    root = tmp_path / 'recs'
    session = root / 'project' / '2026-09-20' / '12-00-00'
    session.mkdir(parents=True)
    (session / 'recording.toml').write_text('format = "recs"\n')
    records = ['{"type":"header"}']
    for channel in range(1, 5):
        path = f'audio/{channel}.flac'
        audio = session / path
        audio.parent.mkdir(exist_ok=True)
        audio.write_bytes(b'audio')
        records.append(
            '{"type":"file_finished","media_type":"audio",'
            f'"source":"device","source_channels":[{channel}],'
            f'"frame_count":2880000,"sample_rate":48000,"path":"{path}"}}'
        )
    (session / 'session-record.jsonl').write_text('\n'.join(records) + '\n')
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b'', b'')

    source = ResolvedSource(
        source=PathSource(kind='path', name='recs', path=root), root=root
    )
    projects = {'project': ProjectUpload(ssh_url='user@host:/srv/recs')}

    first = publish_sessions([source], projects, tmp_path / 'backup', False, run)
    second = publish_sessions([source], projects, tmp_path / 'backup', False, run)

    assert [result.status for result in first] == ['uploaded'] * 4
    assert [result.status for result in second] == ['unchanged'] * 4
    assert [command[0] for command in calls].count('scp') == 4
    events = [
        json.loads(line)
        for line in (tmp_path / 'backup' / '.baccy' / 'events.jsonl')
        .read_text()
        .splitlines()
    ]
    assert [event['result'] for event in events] == ['uploaded'] * 4
    assert {event['operation'] for event in events} == {'upload'}
    assert not (tmp_path / 'backup' / '.baccy' / 'uploads.jsonl').exists()
    destinations = [command[-1] for command in calls]
    assert any('/project/2026-09-20/12-00-00/audio/3.flac' in d for d in destinations)
    assert any('/project/2026-09-20/12-00-00/audio/4.flac' in d for d in destinations)


def test_upload_records_invalid_session_journal_failure(tmp_path: Path) -> None:
    root = tmp_path / 'recs'
    session = root / 'project' / '2026-09-20' / '12-00-00'
    session.mkdir(parents=True)
    (session / 'session-record.jsonl').write_text('{invalid}\n')
    source = ResolvedSource(
        source=PathSource(kind='path', name='recs', path=root), root=root
    )

    results = publish_sessions(
        [source],
        {'project': ProjectUpload(ssh_url='user@host:/srv/recs')},
        tmp_path / 'backup',
        False,
    )

    event = json.loads((tmp_path / 'backup' / '.baccy' / 'events.jsonl').read_text())
    assert [result.status for result in results] == ['failed']
    assert event['operation'] == 'upload'
    assert event['result'] == 'failed'
