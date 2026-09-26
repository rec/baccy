import shlex
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath

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
    s3_files: dict[str, tuple[S3Destination, list[tuple[str, Path]]]] = {}
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
            s3_files.setdefault(result.destination, (destination, []))[1].append(
                (path, result.relative_path)
            )
    values: list[tuple[str, datetime, int]] = []
    for destination, files in s3_files.values():
        remote_files = _s3_files(destination, [target for _, target in files])
        values.extend(
            (path, *remote_files[target.as_posix()])
            for path, target in files
            if target.as_posix() in remote_files
        )
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
    command = '; '.join(_ssh_stat(index, path) for index, path in enumerate(paths))
    result = subprocess.run(
        ['ssh', *_SSH_OPTIONS, host, command],
        capture_output=True,
        check=True,
        text=True,
    )
    values: dict[str, tuple[datetime, int]] = {}
    for line in result.stdout.splitlines():
        if (fields := line.split(maxsplit=2)) and len(fields) == 3:
            index, modified, size = fields
            try:
                values[targets[int(index)].as_posix()] = (
                    datetime.fromtimestamp(int(modified)).astimezone(),
                    int(size),
                )
            except IndexError, ValueError:
                continue
    return values


def _ssh_stat(index: int, path: str) -> str:
    quoted = shlex.quote(path)
    return (
        f'if [ -f {quoted} ]; then '
        f"value=$(stat -c '%Y %s' {quoted} 2>/dev/null "
        f"|| stat -f '%m %z' {quoted}) || exit; "
        f'printf \'%s %s\\n\' {index} "$value"; fi'
    )


def _s3_files(
    destination: S3Destination, targets: list[Path]
) -> dict[str, tuple[datetime, int]]:
    keys = {
        '/'.join(part for part in (destination.prefix, target.as_posix()) if part)
        for target in targets
    }
    prefixes = {
        '/'.join(part for part in (destination.prefix, target.parts[0]) if part) + '/'
        for target in targets
        if target.parts
    }
    values: dict[str, tuple[datetime, int]] = {}
    paginator = s3_client(destination).get_paginator('list_objects_v2')
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=destination.bucket, Prefix=prefix):
            for value in page.get('Contents', []):
                key = value.get('Key')
                modified = value.get('LastModified')
                size = value.get('Size')
                if (
                    isinstance(key, str)
                    and key in keys
                    and isinstance(modified, datetime)
                    and isinstance(size, int)
                ):
                    values[key.removeprefix(f'{destination.prefix.rstrip("/")}/')] = (
                        modified,
                        size,
                    )
    return values


def _size(value: int) -> str:
    if value < 1024:
        return f'{value}B'
    for suffix, divisor in [('K', 1024), ('M', 1024**2), ('G', 1024**3)]:
        if value < divisor * 1024:
            return f'{value / divisor:.0f}{suffix}'
    return f'{value / 1024**4:.0f}T'


def _time(value: datetime) -> str:
    return value.astimezone().strftime('%a %b %d %H:%M:%S %Z %Y')
