import plistlib
import subprocess
from collections.abc import Callable
from pathlib import Path

from baccy.discovery import discover_removable_sources, resolve_sources
from baccy.models import PathSource, ResolvedSource, VolumeSource


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


def test_discover_removable_sources_finds_camera_volume(tmp_path: Path) -> None:
    volumes = tmp_path / 'Volumes'
    camera = volumes / 'CAMERA'
    (camera / 'DCIM').mkdir(parents=True)

    sources = discover_removable_sources(
        tmp_path / 'backup', [], volumes, _diskutil(camera='camera-uuid')
    )

    assert [s.root for s in sources] == [camera]
    assert sources[0].source.name == 'removable-camera-uuid'


def test_discover_removable_sources_finds_nested_recs_session(tmp_path: Path) -> None:
    volumes = tmp_path / 'Volumes'
    recordings = volumes / 'RECORDINGS'
    session = recordings / 'recs' / '2026-09-20 12-00-00'
    session.mkdir(parents=True)
    (session / 'session-record.jsonl').write_text('{"type":"header"}\n')

    sources = discover_removable_sources(
        tmp_path / 'backup', [], volumes, _diskutil(recordings='recs-uuid')
    )

    assert [s.root for s in sources] == [recordings]


def test_discover_removable_sources_ignores_unrecognized_volume(tmp_path: Path) -> None:
    volumes = tmp_path / 'Volumes'
    ordinary = volumes / 'ORDINARY'
    ordinary.mkdir(parents=True)
    (ordinary / 'notes.txt').write_text('private')

    sources = discover_removable_sources(
        tmp_path / 'backup', [], volumes, _diskutil(ordinary='ordinary-uuid')
    )

    assert sources == []


def test_discover_removable_sources_ignores_unrelated_recording_file(
    tmp_path: Path,
) -> None:
    volumes = tmp_path / 'Volumes'
    ordinary = volumes / 'ORDINARY'
    ordinary.mkdir(parents=True)
    (ordinary / 'recording.toml').write_text('format = "other"\n')

    sources = discover_removable_sources(
        tmp_path / 'backup', [], volumes, _diskutil(ordinary='ordinary-uuid')
    )

    assert sources == []


def test_discover_removable_sources_ignores_backup_volume(tmp_path: Path) -> None:
    volumes = tmp_path / 'Volumes'
    backup = volumes / 'BACKUP'
    (backup / 'DCIM').mkdir(parents=True)

    sources = discover_removable_sources(
        backup / 'baccy', [], volumes, _diskutil(backup='backup-uuid')
    )

    assert sources == []


def test_discover_removable_sources_ignores_configured_root(tmp_path: Path) -> None:
    volumes = tmp_path / 'Volumes'
    camera = volumes / 'CAMERA'
    (camera / 'DCIM').mkdir(parents=True)
    source = PathSource(kind='path', name='camera', path=camera)

    sources = discover_removable_sources(
        tmp_path / 'backup',
        [ResolvedSource(source=source, root=camera)],
        volumes,
        _diskutil(camera='camera-uuid'),
    )

    assert sources == []


def _diskutil(
    **volumes: str,
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        mount = Path(command[-1])
        uuid = volumes[mount.name.casefold()]
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=plistlib.dumps(
                {
                    'Ejectable': True,
                    'Internal': False,
                    'VolumeName': mount.name,
                    'VolumeUUID': uuid,
                }
            ),
        )

    return run
