import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pytest import MonkeyPatch

from baccy.listing import _ssh_files, list_uploaded
from baccy.models import Encoding, FileResult, Settings, SshDestination, UploadRule


def test_list_uploaded_lists_existing_planned_uploads(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = Settings()
    monkeypatch.setattr(
        'baccy.listing._planned_uploads',
        lambda settings: [
            FileResult(
                source='totm',
                relative_path=Path('totm/a.flac'),
                status='would_upload',
                destination='s3:audio',
            ),
            FileResult(
                source='totm',
                relative_path=Path('totm/recording.flac'),
                status='would_upload',
                destination='s3:audio',
            ),
            FileResult(
                source='totm',
                relative_path=Path('totm/index.html'),
                status='would_upload',
                destination='ssh:user@example.org:/srv/public',
            ),
        ],
    )
    modified = datetime(2026, 9, 26, 10, 40, 8, tzinfo=ZoneInfo('Europe/Paris'))
    monkeypatch.setattr(
        'baccy.listing._s3_files',
        lambda destination, targets: {
            target.as_posix(): (modified, 53 * 1024**2) for target in targets
        },
    )
    monkeypatch.setattr('baccy.listing._ssh_files', lambda destination, targets: {})

    assert list_uploaded(settings) == [
        's3:audio/totm/a.flac          Sat Sep 26 10:40:08 CEST 2026  53M',
        's3:audio/totm/recording.flac  Sat Sep 26 10:40:08 CEST 2026  53M',
    ]


def test_list_uploaded_does_not_hash_audio(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    backup = tmp_path / 'backup'
    session = backup / 'audio' / 'totm' / 'session'
    audio = session / 'audio' / 'master.flac'
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b'audio')
    (session / 'session-record.jsonl').write_text(
        '\n'.join(
            json.dumps(value)
            for value in [
                {
                    'type': 'file_started',
                    'media_type': 'audio',
                    'stream_id': 'master',
                    'timestamp': '2026-09-26T10:40:08Z',
                    'format': 'flac',
                    'source': 'device',
                    'track_name': 'master',
                    'source_channels': [1],
                    'path': 'audio/master.flac',
                },
                {
                    'type': 'file_finished',
                    'media_type': 'audio',
                    'stream_id': 'master',
                    'path': 'audio/master.flac',
                    'frame_count': 48_000,
                    'sample_rate': 48_000,
                },
            ]
        )
        + '\n'
    )
    settings = Settings(
        backup_root=backup,
        uploads=[
            UploadRule(
                name='audio',
                match='True',
                encoding=Encoding(format='source'),
                destination='s3:audio',
            )
        ],
    )
    monkeypatch.setattr(
        'baccy.upload._source_hash',
        lambda path: (_ for _ in ()).throw(AssertionError(path)),
    )
    monkeypatch.setattr('baccy.listing._s3_files', lambda destination, targets: {})

    assert list_uploaded(settings) == []


def test_ssh_listing_ignores_a_banner(monkeypatch: MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='Welcome to the server\n1790406008\t55574528\t/srv/totm/a.flac\n',
        )

    monkeypatch.setattr('baccy.listing.subprocess.run', run)

    assert _ssh_files(
        SshDestination(kind='ssh', url='user@example.org:/srv'),
        [Path('totm/a.flac')],
    ) == {'totm/a.flac': (datetime.fromtimestamp(1790406008).astimezone(), 55574528)}
    assert "stat -f '%m\\t%z\\t%N'" in commands[0][-1]
