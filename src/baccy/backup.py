import errno
import fcntl
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .catalog import Catalog
from .copy import copy_candidate, preview_candidate
from .discovery import discover_removable_sources, resolve_sources
from .models import (
    BackupSummary,
    Candidate,
    FileResult,
    PathSource,
    RecognizedSource,
    ResolvedSource,
    Settings,
    Source,
    VolumeSource,
)
from .network import NetworkDiscovery, NetworkRecsSource, backup_network_source
from .scan import scan
from .upload import publish_sessions


def run_backup(
    settings: Settings,
    dry_run: bool = False,
    network: NetworkDiscovery | None = None,
    recognize: Callable[[list[RecognizedSource]], None] | None = None,
    recognize_machines: Callable[[list[RecognizedSource]], None] | None = None,
) -> BackupSummary:
    _validate_config_roots(settings)
    resolved, unavailable = resolve_sources(settings.sources)
    if settings.discover_removable:
        removable = discover_removable_sources(settings.backup_root, resolved)
        resolved.extend(removable)
    else:
        removable = []
    _validate_roots(settings.backup_root, resolved)
    discovery = NetworkDiscovery() if network is None else network
    discovery.verbose = settings.verbose
    network_sources = discovery.discover()
    if recognize_machines is not None:
        recognize_machines(
            [
                RecognizedSource(
                    source=machine.name, label=machine.host, kind='machine'
                )
                for machine in discovery.new_machines
            ]
        )
    if recognize is not None:
        recognize(
            [
                RecognizedSource(
                    source=source.source.name,
                    label=(
                        source.source.expected_name or source.root.name
                        if isinstance(source.source, VolumeSource)
                        else source.root.name
                    ),
                    kind='disk',
                )
                for source in removable
            ]
            + [
                RecognizedSource(source=source.name, label=source.host, kind='machine')
                for source in network_sources
            ]
        )
    if dry_run:
        return _run_candidates(
            settings, resolved, unavailable, network_sources, discovery, dry_run=True
        )
    with BackupLock(settings.backup_root):
        return _run_candidates(
            settings, resolved, unavailable, network_sources, discovery, dry_run=False
        )


def _run_candidates(
    settings: Settings,
    resolved: list[ResolvedSource],
    unavailable: list[Source],
    network_sources: list[NetworkRecsSource],
    network: NetworkDiscovery,
    dry_run: bool,
) -> BackupSummary:
    summary = BackupSummary()
    for source in unavailable:
        summary = summary.with_result(
            FileResult(source=source.name, status='unavailable')
        )
    candidates = [
        candidate.model_copy(update={'project': _project_name(candidate)})
        for source in resolved
        for candidate in scan(source)
    ]
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
        if result.status == 'deferred' and not dry_run:
            catalog.append_deferred(
                result.source, candidate.relative_path, result.detail
            )
    network_results: list[FileResult] = []
    for source in network_sources:
        network_results.extend(
            backup_network_source(
                source,
                settings.backup_root,
                catalog,
                dry_run,
                network.run,
            )
        )
    for result in network_results:
        summary = summary.with_result(result)
        if (
            result.status == 'deferred'
            and result.relative_path is not None
            and not dry_run
        ):
            catalog.append_deferred(result.source, result.relative_path, result.detail)
    for result in publish_sessions(
        _upload_sources(settings.backup_root),
        settings,
        dry_run,
    ):
        summary = summary.with_result(result)
        if (
            result.status == 'deferred'
            and result.relative_path is not None
            and not dry_run
        ):
            catalog.append_upload_deferred(
                result.source, result.relative_path, result.detail
            )
    return summary.model_copy(
        update={
            'discovered': summary.discovered
            + sum(result.relative_path is not None for result in network_results),
            'results': summary.results,
        }
    )


def _upload_sources(root: Path) -> list[ResolvedSource]:
    return [
        ResolvedSource(
            source=PathSource(kind='path', name='audio', path=root / 'audio'),
            root=root / 'audio',
        ),
    ]


def _project_name(candidate: Candidate) -> str | None:
    for parent in candidate.path.parents:
        if not parent.is_relative_to(candidate.source.root):
            break
        if (parent / 'session-record.jsonl').is_file():
            relative_session = parent.relative_to(candidate.source.root)
            return relative_session.parts[0] if relative_session.parts else None
    return None


class BackupLock:
    def __init__(self, backup_root: Path) -> None:
        self.path = backup_root / '.lock'
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
