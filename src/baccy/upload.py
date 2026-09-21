import json
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from .catalog import Catalog
from .models import FileResult, ProjectUpload, ResolvedSource

Command = Callable[..., subprocess.CompletedProcess[bytes]]
_SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']


def publish_sessions(
    sources: list[ResolvedSource],
    projects: dict[str, ProjectUpload],
    backup_root: Path,
    dry_run: bool,
    run: Command = subprocess.run,
) -> list[FileResult]:
    catalog = Catalog(backup_root)
    results: list[FileResult] = []
    for source in sources:
        for journal in sorted(source.root.glob('**/session-record.jsonl')):
            if journal.is_symlink():
                continue
            relative_session = journal.parent.relative_to(source.root)
            if not relative_session.parts:
                continue
            project_name = relative_session.parts[0]
            if (project := projects.get(project_name)) is None:
                continue
            results.extend(
                _publish_session(
                    source.source.name,
                    journal.parent,
                    relative_session,
                    project_name,
                    project,
                    catalog,
                    dry_run,
                    run,
                )
            )
    return results


def _publish_session(
    source: str,
    session: Path,
    relative_session: Path,
    project_name: str,
    project: ProjectUpload,
    catalog: Catalog,
    dry_run: bool,
    run: Command,
) -> list[FileResult]:
    try:
        records = _finished_audio(session / 'session-record.jsonl')
        paths = [Path('session-record.jsonl')]
        if (session / 'recording.toml').is_file():
            paths.append(Path('recording.toml'))
        paths.extend(_selected_audio(records, project))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        if not dry_run:
            catalog.append(
                {
                    'operation': 'upload',
                    'source': project_name,
                    'relative_path': (
                        Path(source) / relative_session / 'session-record.jsonl'
                    ).as_posix(),
                    'result': 'failed',
                    'detail': str(error),
                }
            )
        return [FileResult(source=project_name, status='failed', detail=str(error))]
    results: list[FileResult] = []
    for relative_path in sorted(set(paths)):
        path = session / relative_path
        if not path.is_file():
            continue
        upload_path = relative_session / relative_path
        identity = Path(source) / upload_path
        if _matches_catalog(catalog, project_name, identity, path):
            results.append(
                FileResult(
                    source=project_name, relative_path=upload_path, status='unchanged'
                )
            )
            continue
        if dry_run:
            results.append(
                FileResult(
                    source=project_name,
                    relative_path=upload_path,
                    status='would_upload',
                )
            )
            continue
        try:
            _upload(path, upload_path, project.ssh_url, run)
        except OSError as error:
            if not dry_run:
                catalog.append(
                    {
                        'operation': 'upload',
                        'source': project_name,
                        'relative_path': identity.as_posix(),
                        'result': 'failed',
                        'detail': str(error),
                    }
                )
            results.append(
                FileResult(
                    source=project_name,
                    relative_path=upload_path,
                    status='failed',
                    detail=str(error),
                )
            )
            continue
        stat = path.stat()
        catalog.append(
            {
                'source': project_name,
                'relative_path': identity.as_posix(),
                'size': stat.st_size,
                'mtime_ns': stat.st_mtime_ns,
                'operation': 'upload',
                'result': 'uploaded',
            }
        )
        results.append(
            FileResult(
                source=project_name, relative_path=upload_path, status='uploaded'
            )
        )
    return results


def _finished_audio(journal: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with journal.open() as file:
        for line in file:
            if not line.endswith('\n'):
                break
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f'invalid recs record in {journal}')
            if (
                value.get('type') == 'file_finished'
                and value.get('media_type') == 'audio'
            ):
                records.append(value)
    return records


def _selected_audio(
    records: list[dict[str, object]], project: ProjectUpload
) -> list[Path]:
    if project.tracks is not None:
        return [
            Path(path)
            for record in records
            if isinstance(path := record.get('path'), str)
            and record.get('track_name') in project.tracks
            and _long_enough(record, project.minimum_seconds)
        ]
    channel_counts: dict[str, int] = {}
    for record in records:
        source = record.get('source')
        channels = record.get('source_channels')
        if (
            isinstance(source, str)
            and isinstance(channels, list)
            and all(isinstance(channel, int) for channel in channels)
        ):
            channel_counts[source] = max(channel_counts.get(source, 0), *channels)
    if not channel_counts:
        return []
    source, count = max(channel_counts.items(), key=lambda item: (item[1], item[0]))
    desired = {count - 1, count}
    return [
        Path(path)
        for record in records
        if isinstance(path := record.get('path'), str)
        and record.get('source') == source
        and isinstance(channels := record.get('source_channels'), list)
        and set(channels).issubset(desired)
        and channels
        and _long_enough(record, project.minimum_seconds)
    ]


def _long_enough(record: dict[str, object], minimum_seconds: float) -> bool:
    frames = record.get('frame_count')
    sample_rate = record.get('sample_rate')
    return (
        isinstance(frames, int)
        and isinstance(sample_rate, int)
        and sample_rate > 0
        and frames / sample_rate >= minimum_seconds
    )


def _matches_catalog(catalog: Catalog, project: str, path: Path, source: Path) -> bool:
    if (record := catalog.latest(project, path, 'upload')) is None:
        return False
    stat = source.stat()
    return (
        record.get('size') == stat.st_size
        and record.get('mtime_ns') == stat.st_mtime_ns
    )


def _upload(path: Path, upload_path: Path, ssh_url: str, run: Command) -> None:
    host, _, base = ssh_url.partition(':')
    destination = f'{base.rstrip("/")}/{upload_path.as_posix()}'
    directory = str(Path(destination).parent)
    remote = run(
        ['ssh', *_SSH_OPTIONS, host, f'mkdir -p {shlex.quote(directory)}'],
        capture_output=True,
        check=False,
    )
    if remote.returncode:
        raise OSError(remote.stderr.decode(errors='replace').strip())
    result = run(
        ['scp', *_SSH_OPTIONS, str(path), f'{host}:{shlex.quote(destination)}'],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise OSError(result.stderr.decode(errors='replace').strip())
