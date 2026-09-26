from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pytest import MonkeyPatch

from baccy.listing import list_uploaded
from baccy.models import BackupSummary, FileResult, Settings


def test_list_uploaded_lists_existing_planned_uploads(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = Settings()
    monkeypatch.setattr(
        'baccy.listing.sync',
        lambda directories, settings, dry_run: BackupSummary(
            results=[
                FileResult(
                    source='totm',
                    relative_path=Path('totm/a.flac'),
                    status='would_upload',
                    destination='s3:audio',
                ),
                FileResult(
                    source='totm',
                    relative_path=Path('totm/recording.flac'),
                    status='would_upload',
                    destination='s3:audio',
                ),
                FileResult(
                    source='totm',
                    relative_path=Path('totm/index.html'),
                    status='would_upload',
                    destination='ssh:user@example.org:/srv/public',
                ),
            ]
        ),
    )
    modified = datetime(2026, 9, 26, 10, 40, 8, tzinfo=ZoneInfo('Europe/Paris'))
    monkeypatch.setattr(
        'baccy.listing._remote_file',
        lambda destination, target: (
            (modified, 53 * 1024**2) if target.suffix == '.flac' else None
        ),
    )

    assert list_uploaded(settings) == [
        's3:audio/totm/a.flac          Sat Sep 26 10:40:08 CEST 2026  53M',
        's3:audio/totm/recording.flac  Sat Sep 26 10:40:08 CEST 2026  53M',
    ]
