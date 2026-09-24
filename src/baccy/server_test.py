import subprocess

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .models import Destination, S3Destination, Settings, SshDestination

_SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']


def test_destinations(settings: Settings) -> list[str]:
    failures: list[str] = []
    for name, destination in settings.destinations.items():
        try:
            _test_destination(destination)
        except (BotoCoreError, ClientError, OSError) as error:
            failures.append(f'{name}: {error}')
    return failures


def _test_destination(destination: Destination) -> None:
    if isinstance(destination, SshDestination):
        host, _, _ = destination.url.partition(':')
        result = subprocess.run(
            ['ssh', *_SSH_OPTIONS, host, 'true'], capture_output=True, check=False
        )
        if result.returncode:
            raise OSError(result.stderr.decode(errors='replace').strip())
        return
    _test_s3(destination)


def _test_s3(destination: S3Destination) -> None:
    boto3.client(
        's3',
        endpoint_url=destination.endpoint_url,
    ).list_objects_v2(Bucket=destination.bucket, MaxKeys=1)
