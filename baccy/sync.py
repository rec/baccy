from pathlib import Path

from .backup import BackupLock
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
    if dry_run:
        results = publish_sessions(
            [source], settings, dry_run=True, sync=True, directories=selected or None
        )
    else:
        with BackupLock(settings.backup_root):
            if (settings.backup_root / 'rename-progress.json').exists():
                raise ValueError(
                    'a rename is pending; rerun the original rename command'
                )
            results = publish_sessions(
                [source],
                settings,
                dry_run=False,
                sync=True,
                directories=selected or None,
            )
    return BackupSummary.from_results(results)


def _directory(root: Path, value: Path) -> Path:
    path = (root / value).resolve() if not value.is_absolute() else value.resolve()
    if not path.is_relative_to(root) or not path.is_dir():
        raise ValueError(f'sync directory is not within the backup root: {value}')
    return path
