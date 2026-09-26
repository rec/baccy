import json
from pathlib import Path

from pytest import MonkeyPatch

from baccy.listing import list_uploaded
from baccy.models import Encoding, LandingPageUpload, Settings, UploadRule


def test_list_uploaded_lists_only_baccy_uploads(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    settings = Settings(
        backup_root=tmp_path / 'backup',
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
    events = settings.backup_root / 'events.jsonl'
    events.parent.mkdir()
    events.write_text(
        '\n'.join(
            json.dumps(value)
            for value in [
                {
                    'operation': 'upload',
                    'result': 'uploaded',
                    'destination': 'aws/audio/',
                    'target': 'totm/recording.flac',
                },
                {
                    'operation': 'landing_page',
                    'result': 'uploaded',
                    'destination': 'user@example.org:/srv/public',
                    'target': 'totm/index.html',
                },
                {
                    'operation': 'backup',
                    'result': 'copied',
                    'destination': 'aws/audio/',
                    'target': 'not-published.flac',
                },
            ]
        )
        + '\n'
    )
    monkeypatch.setattr('baccy.listing.s3_endpoint_url', lambda destination: None)

    assert list_uploaded(settings) == [
        's3:audio/totm/recording.flac',
        'ssh:user@example.org:/srv/public/totm/index.html',
    ]
