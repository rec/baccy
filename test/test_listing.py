import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError
from pytest import MonkeyPatch

from baccy.listing import _s3_files, _ssh_files, list_present_uploads
from baccy.models import (
    Encoding,
    FileResult,
    S3Destination,
    Settings,
    SshDestination,
    UploadRule,
)


def test_list_present_uploads_lists_existing_planned_uploads(
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

    assert list_present_uploads(settings) == [
        's3:audio/totm/a.flac          Sat Sep 26 10:40:08 CEST 2026  53M',
        's3:audio/totm/recording.flac  Sat Sep 26 10:40:08 CEST 2026  53M',
        '',
        'Summary',
        'Files: 2',
        'Locations:',
        's3:audio  2',
        'Suffixes:',
        '.flac  2',
        'Total size: 106M',
    ]


def test_list_shows_s3_prefix_in_remote_path(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        'baccy.listing._planned_uploads',
        lambda settings: [
            FileResult(
                source='totm',
                relative_path=Path('totm/a.flac'),
                status='would_upload',
                destination='s3:audio/archive',
            )
        ],
    )
    modified = datetime(2026, 9, 26, tzinfo=ZoneInfo('Europe/Paris'))
    monkeypatch.setattr(
        'baccy.listing._s3_files',
        lambda destination, targets: {'totm/a.flac': (modified, 5)},
    )

    rows = list_present_uploads(Settings())

    assert rows[0].startswith('s3:audio/archive/totm/a.flac  ')


def test_list_present_uploads_does_not_hash_audio(
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

    assert list_present_uploads(settings) == [
        '',
        'Summary',
        'Files: 0',
        'Locations:',
        'Suffixes:',
        'Total size: 0B',
    ]


def test_ssh_listing_ignores_a_banner(monkeypatch: MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='Welcome to the server\n0 1790406008 55574528\n',
        )

    monkeypatch.setattr('baccy.listing.subprocess.run', run)

    assert _ssh_files(
        SshDestination(kind='ssh', address='user@example.org:/srv'),
        [Path('totm/a.flac')],
    ) == {'totm/a.flac': (datetime.fromtimestamp(1790406008).astimezone(), 55574528)}
    assert "stat -f '%m %z'" in commands[0][-1]


def test_ssh_listing_batches_large_target_sets(monkeypatch: MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='')

    monkeypatch.setattr('baccy.listing.subprocess.run', run)
    targets = [Path(f'project/{index}.flac') for index in range(33)]

    assert _ssh_files(SshDestination(kind='ssh', address='host:/srv'), targets) == {}
    assert len(commands) == 3


def test_ssh_listing_bounds_command_length(monkeypatch: MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='')

    monkeypatch.setattr('baccy.listing.subprocess.run', run)
    targets = [Path(f'project/{index}{"x" * 1000}.flac') for index in range(20)]

    assert _ssh_files(SshDestination(kind='ssh', address='host:/srv'), targets) == {}
    assert len(commands) > 2
    assert all(len(command[-1]) <= 16_384 for command in commands)


def test_s3_listing_checks_only_planned_keys(monkeypatch: MonkeyPatch) -> None:
    requested: list[str] = []
    modified = datetime(2026, 9, 26, tzinfo=ZoneInfo('Europe/Paris'))

    class Client:
        def head_object(self, Bucket: str, Key: str) -> dict[str, object]:
            requested.append(Key)
            if Key == 'prefix/project/missing.flac':
                raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
            return {'LastModified': modified, 'ContentLength': 5}

    monkeypatch.setattr('baccy.listing.s3_client', lambda destination: Client())

    assert _s3_files(
        S3Destination(kind='s3', bucket='archive', prefix='prefix'),
        [Path('project/audio.flac'), Path('project/missing.flac')],
    ) == {'project/audio.flac': (modified, 5)}
    assert sorted(requested) == [
        'prefix/project/audio.flac',
        'prefix/project/missing.flac',
    ]
