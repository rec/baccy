import shlex
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath

from botocore.exceptions import ClientError

from .models import (
    Destination,
    S3Destination,
    Settings,
    SshDestination,
    parse_destination,
)
from .s3 import s3_client
from .sync import sync

_SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']


def list_uploaded(settings: Settings) -> list[str]:
    values = [
        (path, modified, size)
        for result in sync([], settings, dry_run=True).results
        if result.status == 'would_upload'
        and result.destination is not None
        and result.relative_path is not None
        and (
            remote := _remote_file(
                parse_destination(result.destination, settings.s3_max_bandwidth),
                result.relative_path,
            )
        )
        is not None
        for path in [f'{result.destination}/{result.relative_path.as_posix()}']
        for modified, size in [remote]
    ]
    width = max((len(path) for path, _, _ in values), default=0)
    return [
        f'{path:<{width}}  {_time(modified)}  {_size(size)}'
        for path, modified, size in sorted(values)
    ]


def _remote_file(destination: Destination, target: Path) -> tuple[datetime, int] | None:
    if isinstance(destination, SshDestination):
        return _ssh_file(destination, target)
    return _s3_file(destination, target)


def _ssh_file(destination: SshDestination, target: Path) -> tuple[datetime, int] | None:
    host, _, base = destination.url.partition(':')
    path = PurePosixPath(base) / target.as_posix()
    result = subprocess.run(
        [
            'ssh',
            *_SSH_OPTIONS,
            host,
            f"stat -c '%Y %s' {shlex.quote(path.as_posix())}",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        if 'No such file or directory' in result.stderr:
            return None
        raise OSError(result.stderr.strip())
    modified, size = result.stdout.split()
    return datetime.fromtimestamp(int(modified)).astimezone(), int(size)


def _s3_file(destination: S3Destination, target: Path) -> tuple[datetime, int] | None:
    key = '/'.join(part for part in (destination.prefix, target.as_posix()) if part)
    try:
        value = s3_client(destination).head_object(Bucket=destination.bucket, Key=key)
    except ClientError as error:
        if error.response['Error'].get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
            return None
        raise
    modified = value.get('LastModified')
    size = value.get('ContentLength')
    if not isinstance(modified, datetime) or not isinstance(size, int):
        raise ValueError(f'invalid S3 metadata for {key}')
    return modified, size


def _size(value: int) -> str:
    if value < 1024:
        return f'{value}B'
    for suffix, divisor in [('K', 1024), ('M', 1024**2), ('G', 1024**3)]:
        if value < divisor * 1024:
            return f'{value / divisor:.0f}{suffix}'
    return f'{value / 1024**4:.0f}T'


def _time(value: datetime) -> str:
    return value.astimezone().strftime('%a %b %d %H:%M:%S %Z %Y')
