import ast
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from botocore.exceptions import BotoCoreError, ClientError
from jinja2 import Template
from pydantic import BaseModel
from reccy.paths import legal_url_path

from .catalog import Catalog
from .match import MatchExpression
from .models import (
    Destination,
    FileResult,
    LandingPageUpload,
    ResolvedSource,
    S3Destination,
    Settings,
    SshDestination,
    UploadRule,
    parse_destination,
)
from .s3 import s3_client, s3_endpoint_url, s3_transfer_config

_SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']
_COMMAND_TIMEOUT_SECONDS = 4 * 60 * 60
_LOGGER = logging.getLogger(__name__)
_DEFAULT_LANDING_PAGE_TEMPLATE = (
    Path(__file__).parent / 'templates' / '_default.html'
).read_text()


class Segment(BaseModel, frozen=True):
    path: Path
    timestamp: str
    source: str
    channels: list[int]
    frame_count: int
    sample_rate: int
    track: str
    format: str

    @property
    def duration(self) -> float:
        return self.frame_count / self.sample_rate


class ArtifactPlan(BaseModel, frozen=True):
    project: str
    source_name: str
    session: Path
    segment: Segment
    rule: UploadRule
    destination: Destination
    target: PurePosixPath
    identity: str
    source_hash: str


class LandingPagePlan(BaseModel, frozen=True):
    project: str
    destination: Destination
    target: PurePosixPath
    identity: str
    content: str


def publish_sessions(
    sources: list[ResolvedSource],
    settings: Settings,
    dry_run: bool,
    sync: bool = False,
    directories: list[Path] | None = None,
    on_write: Callable[[FileResult], None] | None = None,
    catalog: Catalog | None = None,
    on_scan: Callable[[int], None] | None = None,
    verify: bool = False,
) -> list[FileResult]:
    if catalog is None:
        catalog = Catalog(settings.backup_root)
    if not dry_run:
        catalog.compact_if_needed()
    results: list[FileResult] = []
    expressions = {rule.name: MatchExpression(rule.match) for rule in settings.uploads}
    remote_targets: dict[str, dict[str, int]] = {}
    artifacts: list[ArtifactPlan] = []
    session_roots: list[Path] = []
    scanned = 0
    for source in sources:
        for journal in sorted(source.root.glob('**/session-record.jsonl')):
            if journal.is_symlink():
                continue
            if directories is not None and not any(
                journal.is_relative_to(directory) for directory in directories
            ):
                continue
            relative_session = journal.parent.relative_to(source.root)
            if not relative_session.parts:
                continue
            scanned += 1
            if on_scan is not None:
                on_scan(scanned)
            project_name = relative_session.parts[0]
            plans, failures = _publish_session(
                source.source.name,
                journal.parent,
                relative_session,
                project_name,
                settings,
                expressions,
                catalog,
                dry_run,
                sync,
            )
            artifacts.extend(plans)
            session_roots.extend([journal.parent] * len(plans))
            results.extend(failures)
    pages = [
        (page, landing_page.upload)
        for landing_page in settings.landing_pages
        for page in _landing_page_plans(artifacts, landing_page, settings)
    ]
    targets = Counter(
        (_destination_identity(plan.destination), plan.target.as_posix())
        for plan in [*artifacts, *(page for page, _ in pages)]
    )
    incomplete_audio: set[tuple[str, str, PurePosixPath]] = set()
    for plan, session_root in zip(artifacts, session_roots, strict=True):
        group = (plan.project, plan.rule.name, plan.target.parent)
        key = _destination_identity(plan.destination), plan.target.as_posix()
        if targets[key] > 1:
            results.append(
                FileResult(
                    source=plan.project,
                    relative_path=Path(plan.target),
                    status='deferred',
                    destination=_display_destination(plan.destination),
                    detail='upload target collides with another artifact',
                )
            )
            incomplete_audio.add(group)
            continue
        try:
            outcome = _materialize_and_upload(
                plan,
                session_root,
                catalog,
                dry_run,
                sync,
                remote_targets,
                on_write,
                verify,
            )
            results.extend(outcome)
            if any(
                result.status not in {'uploaded', 'unchanged', 'would_upload'}
                for result in outcome
            ):
                incomplete_audio.add(group)
        except (
            BotoCoreError,
            ClientError,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            results.extend(
                _record_failure(
                    catalog, plan.project, Path(plan.target), str(error), dry_run
                )
            )
            incomplete_audio.add(group)
    for plan, upload_name in pages:
        key = _destination_identity(plan.destination), plan.target.as_posix()
        if targets[key] > 1:
            results.append(
                _landing_page_result(
                    plan, 'deferred', 'upload target collides with another artifact'
                )
            )
            continue
        if (plan.project, upload_name, plan.target.parent) in incomplete_audio:
            results.append(
                _landing_page_result(
                    plan, 'deferred', 'linked audio upload is incomplete'
                )
            )
            continue
        try:
            results.extend(
                _materialize_and_upload_landing_page(
                    plan, catalog, dry_run, sync, remote_targets, on_write, verify
                )
            )
        except (
            BotoCoreError,
            ClientError,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            results.append(_landing_page_result(plan, 'failed', str(error)))
    return results


def planned_source_uploads(settings: Settings) -> list[ArtifactPlan]:
    root = settings.backup_root / 'audio'
    expressions = {rule.name: MatchExpression(rule.match) for rule in settings.uploads}
    plans: list[ArtifactPlan] = []
    for journal in sorted(root.glob('**/session-record.jsonl')):
        relative_session = journal.parent.relative_to(root)
        if not relative_session.parts:
            continue
        values, _ = _artifact_plans(
            _completed_segments(journal, warn_zero_frames=False),
            'backup',
            journal.parent,
            relative_session,
            relative_session.parts[0],
            settings,
            expressions,
            True,
        )
        plans.extend(plan for plan in values if plan.rule.encoding.format == 'source')
    return plans


def _publish_session(
    source_name: str,
    session_root: Path,
    relative_session: Path,
    project_name: str,
    settings: Settings,
    expressions: dict[str, MatchExpression],
    catalog: Catalog,
    dry_run: bool,
    sync: bool,
) -> tuple[list[ArtifactPlan], list[FileResult]]:
    try:
        segments = _completed_segments(session_root / 'session-record.jsonl')
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        return [], _record_failure(
            catalog,
            project_name,
            Path(source_name) / relative_session / 'session-record.jsonl',
            str(error),
            dry_run,
        )
    missing = [
        FileResult(
            source=project_name,
            relative_path=relative_session / segment.path,
            status='failed',
            detail='completed source backup is missing',
        )
        for segment in segments
        if not (session_root / segment.path).is_file()
    ]
    if missing:
        return [], missing
    return _artifact_plans(
        segments,
        source_name,
        session_root,
        relative_session,
        project_name,
        settings,
        expressions,
        sync,
        catalog,
    )


def _completed_segments(journal: Path, warn_zero_frames: bool = True) -> list[Segment]:
    starts: dict[tuple[str, str], dict[str, object]] = {}
    source_details: dict[str, tuple[str, int]] = {}
    segments: list[Segment] = []
    with journal.open() as file:
        for line in file:
            if not line.endswith('\n'):
                break
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f'invalid recs record in {journal}')
            record_type = value.get('type')
            if record_type == 'source_online':
                _record_source_details(value, source_details)
                continue
            if not _is_audio_record(value):
                continue
            stream_id = value.get('stream_id')
            path = value.get('path')
            if record_type not in {'file_started', 'file_finished'}:
                continue
            if not isinstance(stream_id, str) or not isinstance(path, str):
                raise ValueError(f'invalid audio lifecycle record in {journal}')
            identity = stream_id, path
            if record_type == 'file_started':
                starts[identity] = value
            elif (start := starts.get(identity)) is not None:
                if (
                    segment := _segment(
                        start, value, journal, source_details, warn_zero_frames
                    )
                ) is not None:
                    segments.append(segment)
    return segments


def _record_source_details(
    record: dict[str, object], source_details: dict[str, tuple[str, int]]
) -> None:
    clock_id = record.get('clock_id')
    source = record.get('source')
    sample_rate = record.get('sample_rate')
    if (
        isinstance(clock_id, str)
        and isinstance(source, str)
        and isinstance(sample_rate, int)
        and sample_rate > 0
    ):
        source_details[clock_id] = source, sample_rate


def _is_audio_record(record: dict[str, object]) -> bool:
    stream_id = record.get('stream_id')
    return record.get('media_type') == 'audio' or (
        isinstance(stream_id, str) and stream_id.startswith('audio:')
    )


def _segment(
    start: dict[str, object],
    finish: dict[str, object],
    journal: Path,
    source_details: dict[str, tuple[str, int]],
    warn_zero_frames: bool,
) -> Segment | None:
    path = start.get('path')
    timestamp = start.get('timestamp')
    source = start.get('source')
    channels = start.get('source_channels')
    frames = finish.get('frame_count')
    sample_rate = finish.get('sample_rate', start.get('sample_rate'))
    track = start.get('track_name')
    format_name = start.get('format', finish.get('format'))
    stream_id = start.get('stream_id')
    clock_id = start.get('clock_id')
    if (
        (not isinstance(source, str) or not isinstance(track, str))
        and isinstance(stream_id, str)
        and stream_id.startswith('audio:')
    ):
        source, track = _audio_stream_parts(stream_id, journal)
    if not isinstance(sample_rate, int) and isinstance(clock_id, str):
        details = source_details.get(clock_id)
        if details is not None:
            source = source if isinstance(source, str) else details[0]
            sample_rate = details[1]
    if not isinstance(format_name, str) and isinstance(path, str):
        format_name = Path(path).suffix.removeprefix('.')
    if (
        not isinstance(path, str)
        or PurePosixPath(path).is_absolute()
        or '..' in PurePosixPath(path).parts
        or not isinstance(timestamp, str)
        or not isinstance(source, str)
        or not isinstance(channels, list)
        or not all(isinstance(channel, int) and channel > 0 for channel in channels)
        or not isinstance(frames, int)
        or frames < 0
        or not isinstance(sample_rate, int)
        or sample_rate <= 0
        or not isinstance(format_name, str)
    ):
        raise ValueError(f'invalid completed audio record in {journal}')
    if not channels:
        return None
    if frames == 0 and warn_zero_frames:
        message = 'warning: ignoring zero frame count in completed audio record'
        if os.environ.get('BACCY_DAEMON') == '1':
            _LOGGER.warning('%s: %s', message, journal.parent / path)
        else:
            print(f'{message}: {journal.parent / path}', file=sys.stderr)
    return Segment(
        path=Path(path),
        timestamp=timestamp,
        source=source,
        channels=sorted(channels),
        frame_count=frames,
        sample_rate=sample_rate,
        track=track if isinstance(track, str) else '',
        format=format_name.lower(),
    )


def _audio_stream_parts(stream_id: str, journal: Path) -> tuple[str, str]:
    _, _, values = stream_id.partition(':')
    source, separator, track_and_capture = values.partition(':')
    track, _, _ = track_and_capture.partition(':')
    if not separator or not source or not track:
        raise ValueError(f'invalid audio stream ID in {journal}: {stream_id}')
    return unquote(source), unquote(track)


def _main_channels(
    segments: list[Segment],
) -> tuple[tuple[str, set[int]] | None, str | None]:
    widths: dict[str, int] = {}
    for segment in segments:
        widths[segment.source] = max(widths.get(segment.source, 0), *segment.channels)
    if not widths:
        return None, None
    maximum = max(widths.values())
    devices = [device for device, width in widths.items() if width == maximum]
    if len(devices) != 1:
        return None, 'main device is ambiguous'
    device = devices[0]
    return (device, set(range(max(1, maximum - 1), maximum + 1))), None


def _artifact_plans(
    segments: list[Segment],
    source_name: str,
    session_root: Path,
    relative_session: Path,
    project_name: str,
    settings: Settings,
    expressions: dict[str, MatchExpression],
    sync: bool,
    catalog: Catalog | None = None,
) -> tuple[list[ArtifactPlan], list[FileResult]]:
    plans: list[ArtifactPlan] = []
    results: list[FileResult] = []
    hashes: dict[Path, str] = {}
    named_main_tracks = {
        (segment.source, segment.track)
        for segment in segments
        if segment.track.casefold().startswith(('master', 'main'))
    }
    session_duration = max((segment.duration for segment in segments), default=0.0)
    main, main_error = (
        _main_channels(segments) if not named_main_tracks else (None, None)
    )
    for segment in segments:
        named_main = (segment.source, segment.track) in named_main_tracks
        is_main = named_main or (
            main is not None
            and segment.source == main[0]
            and set(segment.channels).issubset(main[1])
        )
        values = {
            'duration': (
                session_duration
                if named_main and segment.frame_count == 0
                else segment.duration
            ),
            'main': is_main,
            'device': segment.source,
            'channels': segment.channels,
            'track': segment.track,
            'format': segment.format,
            'player': None,
            'has_player': False,
        }
        for rule in settings.uploads:
            expression = expressions[rule.name]
            if main_error is not None and _expression_uses_main(expression):
                results.append(
                    FileResult(
                        source=project_name,
                        relative_path=segment.path,
                        status='deferred',
                        detail=main_error,
                    )
                )
                continue
            if not expression.matches(values):
                continue
            destination = parse_destination(rule.destination, settings.s3_max_bandwidth)
            try:
                target = _render_target(rule, relative_session, segment)
                source_path = session_root / segment.path
                record = (
                    catalog.latest_target(
                        _destination_identity(destination), target.as_posix()
                    )
                    if catalog is not None and not sync
                    else None
                )
                source_stat = source_path.stat() if record is not None else None
                if sync:
                    source_hash = segment.path.as_posix()
                elif (
                    record is not None
                    and source_stat is not None
                    and record.get('size') == source_stat.st_size
                    and record.get('mtime_ns') == source_stat.st_mtime_ns
                    and isinstance(record.get('source_hash'), str)
                ):
                    source_hash = str(record['source_hash'])
                else:
                    if source_path not in hashes:
                        hashes[source_path] = _source_hash(source_path)
                    source_hash = hashes[source_path]
                identity = _artifact_identity(
                    session=relative_session,
                    source_hash=source_hash,
                    segment=segment,
                    rule=rule,
                    destination=destination,
                )
            except (OSError, ValueError) as error:
                results.append(
                    FileResult(
                        source=project_name,
                        relative_path=segment.path,
                        status='deferred',
                        detail=str(error),
                    )
                )
                continue
            plans.append(
                ArtifactPlan(
                    project=project_name,
                    source_name=source_name,
                    session=relative_session,
                    segment=segment,
                    rule=rule,
                    destination=destination,
                    target=target,
                    identity=identity,
                    source_hash=source_hash,
                )
            )
    return plans, results


def _expression_uses_main(expression: MatchExpression) -> bool:
    return any(
        node.id == 'main'
        for node in ast.walk(expression.tree)
        if isinstance(node, ast.Name)
    )


def _render_target(rule: UploadRule, session: Path, segment: Segment) -> PurePosixPath:
    if rule.encoding.format == 'mp3':
        filename = Path(segment.path.name.rsplit(' + ', maxsplit=1)[-1]).with_suffix(
            '.mp3'
        )
        target = PurePosixPath(session.parts[0]) / PurePosixPath(filename.as_posix())
        return _legal_target(target)
    suffix = (
        segment.path.suffix
        if rule.encoding.format == 'source'
        else f'.{rule.encoding.format}'
    )
    target = PurePosixPath(session.as_posix()) / PurePosixPath(
        segment.path.with_suffix(suffix).as_posix()
    )
    return _legal_target(target)


def _legal_target(path: PurePosixPath) -> PurePosixPath:
    value = PurePosixPath(legal_url_path(Path(path.as_posix())).as_posix())
    if value.is_absolute() or '..' in value.parts or value == PurePosixPath('.'):
        raise ValueError(f'invalid web-safe upload target: {value}')
    return value


def _landing_page_plans(
    artifacts: list[ArtifactPlan],
    landing_page: LandingPageUpload,
    settings: Settings,
) -> list[LandingPagePlan]:
    grouped: dict[tuple[str, PurePosixPath], list[ArtifactPlan]] = {}
    for artifact in artifacts:
        if artifact.rule.name == landing_page.upload:
            grouped.setdefault((artifact.project, artifact.target.parent), []).append(
                artifact
            )
    destination = parse_destination(landing_page.destination, settings.s3_max_bandwidth)
    plans: list[LandingPagePlan] = []
    for (project_name, directory), values in grouped.items():
        target = _legal_target(directory / 'index.html')
        try:
            project: dict[str, object] = {'name': project_name}
            if landing_page.template is None:
                template = _DEFAULT_LANDING_PAGE_TEMPLATE
            else:
                project = _load_project(project_name)
                templates = project.get('templates', {})
                if not isinstance(templates, dict):
                    raise ValueError('recording project templates must be a dictionary')
                template = templates[landing_page.template]
        except (KeyError, ValueError) as error:
            plans.append(
                LandingPagePlan(
                    project=project_name,
                    destination=destination,
                    target=target,
                    identity='',
                    content=str(error),
                )
            )
            continue
        urls = [
            value.target.name
            for value in sorted(values, key=lambda value: value.target)
        ]
        content = Template(template).render(**project, urls=urls)
        identity = hashlib.sha256(
            json.dumps(
                {
                    'landing_page': landing_page.model_dump(mode='json'),
                    'project': project,
                    'target': target.as_posix(),
                    'urls': urls,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        plans.append(
            LandingPagePlan(
                project=project_name,
                destination=destination,
                target=target,
                identity=identity,
                content=content,
            )
        )
    return plans


def _materialize_and_upload_landing_page(
    plan: LandingPagePlan,
    catalog: Catalog,
    dry_run: bool,
    sync: bool,
    remote_targets: dict[str, dict[str, int]],
    on_write: Callable[[FileResult], None] | None,
    verify: bool,
) -> list[FileResult]:
    if not plan.identity:
        return [_landing_page_result(plan, 'failed', plan.content)]
    if dry_run:
        return [_landing_page_result(plan, 'would_upload')]
    destination_id = _destination_identity(plan.destination)
    if sync:
        if destination_id not in remote_targets:
            remote_targets[destination_id] = _remote_targets(plan.destination)
        remote_size = remote_targets[destination_id].get(plan.target.as_posix())
        metadata_matches = remote_size == len(plan.content.encode()) and (
            not isinstance(plan.destination, S3Destination)
            or _remote_s3_identity(plan.destination, plan.target) == plan.identity
        )
        if metadata_matches and verify:
            metadata_matches = _remote_hash(plan.destination, plan.target) == (
                hashlib.sha256(plan.content.encode()).hexdigest()
            )
        if metadata_matches:
            return [_landing_page_result(plan, 'unchanged')]
    elif catalog.latest(plan.project, Path(plan.identity), 'landing_page') is not None:
        return [_landing_page_result(plan, 'unchanged')]
    if on_write is not None:
        on_write(_landing_page_result(plan, 'writing'))
    path = _materialize_landing_page(plan, catalog.path.parent)
    try:
        uploaded = _upload_landing_page(plan, path)
    except (BotoCoreError, ClientError, OSError, subprocess.SubprocessError) as error:
        return [_landing_page_result(plan, 'failed', str(error))]
    if sync:
        remote_targets[destination_id][plan.target.as_posix()] = path.stat().st_size
    if uploaded:
        try:
            catalog.append(
                {
                    'operation': 'landing_page',
                    'source': plan.project,
                    'relative_path': plan.identity,
                    'result': 'uploaded',
                    'destination': destination_id,
                    'target': plan.target.as_posix(),
                    'artifact_size': path.stat().st_size,
                }
            )
        except OSError as error:
            return [
                _landing_page_result(
                    plan,
                    'failed',
                    f'remote page is present but local catalog update failed: {error}',
                )
            ]
        return [_landing_page_result(plan, 'uploaded')]
    return [_landing_page_result(plan, 'unchanged')]


def _landing_page_result(
    plan: LandingPagePlan, status: str, detail: str | None = None
) -> FileResult:
    return FileResult(
        source=plan.project,
        relative_path=Path(plan.target),
        status=status,
        destination=_display_destination(plan.destination),
        detail=detail,
    )


def _load_project(name: str) -> dict[str, object]:
    path = Path.home() / '.config' / 'recs' / 'projects' / f'{name}.json'
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'could not read recording project {name}: {error}') from error
    if not isinstance(value, dict):
        raise ValueError(f'invalid recording project: {path}')
    return value


def _materialize_landing_page(plan: LandingPagePlan, backup_root: Path) -> Path:
    output = backup_root / 'artifacts' / plan.identity / 'index.html'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(plan.content)
    return output


def _upload_landing_page(plan: LandingPagePlan, path: Path) -> bool:
    if isinstance(plan.destination, SshDestination):
        _upload_ssh(path, plan.target, plan.destination)
        return True
    return _upload_s3(path, plan.target, plan.destination, plan.identity)


def _artifact_identity(
    session: Path,
    source_hash: str,
    segment: Segment,
    rule: UploadRule,
    destination: Destination,
) -> str:
    value = {
        'session': session.as_posix(),
        'source_hash': source_hash,
        'segment': segment.model_dump(mode='json'),
        'rule': rule.model_dump(mode='json', by_alias=True),
        'destination': destination.model_dump(mode='json'),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(1_048_576):
            digest.update(block)
    return digest.hexdigest()


def _materialize_and_upload(
    plan: ArtifactPlan,
    session_root: Path,
    catalog: Catalog,
    dry_run: bool,
    sync: bool,
    remote_targets: dict[str, dict[str, int]],
    on_write: Callable[[FileResult], None] | None,
    verify: bool,
) -> list[FileResult]:
    source = session_root / plan.segment.path
    if not source.is_file():
        return _record_failure(
            catalog,
            plan.project,
            Path(plan.identity),
            'completed source backup is missing',
            dry_run,
        )
    destination_id = _destination_identity(plan.destination)
    if dry_run:
        return [
            FileResult(
                source=plan.project,
                relative_path=Path(plan.target),
                status='would_upload',
                destination=_display_destination(plan.destination),
            )
        ]
    if sync:
        if destination_id not in remote_targets:
            remote_targets[destination_id] = _remote_targets(plan.destination)
        targets = remote_targets[destination_id]
        record = catalog.latest_target(destination_id, plan.target.as_posix())
        source_stat = source.stat()
        expected_size = (
            source_stat.st_size
            if plan.rule.encoding.format == 'source'
            else record.get('artifact_size')
            if record is not None
            else None
        )
        remote_size = targets.get(plan.target.as_posix())
        metadata_matches = remote_size is not None and (
            remote_size == expected_size
            if isinstance(expected_size, int)
            else remote_size > 0
        )
        if record is not None and (
            record.get('size') != source_stat.st_size
            or record.get('mtime_ns') != source_stat.st_mtime_ns
        ):
            metadata_matches = False
        if (
            metadata_matches
            and isinstance(plan.destination, S3Destination)
            and (record is not None)
        ):
            metadata_matches = _remote_s3_identity(
                plan.destination, plan.target
            ) == record.get('relative_path')
        if metadata_matches and verify:
            artifact = _materialize(plan, source, catalog.path.parent)
            try:
                metadata_matches = _source_hash(artifact) == _remote_hash(
                    plan.destination, plan.target
                )
            finally:
                if plan.rule.encoding.format == 'mp3':
                    artifact.unlink(missing_ok=True)
        if metadata_matches:
            return [
                FileResult(
                    source=plan.project,
                    relative_path=Path(plan.target),
                    status='unchanged',
                    destination=_display_destination(plan.destination),
                )
            ]
    elif _matches_catalog(catalog, plan.project, Path(plan.identity), source):
        return [
            FileResult(
                source=plan.project, relative_path=Path(plan.target), status='unchanged'
            )
        ]
    if on_write is not None:
        on_write(
            FileResult(
                source=plan.project,
                relative_path=Path(plan.target),
                status='writing',
                destination=_display_destination(plan.destination),
            )
        )
    try:
        artifact = _materialize(plan, source, catalog.path.parent)
        try:
            artifact_size = artifact.stat().st_size
            uploaded = _upload(plan, artifact)
        finally:
            if plan.rule.encoding.format == 'mp3':
                artifact.unlink(missing_ok=True)
    except (BotoCoreError, ClientError, OSError, subprocess.SubprocessError) as error:
        return _record_failure(
            catalog, plan.project, Path(plan.identity), str(error), dry_run
        )
    if sync:
        remote_targets[destination_id][plan.target.as_posix()] = artifact_size
    status = 'uploaded' if uploaded else 'unchanged'
    try:
        stat = source.stat()
        event: dict[str, object] = {
            'source': plan.project,
            'relative_path': plan.identity,
            'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns,
            'operation': 'upload',
            'result': status,
            'destination': destination_id,
            'target': plan.target.as_posix(),
            'artifact_size': artifact_size,
        }
        if not sync:
            event['source_hash'] = plan.source_hash
        if uploaded:
            event.update(
                {'rule': plan.rule.name, 'encoding': plan.rule.encoding.model_dump()}
            )
        catalog.append(event)
    except OSError as error:
        return [
            FileResult(
                source=plan.project,
                relative_path=Path(plan.target),
                status='failed',
                destination=_display_destination(plan.destination),
                detail=f'remote file present; catalog update failed: {error}',
            )
        ]
    return [
        FileResult(
            source=plan.project,
            relative_path=Path(plan.target),
            status=status,
            destination=_display_destination(plan.destination),
        )
    ]


def _materialize(plan: ArtifactPlan, source: Path, backup_root: Path) -> Path:
    if plan.rule.encoding.format == 'source':
        return source
    extension = plan.rule.encoding.format
    if extension == 'mp3':
        with tempfile.NamedTemporaryFile(
            dir='/tmp', suffix='.mp3', delete=False
        ) as file:
            temporary = Path(file.name)
        output = temporary
    else:
        output = backup_root / 'artifacts' / plan.identity / f'artifact.{extension}'
        if output.is_file():
            return output
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=output.parent, suffix=f'.{extension}', delete=False
        ) as file:
            temporary = Path(file.name)
    try:
        command = ['ffmpeg', '-y', '-i', str(source)]
        if extension == 'mp3':
            command.extend(['-b:a', f'{plan.rule.encoding.bitrate_kbps}k'])
        else:
            command.extend(['-c:a', 'flac'])
        command.append(str(temporary))
        subprocess.run(
            command, capture_output=True, check=True, timeout=_COMMAND_TIMEOUT_SECONDS
        )
        if extension != 'mp3':
            temporary.replace(output)
    except OSError, subprocess.SubprocessError, KeyboardInterrupt:
        temporary.unlink(missing_ok=True)
        raise
    return output


def _upload(plan: ArtifactPlan, path: Path) -> bool:
    if isinstance(plan.destination, SshDestination):
        _upload_ssh(path, plan.target, plan.destination)
        return True
    else:
        return _upload_s3(path, plan.target, plan.destination, plan.identity)


def _upload_ssh(
    path: Path,
    target: PurePosixPath,
    destination: SshDestination,
) -> None:
    host, _, base = destination.url.partition(':')
    remote_path = f'{base.rstrip("/")}/{target.as_posix()}'
    directory = str(PurePosixPath(remote_path).parent)
    _run(['ssh', *_SSH_OPTIONS, host, f'mkdir -p {shlex.quote(directory)}'])
    _run(['scp', *_SSH_OPTIONS, str(path), f'{host}:{remote_path}'])


def _upload_s3(
    path: Path,
    target: PurePosixPath,
    destination: S3Destination,
    identity: str,
) -> bool:
    client = s3_client(destination)
    key = '/'.join(part for part in (destination.prefix, target.as_posix()) if part)
    try:
        existing = client.head_object(Bucket=destination.bucket, Key=key)
    except ClientError as error:
        if error.response['Error'].get('Code') not in {'404', 'NoSuchKey', 'NotFound'}:
            raise
    else:
        if (
            existing.get('Metadata', {}).get('baccy-identity') == identity
            and existing.get('ContentLength') == path.stat().st_size
        ):
            return False
    extra = {'Metadata': {'baccy-identity': identity}}
    arguments = {'ExtraArgs': extra}
    client.upload_file(
        str(path),
        destination.bucket,
        key,
        Config=s3_transfer_config(destination),
        **arguments,
    )
    return True


def _remote_s3_identity(
    destination: S3Destination, target: PurePosixPath
) -> str | None:
    key = '/'.join(part for part in (destination.prefix, target.as_posix()) if part)
    try:
        value = s3_client(destination).head_object(Bucket=destination.bucket, Key=key)
    except ClientError as error:
        if error.response['Error'].get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
            return None
        raise
    identity = value.get('Metadata', {}).get('baccy-identity')
    return identity if isinstance(identity, str) else None


def _remote_hash(destination: Destination, target: PurePosixPath) -> str | None:
    if isinstance(destination, SshDestination):
        host, _, base = destination.url.partition(':')
        path = shlex.quote(f'{base.rstrip("/")}/{target.as_posix()}')
        result = subprocess.run(
            [
                'ssh',
                *_SSH_OPTIONS,
                host,
                f'sha256sum -- {path} 2>/dev/null || shasum -a 256 -- {path}',
            ],
            capture_output=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode:
            raise OSError(result.stderr.decode(errors='replace').strip())
        fields = result.stdout.decode(errors='replace').split(maxsplit=1)
        digest = fields[0] if fields else ''
        if re.fullmatch(r'[0-9a-fA-F]{64}', digest) is None:
            raise OSError('invalid SSH SHA-256 result')
        return digest.lower()
    key = '/'.join(part for part in (destination.prefix, target.as_posix()) if part)
    try:
        response = s3_client(destination).get_object(Bucket=destination.bucket, Key=key)
    except ClientError as error:
        if error.response['Error'].get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
            return None
        raise
    digest = hashlib.sha256()
    body = response['Body']
    started = time.monotonic()
    transferred = 0
    try:
        for chunk in body.iter_chunks(chunk_size=1_048_576):
            digest.update(chunk)
            transferred += len(chunk)
            if (
                delay := transferred / destination.max_bandwidth
                - (time.monotonic() - started)
            ) > 0:
                time.sleep(delay)
    finally:
        body.close()
    return digest.hexdigest()


def _run(command: list[str]) -> None:
    result = subprocess.run(
        command, capture_output=True, check=False, timeout=_COMMAND_TIMEOUT_SECONDS
    )
    if result.returncode:
        raise OSError(result.stderr.decode(errors='replace').strip())


def _matches_catalog(
    catalog: Catalog, project: str, identity: Path, source: Path
) -> bool:
    if (record := catalog.latest(project, identity, 'upload')) is None:
        return False
    stat = source.stat()
    return (
        record.get('size') == stat.st_size
        and record.get('mtime_ns') == stat.st_mtime_ns
    )


def _destination_identity(destination: Destination) -> str:
    if isinstance(destination, SshDestination):
        return destination.url
    endpoint = s3_endpoint_url(destination) or 'aws'
    return f'{endpoint}/{destination.bucket}/{destination.prefix}'


def _display_destination(destination: Destination) -> str:
    if isinstance(destination, S3Destination):
        return f's3:{destination.bucket}'
    return f'ssh:{destination.url}'


def _remote_targets(destination: Destination) -> dict[str, int]:
    if isinstance(destination, SshDestination):
        host, _, base = destination.url.partition(':')
        result = subprocess.run(
            [
                'ssh',
                *_SSH_OPTIONS,
                host,
                f"find {shlex.quote(base)} -type f -exec sh -c '"
                'for path do '
                'size=$(stat -c %s "$path" 2>/dev/null || stat -f %z "$path") || exit; '
                'printf "%s\\0%s\\0" "$path" "$size"; '
                "done' sh {} +",
            ],
            capture_output=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode:
            raise OSError(result.stderr.decode(errors='replace').strip())
        prefix = f'{base.rstrip("/")}/'
        fields = result.stdout.split(b'\0')
        if fields.pop() != b'' or len(fields) % 2:
            raise OSError('incomplete SSH remote listing')
        try:
            return {
                path.decode(errors='surrogateescape').removeprefix(prefix): int(size)
                for path, size in zip(fields[::2], fields[1::2], strict=True)
                if path.decode(errors='surrogateescape').startswith(prefix)
            }
        except ValueError as error:
            raise OSError('invalid SSH remote listing') from error
    client = s3_client(destination)
    prefix = destination.prefix.rstrip('/')
    values: dict[str, int] = {}
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=destination.bucket, Prefix=prefix):
        for value in page.get('Contents', []):
            if isinstance(key := value.get('Key'), str) and isinstance(
                size := value.get('Size'), int
            ):
                values[key.removeprefix(f'{prefix}/')] = size
    return values


def _record_failure(
    catalog: Catalog, project: str, path: Path, detail: str, dry_run: bool
) -> list[FileResult]:
    if not dry_run:
        try:
            catalog.append(
                {
                    'operation': 'upload',
                    'source': project,
                    'relative_path': path.as_posix(),
                    'result': 'failed',
                    'detail': detail,
                }
            )
        except OSError as error:
            _LOGGER.error('could not record upload failure: %s', error)
    return [
        FileResult(source=project, relative_path=path, status='failed', detail=detail)
    ]
