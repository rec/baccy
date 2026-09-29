import ast
import hashlib
import json
import logging
import os
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from jinja2 import Template
from pydantic import BaseModel
from reccy.paths import legal_url_path

from . import models
from .catalog import Catalog
from .match import MatchExpression
from .s3 import s3_endpoint_url

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
    rule: models.UploadRule
    destination: models.Destination
    target: PurePosixPath
    identity: str
    source_hash: str


class LandingPagePlan(BaseModel, frozen=True):
    project: str
    destination: models.Destination
    target: PurePosixPath
    identity: str
    content: str


def planned_source_uploads(settings: models.Settings) -> list[ArtifactPlan]:
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
    settings: models.Settings,
    expressions: dict[str, MatchExpression],
    sync: bool,
    catalog: Catalog | None = None,
) -> tuple[list[ArtifactPlan], list[models.FileResult]]:
    plans: list[ArtifactPlan] = []
    results: list[models.FileResult] = []
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
                    models.FileResult(
                        source=project_name,
                        relative_path=segment.path,
                        status='deferred',
                        detail=main_error,
                    )
                )
                continue
            if not expression.matches(values):
                continue
            destination = models.parse_destination(
                rule.destination, settings.s3_max_bandwidth
            )
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
                    models.FileResult(
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


def _render_target(
    rule: models.UploadRule, session: Path, segment: Segment
) -> PurePosixPath:
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
    landing_page: models.LandingPageUpload,
    settings: models.Settings,
) -> list[LandingPagePlan]:
    grouped: dict[tuple[str, PurePosixPath], list[ArtifactPlan]] = {}
    for artifact in artifacts:
        if artifact.rule.name == landing_page.upload:
            grouped.setdefault((artifact.project, artifact.target.parent), []).append(
                artifact
            )
    destination = models.parse_destination(
        landing_page.destination, settings.s3_max_bandwidth
    )
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


def _load_project(name: str) -> dict[str, object]:
    path = Path.home() / '.config' / 'recs' / 'projects' / f'{name}.json'
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'could not read recording project {name}: {error}') from error
    if not isinstance(value, dict):
        raise ValueError(f'invalid recording project: {path}')
    return value


def _artifact_identity(
    session: Path,
    source_hash: str,
    segment: Segment,
    rule: models.UploadRule,
    destination: models.Destination,
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


def _destination_identity(destination: models.Destination) -> str:
    if isinstance(destination, models.SshDestination):
        return destination.address
    endpoint = s3_endpoint_url(destination) or 'aws'
    return f'{endpoint}/{destination.bucket}/{destination.prefix}'
