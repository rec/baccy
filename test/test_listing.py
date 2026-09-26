import subprocess

from pytest import MonkeyPatch

from baccy.listing import list_uploaded
from baccy.models import Encoding, LandingPageUpload, Settings, UploadRule


def test_list_uploaded_lists_all_configured_destinations(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = Settings(
        uploads=[
            UploadRule(
                name='audio',
                match='True',
                encoding=Encoding(format='source'),
                destination='s3:audio',
            ),
        ],
        landing_pages=[
            LandingPageUpload(
                upload='audio', destination='ssh:user@example.org:/srv/public'
            )
        ],
    )

    class Paginator:
        def paginate(self, **kwargs: object) -> list[dict[str, object]]:
            assert kwargs == {'Bucket': 'audio', 'Prefix': ''}
            return [{'Contents': [{'Key': 'totm/recording.flac'}]}]

    class Client:
        def get_paginator(self, operation: str) -> Paginator:
            assert operation == 'list_objects_v2'
            return Paginator()

    monkeypatch.setattr('baccy.listing.s3_client', lambda destination: Client())
    monkeypatch.setattr(
        'baccy.listing.subprocess.run',
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout='/srv/public/totm/index.html\n'
        ),
    )

    assert list_uploaded(settings) == [
        's3:audio/totm/recording.flac',
        'ssh:user@example.org:/srv/public/totm/index.html',
    ]
