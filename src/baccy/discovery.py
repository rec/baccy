import plistlib
import subprocess
from collections.abc import Callable
from pathlib import Path

from .models import PathSource, ResolvedSource, Source, VolumeSource


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
    return _resolve_volume(source, volumes_root, diskutil)


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
        result = diskutil(
            ['diskutil', 'info', '-plist', str(mount)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            continue
        data = plistlib.loads(result.stdout)
        if data.get('VolumeUUID') != source.uuid:
            continue
        root = mount / source.relative_path
        return root if root.is_dir() else None
    return None
