import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from .backup import BackupLock
from .catalog import Catalog
from .models import BackupSummary, FileResult, Settings


def import_recs(
    directories: list[Path],
    settings: Settings,
    copy_directories: bool,
    project: str | None,
    dry_run: bool = False,
) -> BackupSummary:
    if project is not None and (
        not project or Path(project).parts != (project,) or project in {'.', '..'}
    ):
        raise ValueError('project must be a single path component')
    if dry_run:
        return _preview_import(directories, settings, project)
    imported: list[FileResult] = []
    with BackupLock(settings.backup_root):
        if (settings.backup_root / 'rename-progress.json').exists():
            raise ValueError('a rename is pending; rerun the original rename command')
        catalog = Catalog(settings.backup_root)
        catalog.compact_if_needed()
        for directory in directories:
            for session in _sessions(directory):
                project_name = (
                    project
                    or _project_name(session)
                    or _directory_project(directory, session)
                )
                if project_name is None:
                    raise ValueError(
                        f'cannot determine the project for session: {session}; '
                        'use --project'
                    )
                destination = (
                    settings.backup_root
                    / 'audio'
                    / project_name
                    / _session_relative(directory, session, project_name)
                )
                if destination.exists() or destination.is_symlink():
                    raise FileExistsError(
                        f'import destination already exists: {destination}'
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(
                    prefix='.baccy-import-', dir=destination.parent
                ) as temporary_directory:
                    staged = Path(temporary_directory) / session.name
                    shutil.copytree(session, staged, symlinks=not copy_directories)
                    _verify_session(session, staged)
                    staged.replace(destination)
                catalog.append(
                    {
                        'operation': 'import',
                        'source': str(session),
                        'relative_path': destination.relative_to(
                            settings.backup_root
                        ).as_posix(),
                        'result': 'copied',
                    }
                )
                if not copy_directories:
                    shutil.rmtree(session)
                imported.append(
                    FileResult(
                        source=project_name,
                        relative_path=destination.relative_to(settings.backup_root),
                        status='copied',
                    )
                )
    return BackupSummary.from_results(imported)


def _preview_import(
    directories: list[Path], settings: Settings, project: str | None
) -> BackupSummary:
    results: list[FileResult] = []
    for directory in directories:
        for session in _sessions(directory):
            project_name = (
                project
                or _project_name(session)
                or _directory_project(directory, session)
            )
            if project_name is None:
                raise ValueError(
                    f'cannot determine the project for session: {session}; '
                    'use --project'
                )
            destination = (
                settings.backup_root
                / 'audio'
                / project_name
                / _session_relative(directory, session, project_name)
            )
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(
                    f'import destination already exists: {destination}'
                )
            results.append(
                FileResult(
                    source=project_name,
                    relative_path=destination.relative_to(settings.backup_root),
                    status='would_copy',
                )
            )
    return BackupSummary.from_results(results)


def _sessions(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise ValueError(f'import path is not a directory: {directory}')
    sessions: list[Path] = []
    pending = [directory]
    while pending:
        current = pending.pop()
        journal = current / 'session-record.jsonl'
        if journal.is_file():
            _project_name(current)
            sessions.append(current)
            continue
        pending.extend(
            path
            for path in current.iterdir()
            if path.is_dir() and not path.is_symlink()
        )
    if not sessions:
        raise ValueError(f'import path contains no recs sessions: {directory}')
    return sorted(sessions)


def _verify_session(source: Path, staged: Path) -> None:
    for path in source.rglob('*'):
        copied = staged / path.relative_to(source)
        if path.is_file() and (
            not copied.is_file() or copied.stat().st_size != path.stat().st_size
        ):
            raise OSError(f'import copy differs from source: {path}')
        if path.is_dir() and not copied.is_dir():
            raise OSError(f'import copy is missing a directory: {path}')


def _project_name(session: Path) -> str | None:
    journal = session / 'session-record.jsonl'
    try:
        with journal.open() as file:
            header = json.loads(file.readline())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f'invalid recs session journal: {journal}') from error
    if not isinstance(header, dict) or header.get('type') != 'header':
        raise ValueError(f'invalid recs session header: {journal}')
    project = header.get('project_name')
    if project is None:
        return None
    if (
        not isinstance(project, str)
        or not project
        or '/' in project
        or project in {'.', '..'}
    ):
        raise ValueError(f'invalid recs project name in {journal}')
    return project


def _directory_project(directory: Path, session: Path) -> str | None:
    if session == directory:
        return None
    relative = session.relative_to(directory)
    name = relative.parts[0] if not relative.parts[0].isdecimal() else directory.name
    if not name or '/' in name or name in {'.', '..'}:
        return None
    return name


def _session_relative(directory: Path, session: Path, project: str) -> Path:
    if session != directory:
        relative = session.relative_to(directory)
        if relative.parts[0] == project:
            return Path(*relative.parts[1:])
        return relative
    try:
        with (session / 'session-record.jsonl').open() as file:
            header = json.loads(file.readline())
        started_at = header['started_at']
        timestamp = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            f'cannot determine the date path for session: {session}'
        ) from error
    return (
        Path(f'{timestamp:%Y}') / f'{timestamp:%m}' / f'{timestamp:%d}' / session.name
    )
