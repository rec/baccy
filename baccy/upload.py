import ast
import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

from botocore.exceptions import BotoCoreError, ClientError
from jinja2 import Template
from pydantic import BaseModel

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
)
from .s3 import s3_client, s3_endpoint_url

_SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']
_DEFAULT_LANDING_PAGE_TEMPLATE = """<!doctype html>
<html>
<head><title>{{ name }}</title></head>
<body>
<ul>
{% for url in urls %}
<li><a href="{{ url }}">{{ url.rsplit('/', 1)[-1] }}</a></li>
{% endfor %}
</ul>
</body>
</html>
"""


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
) -> list[FileResult]:
    catalog = Catalog(settings.backup_root)
    results: list[FileResult] = []
    expressions = {rule.name: MatchExpression(rule.match) for rule in settings.uploads}
    remote_targets: dict[str, set[str]] = {}
    artifacts: list[ArtifactPlan] = []
    missing = _missing_sources(sources, directories)
    if missing:
        return missing
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
            project_name = relative_session.parts[0]
            results.extend(
                _publish_session(
                    source.source.name,
                    journal.parent,
                    relative_session,
                    project_name,
                    settings,
                    expressions,
                    catalog,
                    dry_run,
                    sync,
                    remote_targets,
                    artifacts,
                )
            )
    results.extend(
        _publish_landing_pages(
            artifacts, settings, catalog, dry_run, sync, remote_targets
        )
    )
    return results


def _missing_sources(
    sources: list[ResolvedSource], directories: list[Path] | None
) -> list[FileResult]:
    missing: list[FileResult] = []
    for source in sources:
        for journal in sorted(source.root.glob('**/session-record.jsonl')):
            if journal.is_symlink() or (
                directories is not None
                and not any(
                    journal.is_relative_to(directory) for directory in directories
                )
            ):
                continue
            relative_session = journal.parent.relative_to(source.root)
            if not relative_session.parts:
                continue
            try:
                segments = _completed_segments(journal, warn_zero_frames=False)
            except OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError:
                continue
            for segment in segments:
                if not (journal.parent / segment.path).is_file():
                    missing.append(
                        FileResult(
                            source=relative_session.parts[0],
                            relative_path=relative_session / segment.path,
                            status='failed',
                            detail='completed source backup is missing',
                        )
                    )
    return missing


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
    remote_targets: dict[str, set[str]],
    artifacts: list[ArtifactPlan],
) -> list[FileResult]:
    try:
        segments = _completed_segments(session_root / 'session-record.jsonl')
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        return _record_failure(
            catalog,
            project_name,
            Path(source_name) / relative_session / 'session-record.jsonl',
            str(error),
            dry_run,
        )
    plans, results = _artifact_plans(
        segments,
        source_name,
        session_root,
        relative_session,
        project_name,
        settings,
        expressions,
        sync,
    )
    artifacts.extend(plans)
    targets = [
        (_destination_identity(plan.destination), plan.target.as_posix())
        for plan in plans
    ]
    for plan in plans:
        target_key = _destination_identity(plan.destination), plan.target.as_posix()
        if targets.count(target_key) > 1:
            results.append(
                FileResult(
                    source=project_name,
                    relative_path=Path(plan.target),
                    status='deferred',
                    detail='upload target collides with another artifact',
                )
            )
            continue
        results.extend(
            _materialize_and_upload(
                plan, session_root, catalog, dry_run, sync, remote_targets
            )
        )
    return results


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
) -> tuple[list[ArtifactPlan], list[FileResult]]:
    plans: list[ArtifactPlan] = []
    results: list[FileResult] = []
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
            destination = settings.destinations[rule.destination]
            try:
                target = _render_target(rule, relative_session, segment)
                identity = _artifact_identity(
                    session=relative_session,
                    source_hash=(
                        segment.path.as_posix()
                        if sync
                        else _source_hash(session_root / segment.path)
                    ),
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
        return PurePosixPath(session.parts[0]) / PurePosixPath(filename.as_posix())
    suffix = (
        segment.path.suffix
        if rule.encoding.format == 'source'
        else f'.{rule.encoding.format}'
    )
    return PurePosixPath(session.as_posix()) / PurePosixPath(
        segment.path.with_suffix(suffix).as_posix()
    )


def _publish_landing_pages(
    artifacts: list[ArtifactPlan],
    settings: Settings,
    catalog: Catalog,
    dry_run: bool,
    sync: bool,
    remote_targets: dict[str, set[str]],
) -> list[FileResult]:
    results: list[FileResult] = []
    for landing_page in settings.landing_pages:
        for plan in _landing_page_plans(artifacts, landing_page, settings):
            results.extend(
                _materialize_and_upload_landing_page(
                    plan, catalog, dry_run, sync, remote_targets
                )
            )
    return results


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
    destination = settings.destinations[landing_page.destination]
    plans: list[LandingPagePlan] = []
    for (project_name, directory), values in grouped.items():
        try:
            project = _load_project(project_name)
            templates = project.get('templates', {})
            if not isinstance(templates, dict):
                raise ValueError('recording project templates must be a dictionary')
            template = (
                _DEFAULT_LANDING_PAGE_TEMPLATE
                if landing_page.template is None
                else templates[landing_page.template]
            )
        except (KeyError, ValueError) as error:
            plans.append(
                LandingPagePlan(
                    project=project_name,
                    destination=destination,
                    target=directory / 'index.html',
                    identity='',
                    content=str(error),
                )
            )
            continue
        urls = [
            f'{landing_page.url_prefix}/{quote(value.target.as_posix())}'
            for value in sorted(values, key=lambda value: value.target)
        ]
        content = Template(template).render(**project, urls=urls)
        identity = hashlib.sha256(
            json.dumps(
                {
                    'landing_page': landing_page.model_dump(mode='json'),
                    'project': project,
                    'urls': urls,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        plans.append(
            LandingPagePlan(
                project=project_name,
                destination=destination,
                target=directory / 'index.html',
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
    remote_targets: dict[str, set[str]],
) -> list[FileResult]:
    if not plan.identity:
        return [_landing_page_result(plan, 'failed', plan.content)]
    if dry_run:
        return [_landing_page_result(plan, 'would_upload')]
    destination_id = _destination_identity(plan.destination)
    if sync:
        if destination_id not in remote_targets:
            remote_targets[destination_id] = _remote_targets(plan.destination)
        if plan.target.as_posix() in remote_targets[destination_id]:
            return [_landing_page_result(plan, 'unchanged')]
    elif catalog.latest(plan.project, Path(plan.identity), 'landing_page') is not None:
        return [_landing_page_result(plan, 'unchanged')]
    path = _materialize_landing_page(plan, catalog.path.parent)
    try:
        uploaded = _upload_landing_page(plan, path)
    except (BotoCoreError, ClientError, OSError, subprocess.SubprocessError) as error:
        return [_landing_page_result(plan, 'failed', str(error))]
    if sync:
        remote_targets[destination_id].add(plan.target.as_posix())
    if uploaded:
        catalog.append(
            {
                'operation': 'landing_page',
                'source': plan.project,
                'relative_path': plan.identity,
                'result': 'uploaded',
                'destination': destination_id,
                'target': plan.target.as_posix(),
            }
        )
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
    remote_targets: dict[str, set[str]],
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
        if plan.target.as_posix() in targets:
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
    try:
        artifact = _materialize(plan, source, catalog.path.parent)
        uploaded = _upload(plan, artifact)
    except (BotoCoreError, ClientError, OSError, subprocess.SubprocessError) as error:
        return _record_failure(
            catalog, plan.project, Path(plan.identity), str(error), dry_run
        )
    stat = source.stat()
    if sync:
        remote_targets[destination_id].add(plan.target.as_posix())
    if not uploaded:
        catalog.append(
            {
                'source': plan.project,
                'relative_path': plan.identity,
                'size': stat.st_size,
                'mtime_ns': stat.st_mtime_ns,
                'operation': 'upload',
                'result': 'unchanged',
            }
        )
        return [
            FileResult(
                source=plan.project,
                relative_path=Path(plan.target),
                status='unchanged',
                destination=_display_destination(plan.destination),
            )
        ]
    catalog.append(
        {
            'source': plan.project,
            'relative_path': plan.identity,
            'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns,
            'operation': 'upload',
            'result': 'uploaded',
            'rule': plan.rule.name,
            'encoding': plan.rule.encoding.model_dump(),
            'destination': _destination_identity(plan.destination),
            'target': plan.target.as_posix(),
        }
    )
    return [
        FileResult(
            source=plan.project,
            relative_path=Path(plan.target),
            status='uploaded',
            destination=_display_destination(plan.destination),
        )
    ]


def _materialize(plan: ArtifactPlan, source: Path, backup_root: Path) -> Path:
    if plan.rule.encoding.format == 'source':
        return source
    extension = plan.rule.encoding.format
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
        subprocess.run(command, capture_output=True, check=True)
        temporary.replace(output)
    except OSError, subprocess.SubprocessError:
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
    _run(['scp', *_SSH_OPTIONS, str(path), f'{host}:{shlex.quote(remote_path)}'])


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
        if existing.get('Metadata', {}).get('baccy-identity') == identity:
            return False
    extra = {'Metadata': {'baccy-identity': identity}}
    arguments = {'ExtraArgs': extra}
    client.upload_file(str(path), destination.bucket, key, **arguments)
    return True


def _run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, check=False)
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
        return destination.bucket
    return destination.url


def _remote_targets(destination: Destination) -> set[str]:
    if isinstance(destination, SshDestination):
        host, _, base = destination.url.partition(':')
        result = subprocess.run(
            ['ssh', *_SSH_OPTIONS, host, f'find {shlex.quote(base)} -type f -print'],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            return set()
        prefix = f'{base.rstrip("/")}/'
        return {
            path.removeprefix(prefix)
            for path in result.stdout.decode(errors='replace').splitlines()
            if path.startswith(prefix)
        }
    client = s3_client(destination)
    prefix = destination.prefix.rstrip('/')
    values: set[str] = set()
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=destination.bucket, Prefix=prefix):
        for value in page.get('Contents', []):
            if isinstance(key := value.get('Key'), str):
                values.add(key.removeprefix(f'{prefix}/'))
    return values


def _record_failure(
    catalog: Catalog, project: str, path: Path, detail: str, dry_run: bool
) -> list[FileResult]:
    if not dry_run:
        catalog.append(
            {
                'operation': 'upload',
                'source': project,
                'relative_path': path.as_posix(),
                'result': 'failed',
                'detail': detail,
            }
        )
    return [
        FileResult(source=project, relative_path=path, status='failed', detail=detail)
    ]
