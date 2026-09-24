import json
import shutil
from datetime import datetime
from pathlib import Path

from .backup import BackupLock
from .catalog import Catalog
from .models import BackupSummary, FileResult, PathSource, ResolvedSource, Settings
from .upload import publish_sessions


def import_recs(
    directories: list[Path],
    settings: Settings,
    copy_directories: bool,
    project: str | None,
) -> BackupSummary:
    imported: list[FileResult] = []
    with BackupLock(settings.backup_root):
        catalog = Catalog(settings.backup_root)
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
                    / _session_relative(directory, session)
                )
                if destination.exists():
                    raise FileExistsError(
                        f'import destination already exists: {destination}'
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if copy_directories:
                    shutil.copytree(session, destination)
                else:
                    shutil.move(str(session), str(destination))
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
                imported.append(
                    FileResult(
                        source=project_name,
                        relative_path=destination.relative_to(settings.backup_root),
                        status='copied',
                    )
                )
        source = ResolvedSource(
            source=PathSource(
                kind='path', name='audio', path=settings.backup_root / 'audio'
            ),
            root=settings.backup_root / 'audio',
        )
        uploads = publish_sessions([source], settings, False)
    summary = BackupSummary()
    for result in [*imported, *uploads]:
        summary = summary.with_result(result)
    return summary


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
    name = directory.name
    if not name or '/' in name or name in {'.', '..'}:
        return None
    return name


def _session_relative(directory: Path, session: Path) -> Path:
    if session != directory:
        return session.relative_to(directory)
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
