from configparser import ConfigParser
from pathlib import Path

import boto3
from botocore.client import BaseClient

from .models import S3Destination


def s3_client(destination: S3Destination) -> BaseClient:
    return boto3.client('s3', endpoint_url=s3_endpoint_url(destination))


def s3_endpoint_url(destination: S3Destination) -> str | None:
    if destination.endpoint_url is not None:
        return destination.endpoint_url
    config = ConfigParser()
    config.read(Path.home() / '.aws' / 'config')
    return config.get('default', 'endpoint_url', fallback=None)
