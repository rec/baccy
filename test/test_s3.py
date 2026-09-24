from pathlib import Path

from pytest import MonkeyPatch

from baccy.models import S3Destination
from baccy.s3 import s3_client


def test_s3_client_reads_default_endpoint_from_aws_config(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    aws = tmp_path / '.aws'
    aws.mkdir()
    (aws / 'config').write_text(
        '[default]\nregion = nbg1\nendpoint_url = https://nbg1.your-objectstorage.com\n'
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(
        'baccy.s3.boto3.client',
        lambda service, **kwargs: calls.append({'service': service, **kwargs}),
    )

    s3_client(S3Destination(kind='s3', bucket='axto'))

    assert calls == [
        {
            'service': 's3',
            'endpoint_url': 'https://nbg1.your-objectstorage.com',
        }
    ]


def test_s3_client_prefers_a_configured_endpoint(monkeypatch: MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        'baccy.s3.boto3.client',
        lambda service, **kwargs: calls.append({'service': service, **kwargs}),
    )

    s3_client(
        S3Destination(
            kind='s3',
            bucket='axto',
            endpoint_url='https://configured.example.com',
        )
    )

    assert calls == [
        {'service': 's3', 'endpoint_url': 'https://configured.example.com'}
    ]
