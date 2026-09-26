import json
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from baccy.models import Encoding, S3Destination, Settings, UploadRule
from baccy.rename import _log, renamed_files
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

    files = renamed_files(Settings(), 'MacBook', 'Mic', False)

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


def test_rename_uses_regular_expressions_only_when_requested(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        'baccy.rename.planned_source_uploads',
        lambda settings: [
            ArtifactPlan(
                project='totm',
                source_name='backup',
                session=Path('totm/session'),
                segment=Segment(
                    path=Path('audio/Microphone + 1.flac'),
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
                target=Path('totm/session/audio/Microphone + 1.flac'),
                identity='source',
            )
        ],
    )

    files = renamed_files(Settings(), 'Microphone + 1', 'Mic', False)

    assert files[0].replacement.name == 'Mic.flac'
    assert renamed_files(Settings(), 'Microphone + 1', 'Mic', True) == []


def test_rename_logs_without_writing_to_standard_output(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    messages: list[str] = []
    monkeypatch.setattr('baccy.rename._LOGGER.info', messages.append)

    _log({'ok': True})

    assert capsys.readouterr().out == ''
    assert json.loads(messages[0])['ok'] is True
