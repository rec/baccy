import errno
import fcntl
from pathlib import Path
from typing import TextIO

from .catalog import Catalog
from .copy import copy_candidate, preview_candidate
from .discovery import discover_removable_sources, resolve_sources
from .models import (
    BackupSummary,
    FileResult,
    PathSource,
    ResolvedSource,
    Settings,
    Source,
)
from .scan import scan


def run_backup(settings: Settings, dry_run: bool = False) -> BackupSummary:
    _validate_config_roots(settings)
    resolved, unavailable = resolve_sources(settings.sources)
    if settings.discover_removable:
        resolved.extend(discover_removable_sources(settings.backup_root, resolved))
    _validate_roots(settings.backup_root, resolved)
    if dry_run:
        return _run_candidates(settings, resolved, unavailable, dry_run=True)
    with BackupLock(settings.backup_root):
        return _run_candidates(settings, resolved, unavailable, dry_run=False)


def _run_candidates(
    settings: Settings,
    resolved: list[ResolvedSource],
    unavailable: list[Source],
    dry_run: bool,
) -> BackupSummary:
    summary = BackupSummary()
    for source in unavailable:
        summary = summary.with_result(
            FileResult(source=source.name, status='unavailable')
        )
    candidates = [candidate for source in resolved for candidate in scan(source)]
    summary = summary.model_copy(
        update={'discovered': len(candidates), 'results': summary.results}
    )
    catalog = Catalog(settings.backup_root)
    for candidate in candidates:
        try:
            result = (
                preview_candidate(
                    candidate,
                    settings.backup_root,
                    catalog,
                    settings.stability_seconds,
                )
                if dry_run
                else copy_candidate(
                    candidate,
                    settings.backup_root,
                    catalog,
                    settings.stability_seconds,
                )
            )
        except OSError as error:
            result = FileResult(
                source=candidate.source.source.name,
                relative_path=candidate.relative_path,
                status='failed',
                detail=str(error),
            )
            if not dry_run:
                catalog.append(
                    {
                        'source': candidate.source.source.name,
                        'relative_path': candidate.relative_path.as_posix(),
                        'result': 'failed',
                        'detail': str(error),
                    }
                )
            summary = summary.with_result(result)
            if error.errno in {errno.ENOSPC, errno.EROFS}:
                break
        else:
            summary = summary.with_result(result)
    return summary


class BackupLock:
    def __init__(self, backup_root: Path) -> None:
        self.path = backup_root / '.baccy' / 'lock'
        self.file: TextIO | None = None

    def __enter__(self) -> BackupLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a')
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.file.close()
            self.file = None
            raise RuntimeError(
                f'backup root is already locked: {self.path.parent}'
            ) from error
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if file := self.file:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            file.close()


def _validate_roots(backup_root: Path, sources: list[ResolvedSource]) -> None:
    destination = backup_root.resolve()
    roots = [source.root.resolve() for source in sources]
    for root in roots:
        if destination.is_relative_to(root) or root.is_relative_to(destination):
            raise ValueError('backup root and source roots must not overlap')
    for index, root in enumerate(roots):
        if any(
            root.is_relative_to(other) or other.is_relative_to(root)
            for other in roots[:index]
        ):
            raise ValueError('source roots must not overlap')


def _validate_config_roots(settings: Settings) -> None:
    sources = [
        ResolvedSource(source=source, root=source.path)
        for source in settings.sources
        if isinstance(source, PathSource)
    ]
    _validate_roots(settings.backup_root, sources)
