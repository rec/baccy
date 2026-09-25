import subprocess

from botocore.exceptions import BotoCoreError, ClientError

from .models import (
    Destination,
    S3Destination,
    Settings,
    SshDestination,
    parse_destination,
)
from .s3 import s3_client

_SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']


def test_destinations(settings: Settings) -> list[str]:
    failures: list[str] = []
    destinations = {rule.destination for rule in settings.uploads}
    destinations.update(page.destination for page in settings.landing_pages)
    for value in destinations:
        try:
            _test_destination(parse_destination(value))
        except (BotoCoreError, ClientError, OSError) as error:
            failures.append(f'{value}: {error}')
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
    s3_client(destination).list_objects_v2(Bucket=destination.bucket, MaxKeys=1)
