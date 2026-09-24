from pathlib import Path

from .models import BackupSummary, PathSource, ResolvedSource, Settings
from .upload import publish_sessions


def sync(
    directories: list[Path], settings: Settings, dry_run: bool = False
) -> BackupSummary:
    root = (settings.backup_root / 'audio').resolve()
    selected = [_directory(root, directory) for directory in directories]
    source = ResolvedSource(
        source=PathSource(kind='path', name='backup', path=root), root=root
    )
    results = publish_sessions(
        [source], settings, dry_run, sync=True, directories=selected or None
    )
    summary = BackupSummary()
    for result in results:
        summary = summary.with_result(result)
    return summary


def _directory(root: Path, value: Path) -> Path:
    path = (root / value).resolve() if not value.is_absolute() else value.resolve()
    if not path.is_relative_to(root) or not path.is_dir():
        raise ValueError(f'sync directory is not within the backup root: {value}')
    return path
