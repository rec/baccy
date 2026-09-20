import plistlib
import subprocess
from pathlib import Path

from baccy.discovery import resolve_sources
from baccy.models import PathSource, VolumeSource


def test_resolve_sources_finds_path_source(tmp_path: Path) -> None:
    source_root = tmp_path / 'source'
    source_root.mkdir()

    source = PathSource(kind='path', name='one', path=source_root)
    resolved, unavailable = resolve_sources([source])

    assert [s.root for s in resolved] == [source_root]
    assert unavailable == []


def test_resolve_sources_matches_volume_uuid(tmp_path: Path) -> None:
    volumes = tmp_path / 'Volumes'
    volume = volumes / 'card'
    volume.mkdir(parents=True)

    def diskutil(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=plistlib.dumps({'VolumeUUID': 'expected'}),
        )

    source = VolumeSource(kind='volume', name='card', uuid='expected')
    resolved, unavailable = resolve_sources([source], volumes, diskutil)

    assert [s.root for s in resolved] == [volume]
    assert unavailable == []


def test_resolve_sources_reports_missing_path(tmp_path: Path) -> None:
    source = PathSource(kind='path', name='missing', path=tmp_path / 'missing')

    resolved, unavailable = resolve_sources([source])

    assert resolved == []
    assert unavailable == [source]
