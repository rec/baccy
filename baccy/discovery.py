import json
import plistlib
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

from .models import (
    PathSource,
    ResolvedSource,
    Source,
    SourceSelection,
    VolumeSource,
)

_RECS_MARKERS = {'recording.toml', 'session-record.jsonl'}


def resolve_sources(
    sources: list[Source],
    volumes_root: Path = Path('/Volumes'),
    diskutil: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> tuple[list[ResolvedSource], list[Source]]:
    resolved: list[ResolvedSource] = []
    unavailable: list[Source] = []
    for source in sources:
        root = resolve_source(source, volumes_root, diskutil)
        if root is None:
            unavailable.append(source)
        else:
            resolved.append(ResolvedSource(source=source, root=root))
    return resolved, unavailable


def resolve_source(
    source: Source,
    volumes_root: Path,
    diskutil: Callable[..., subprocess.CompletedProcess[bytes]],
) -> Path | None:
    if isinstance(source, PathSource):
        return source.path if source.path.is_dir() else None
    if not isinstance(source, VolumeSource):
        return None
    return _resolve_volume(source, volumes_root, diskutil)


def discover_removable_sources(
    backup_root: Path,
    configured: list[ResolvedSource],
    volumes_root: Path = Path('/Volumes'),
    diskutil: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> list[ResolvedSource]:
    try:
        mounts = sorted(volumes_root.iterdir())
    except FileNotFoundError:
        return []
    excluded_roots = [backup_root.resolve(), *(s.root.resolve() for s in configured)]
    seen_uuids = {
        s.source.uuid.casefold()
        for s in configured
        if isinstance(s.source, VolumeSource)
    }
    sources: list[ResolvedSource] = []
    for mount in mounts:
        if not mount.is_dir() or mount.is_symlink():
            continue
        root = mount.resolve()
        if any(
            root.is_relative_to(excluded) or excluded.is_relative_to(root)
            for excluded in excluded_roots
        ):
            continue
        data = _disk_info(mount, diskutil)
        if data is None or not _is_removable(data):
            continue
        uuid = data.get('VolumeUUID')
        if not isinstance(uuid, str) or not uuid:
            continue
        normalized_uuid = uuid.casefold()
        if normalized_uuid in seen_uuids:
            continue
        selections = _automatic_selections(mount)
        if not selections:
            continue
        volume_name = data.get('VolumeName')
        source = VolumeSource(
            kind='volume',
            name=f'removable-{normalized_uuid}',
            uuid=uuid,
            expected_name=volume_name if isinstance(volume_name, str) else mount.name,
        )
        sources.append(ResolvedSource(source=source, root=mount, selections=selections))
        seen_uuids.add(normalized_uuid)
    return sources


def _resolve_volume(
    source: VolumeSource,
    volumes_root: Path,
    diskutil: Callable[..., subprocess.CompletedProcess[bytes]],
) -> Path | None:
    try:
        mounts = sorted(volumes_root.iterdir())
    except FileNotFoundError:
        return None
    for mount in mounts:
        if not mount.is_dir() or mount.is_symlink():
            continue
        if (data := _disk_info(mount, diskutil)) is None:
            continue
        if data.get('VolumeUUID') != source.uuid:
            continue
        root = mount / source.relative_path
        return root if root.is_dir() else None
    return None


def _disk_info(
    mount: Path,
    diskutil: Callable[..., subprocess.CompletedProcess[bytes]],
) -> dict[str, object] | None:
    result = diskutil(
        ['diskutil', 'info', '-plist', str(mount)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = plistlib.loads(result.stdout)
    return value if isinstance(value, dict) else None


def _is_removable(data: dict[str, object]) -> bool:
    return data.get('Internal') is not True and (
        data.get('RemovableMedia') is True or data.get('Ejectable') is True
    )


def _automatic_selections(root: Path) -> list[SourceSelection]:
    selections = [
        SourceSelection(
            relative_root=directory.relative_to(root),
            extensions=_PHOTO_EXTENSIONS,
        )
        for directory in _camera_directories(root)
    ]
    selections.extend(
        SourceSelection(relative_root=directory.relative_to(root))
        for directory in _recs_session_directories(root)
    )
    return selections


def _camera_directories(root: Path) -> list[Path]:
    try:
        return [
            p
            for p in root.iterdir()
            if p.name.casefold() == 'dcim' and not p.is_symlink() and p.is_dir()
        ]
    except FileNotFoundError, PermissionError:
        return []


def _recs_session_directories(root: Path) -> list[Path]:
    pending = [root]
    sessions: list[Path] = []
    while pending:
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name)
        except FileNotFoundError, PermissionError:
            continue
        if any(
            not path.is_symlink()
            and path.name in _RECS_MARKERS
            and path.is_file()
            and _is_recs_marker(path)
            for path in children
        ):
            sessions.append(directory)
            continue
        for path in children:
            if path.is_symlink():
                continue
            if path.is_dir():
                pending.append(path)
    return sessions


def _is_recs_marker(path: Path) -> bool:
    if path.name == 'recording.toml':
        try:
            with path.open('rb') as file:
                value = tomllib.load(file)
        except OSError, tomllib.TOMLDecodeError:
            return False
        return value.get('format') == 'recs' and value.get('kind') == 'recording'
    try:
        with path.open() as file:
            value = json.loads(file.readline())
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return False
    return isinstance(value, dict) and value.get('type') == 'header'


_PHOTO_EXTENSIONS = [
    '.3fr',
    '.arw',
    '.cr2',
    '.cr3',
    '.crw',
    '.dng',
    '.erf',
    '.fff',
    '.gpr',
    '.heic',
    '.heif',
    '.iiq',
    '.jpeg',
    '.jpg',
    '.kdc',
    '.mef',
    '.mos',
    '.mrw',
    '.nef',
    '.nrw',
    '.orf',
    '.pef',
    '.png',
    '.raf',
    '.raw',
    '.rwl',
    '.rw2',
    '.sr2',
    '.srw',
    '.srf',
    '.tif',
    '.tiff',
    '.x3f',
]
