from pathlib import Path

from pytest import MonkeyPatch

from baccy.models import Encoding, S3Destination, Settings, UploadRule
from baccy.rename import renamed_files
from baccy.upload import ArtifactPlan, Segment


def test_rename_lists_only_direct_s3_uploads(monkeypatch: MonkeyPatch) -> None:
    source = ArtifactPlan(
        project='totm',
        source_name='backup',
        session=Path('totm/session'),
        segment=Segment(
            path=Path('audio/MacBook.flac'),
            timestamp='2026-09-26T10:40:08Z',
            source='device',
            channels=[1],
            frame_count=48_000,
            sample_rate=48_000,
            track='main',
            format='flac',
        ),
        rule=UploadRule(
            name='source',
            match='True',
            encoding=Encoding(format='source'),
            destination='s3:archive',
        ),
        destination=S3Destination(kind='s3', bucket='archive'),
        target=Path('totm/session/audio/MacBook.flac'),
        identity='source',
    )
    monkeypatch.setattr(
        'baccy.rename.planned_source_uploads', lambda settings: [source]
    )

    files = renamed_files(Settings(), 'MacBook', 'Mic')

    assert [value.model_dump() for value in files] == [
        {
            'session': Path('totm/session'),
            'source': Path('totm/session/audio/MacBook.flac'),
            'replacement': Path('totm/session/audio/Mic.flac'),
            'destinations': [
                {
                    'kind': 's3',
                    'bucket': 'archive',
                    'endpoint_url': None,
                    'prefix': '',
                    'max_bandwidth': 1_000_000,
                }
            ],
        }
    ]
