import shlex
import subprocess

from .models import (
    Destination,
    S3Destination,
    Settings,
    SshDestination,
    parse_destination,
)
from .s3 import s3_client

_SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']


def list_uploaded(settings: Settings) -> list[str]:
    destinations = _destinations(settings)
    return sorted(path for destination in destinations for path in _files(destination))


def _destinations(settings: Settings) -> list[Destination]:
    values = [rule.destination for rule in settings.uploads]
    values.extend(page.destination for page in settings.landing_pages)
    return [
        parse_destination(value, settings.s3_max_bandwidth)
        for value in dict.fromkeys(values)
    ]


def _files(destination: Destination) -> list[str]:
    if isinstance(destination, SshDestination):
        return _ssh_files(destination)
    return _s3_files(destination)


def _ssh_files(destination: SshDestination) -> list[str]:
    host, _, base = destination.url.partition(':')
    result = subprocess.run(
        ['ssh', *_SSH_OPTIONS, host, f'find {shlex.quote(base)} -type f -print'],
        capture_output=True,
        check=True,
        text=True,
    )
    return [f'ssh:{host}:{path}' for path in result.stdout.splitlines()]


def _s3_files(destination: S3Destination) -> list[str]:
    client = s3_client(destination)
    prefix = destination.prefix.rstrip('/')
    values: list[str] = []
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=destination.bucket, Prefix=prefix):
        for value in page.get('Contents', []):
            if isinstance(key := value.get('Key'), str):
                values.append(f's3:{destination.bucket}/{key}')
    return values
