import json
import logging
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import PurePosixPath

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from .models import Destination, S3Destination, Settings, SshDestination
from .s3 import s3_client, s3_endpoint_url
from .upload import planned_remote_targets

_LOGGER = logging.getLogger(__name__)
_SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']


class Relocation(BaseModel, frozen=True):
    destination: Destination
    source: PurePosixPath
    target: PurePosixPath

    @property
    def source_address(self) -> str:
        return _address(self.destination, self.source)

    @property
    def target_address(self) -> str:
        return _address(self.destination, self.target)


def planned_relocations(settings: Settings) -> list[Relocation]:
    values = [
        Relocation(
            destination=plan.destination,
            source=plan.legacy_target,
            target=plan.target,
        )
        for plan in planned_remote_targets(settings)
        if plan.legacy_target != plan.target
    ]
    _validate_relocations(values)
    return sorted(
        values, key=lambda value: (value.source_address, value.target_address)
    )


def relocate_urls(relocations: list[Relocation]) -> bool:
    for relocation in relocations:
        _log({'source': relocation.source_address, 'target': relocation.target_address})
        try:
            source, target = _remote_presence(relocation)
            if source and target:
                raise ValueError('legacy and web-safe remote paths both exist')
            if source:
                _relocate(relocation)
                _log({'ok': True})
            elif target:
                _log({'status': 'already migrated'})
            else:
                _log({'status': 'absent'})
        except (BotoCoreError, ClientError, OSError, ValueError) as error:
            _log({'error': str(error)})
            return False
    return True


def _validate_relocations(relocations: list[Relocation]) -> None:
    destinations: dict[tuple[str, str], PurePosixPath] = {}
    for relocation in relocations:
        key = (
            _destination_identity(relocation.destination),
            relocation.target.as_posix(),
        )
        if (
            source := destinations.get(key)
        ) is not None and source != relocation.source:
            raise ValueError(
                f'web-safe upload target collides: {source} and {relocation.source}'
            )
        destinations[key] = relocation.source


def _remote_presence(relocation: Relocation) -> tuple[bool, bool]:
    return _exists(relocation.destination, relocation.source), _exists(
        relocation.destination, relocation.target
    )


def _exists(destination: Destination, target: PurePosixPath) -> bool:
    if isinstance(destination, S3Destination):
        try:
            s3_client(destination).head_object(
                Bucket=destination.bucket, Key=_s3_key(destination, target)
            )
        except ClientError as error:
            if error.response['Error'].get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                return False
            raise
        return True
    host, path = _ssh_path(destination, target)
    result = subprocess.run(
        ['ssh', *_SSH_OPTIONS, host, f'test -f {shlex.quote(path)}'],
        capture_output=True,
        check=False,
    )
    if result.returncode in {0, 1}:
        return result.returncode == 0
    raise OSError(result.stderr.decode(errors='replace').strip())


def _relocate(relocation: Relocation) -> None:
    if isinstance(relocation.destination, S3Destination):
        client = s3_client(relocation.destination)
        source = _s3_key(relocation.destination, relocation.source)
        target = _s3_key(relocation.destination, relocation.target)
        client.copy_object(
            Bucket=relocation.destination.bucket,
            Key=target,
            CopySource={'Bucket': relocation.destination.bucket, 'Key': source},
        )
        client.delete_object(Bucket=relocation.destination.bucket, Key=source)
        return
    host, source = _ssh_path(relocation.destination, relocation.source)
    _, target = _ssh_path(relocation.destination, relocation.target)
    command = (
        f'mkdir -p {shlex.quote(str(PurePosixPath(target).parent))} && '
        f'mv {shlex.quote(source)} {shlex.quote(target)}'
    )
    result = subprocess.run(
        ['ssh', *_SSH_OPTIONS, host, command], capture_output=True, check=False
    )
    if result.returncode:
        raise OSError(result.stderr.decode(errors='replace').strip())


def _address(destination: Destination, target: PurePosixPath) -> str:
    if isinstance(destination, S3Destination):
        return f's3:{destination.bucket}/{target}'
    return f'ssh:{destination.url}/{target}'


def _s3_key(destination: S3Destination, target: PurePosixPath) -> str:
    return '/'.join(part for part in (destination.prefix, target.as_posix()) if part)


def _ssh_path(destination: SshDestination, target: PurePosixPath) -> tuple[str, str]:
    host, _, base = destination.url.partition(':')
    return host, f'{base.rstrip("/")}/{target}'


def _destination_identity(destination: Destination) -> str:
    if isinstance(destination, SshDestination):
        return destination.url
    return (
        f'{s3_endpoint_url(destination) or "aws"}/'
        f'{destination.bucket}/{destination.prefix}'
    )


def _log(value: dict[str, object]) -> None:
    timestamp = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    _LOGGER.info(json.dumps({'timestamp': timestamp} | value))
