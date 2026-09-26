import shlex
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath

from botocore.exceptions import ClientError

from .models import (
    FileResult,
    PathSource,
    ResolvedSource,
    S3Destination,
    Settings,
    SshDestination,
    parse_destination,
)
from .s3 import s3_client
from .upload import publish_sessions

_SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']


def list_uploaded(settings: Settings) -> list[str]:
    s3_files: list[tuple[str, S3Destination, Path]] = []
    ssh_files: dict[str, tuple[SshDestination, list[tuple[str, Path]]]] = {}
    for result in _planned_uploads(settings):
        if (
            result.status != 'would_upload'
            or result.destination is None
            or result.relative_path is None
        ):
            continue
        destination = parse_destination(result.destination, settings.s3_max_bandwidth)
        path = f'{result.destination}/{result.relative_path.as_posix()}'
        if isinstance(destination, SshDestination):
            ssh_files.setdefault(destination.url, (destination, []))[1].append(
                (path, result.relative_path)
            )
        else:
            s3_files.append((path, destination, result.relative_path))
    values = [
        (path, modified, size)
        for path, destination, target in s3_files
        if (remote := _s3_file(destination, target)) is not None
        for modified, size in [remote]
    ]
    for destination, files in ssh_files.values():
        remote_files = _ssh_files(destination, [target for _, target in files])
        values.extend(
            (path, *remote_files[target.as_posix()])
            for path, target in files
            if target.as_posix() in remote_files
        )
    width = max((len(path) for path, _, _ in values), default=0)
    return [
        f'{path:<{width}}  {_time(modified)}  {_size(size)}'
        for path, modified, size in sorted(values)
    ]


def _planned_uploads(settings: Settings) -> list[FileResult]:
    root = settings.backup_root / 'audio'
    source = ResolvedSource(
        source=PathSource(kind='path', name='backup', path=root), root=root
    )
    return publish_sessions([source], settings, dry_run=True, sync=True)


def _ssh_files(
    destination: SshDestination, targets: list[Path]
) -> dict[str, tuple[datetime, int]]:
    host, _, base = destination.url.partition(':')
    paths = [str(PurePosixPath(base) / target.as_posix()) for target in targets]
    command = '; '.join(_ssh_stat(path) for path in paths)
    result = subprocess.run(
        ['ssh', *_SSH_OPTIONS, host, command],
        capture_output=True,
        check=True,
        text=True,
    )
    values: dict[str, tuple[datetime, int]] = {}
    prefix = f'{base.rstrip("/")}/'
    for line in result.stdout.splitlines():
        modified, size, path = line.split('\t', maxsplit=2)
        values[path.removeprefix(prefix)] = (
            datetime.fromtimestamp(int(modified)).astimezone(),
            int(size),
        )
    return values


def _ssh_stat(path: str) -> str:
    quoted = shlex.quote(path)
    return f"if [ -f {quoted} ]; then stat -c '%Y\\t%s\\t%n' {quoted}; fi"


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
