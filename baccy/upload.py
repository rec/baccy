import hashlib
import json
import logging
import re
import shlex
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from botocore.exceptions import BotoCoreError, ClientError

from . import models, upload_plan
from .catalog import Catalog
from .match import MatchExpression
from .s3 import s3_client, s3_transfer_config
from .ssh import SSH_OPTIONS

_COMMAND_TIMEOUT_SECONDS = 4 * 60 * 60
_LOGGER = logging.getLogger(__name__)


def publish_sessions(
    sources: list[models.ResolvedSource],
    settings: models.Settings,
    dry_run: bool,
    sync: bool = False,
    directories: list[Path] | None = None,
    on_write: Callable[[models.FileResult], None] | None = None,
    catalog: Catalog | None = None,
    on_scan: Callable[[int], None] | None = None,
    verify: bool = False,
) -> list[models.FileResult]:
    if catalog is None:
        catalog = Catalog(settings.backup_root)
    if not dry_run:
        catalog.compact_if_needed()
    results: list[models.FileResult] = []
    expressions = {rule.name: MatchExpression(rule.match) for rule in settings.uploads}
    remote_targets: dict[str, dict[str, int]] = {}
    artifacts: list[upload_plan.ArtifactPlan] = []
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
        for page in upload_plan._landing_page_plans(artifacts, landing_page, settings)
    ]
    targets = Counter(
        (upload_plan._destination_identity(plan.destination), plan.target.as_posix())
        for plan in [*artifacts, *(page for page, _ in pages)]
    )
    incomplete_audio: set[tuple[str, str, PurePosixPath]] = set()
    for plan, session_root in zip(artifacts, session_roots, strict=True):
        group = (plan.project, plan.rule.name, plan.target.parent)
        key = (
            upload_plan._destination_identity(plan.destination),
            plan.target.as_posix(),
        )
        if targets[key] > 1:
            results.append(
                models.FileResult(
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
        key = (
            upload_plan._destination_identity(plan.destination),
            plan.target.as_posix(),
        )
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


def _publish_session(
    source_name: str,
    session_root: Path,
    relative_session: Path,
    project_name: str,
    settings: models.Settings,
    expressions: dict[str, MatchExpression],
    catalog: Catalog,
    dry_run: bool,
    sync: bool,
) -> tuple[list[upload_plan.ArtifactPlan], list[models.FileResult]]:
    try:
        segments = upload_plan._completed_segments(
            session_root / 'session-record.jsonl'
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        return [], _record_failure(
            catalog,
            project_name,
            Path(source_name) / relative_session / 'session-record.jsonl',
            str(error),
            dry_run,
        )
    missing = [
        models.FileResult(
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
    return upload_plan._artifact_plans(
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


def _materialize_and_upload_landing_page(
    plan: upload_plan.LandingPagePlan,
    catalog: Catalog,
    dry_run: bool,
    sync: bool,
    remote_targets: dict[str, dict[str, int]],
    on_write: Callable[[models.FileResult], None] | None,
    verify: bool,
) -> list[models.FileResult]:
    if not plan.identity:
        return [_landing_page_result(plan, 'failed', plan.content)]
    if dry_run:
        return [_landing_page_result(plan, 'would_upload')]
    destination_id = upload_plan._destination_identity(plan.destination)
    if sync:
        if destination_id not in remote_targets:
            remote_targets[destination_id] = _remote_targets(plan.destination)
        remote_size = remote_targets[destination_id].get(plan.target.as_posix())
        metadata_matches = remote_size == len(plan.content.encode()) and (
            not isinstance(plan.destination, models.S3Destination)
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
    plan: upload_plan.LandingPagePlan, status: str, detail: str | None = None
) -> models.FileResult:
    return models.FileResult(
        source=plan.project,
        relative_path=Path(plan.target),
        status=status,
        destination=_display_destination(plan.destination),
        detail=detail,
    )


def _materialize_landing_page(
    plan: upload_plan.LandingPagePlan, backup_root: Path
) -> Path:
    output = backup_root / 'artifacts' / plan.identity / 'index.html'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(plan.content)
    return output


def _upload_landing_page(plan: upload_plan.LandingPagePlan, path: Path) -> bool:
    if isinstance(plan.destination, models.SshDestination):
        _upload_ssh(path, plan.target, plan.destination)
        return True
    return _upload_s3(path, plan.target, plan.destination, plan.identity)


def _materialize_and_upload(
    plan: upload_plan.ArtifactPlan,
    session_root: Path,
    catalog: Catalog,
    dry_run: bool,
    sync: bool,
    remote_targets: dict[str, dict[str, int]],
    on_write: Callable[[models.FileResult], None] | None,
    verify: bool,
) -> list[models.FileResult]:
    source = session_root / plan.segment.path
    if not source.is_file():
        return _record_failure(
            catalog,
            plan.project,
            Path(plan.identity),
            'completed source backup is missing',
            dry_run,
        )
    destination_id = upload_plan._destination_identity(plan.destination)
    if dry_run:
        return [
            models.FileResult(
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
            and isinstance(plan.destination, models.S3Destination)
            and (record is not None)
        ):
            metadata_matches = _remote_s3_identity(
                plan.destination, plan.target
            ) == record.get('relative_path')
        if metadata_matches and verify:
            artifact = _materialize(plan, source, catalog.path.parent)
            try:
                metadata_matches = upload_plan._source_hash(artifact) == _remote_hash(
                    plan.destination, plan.target
                )
            finally:
                if plan.rule.encoding.format == 'mp3':
                    artifact.unlink(missing_ok=True)
        if metadata_matches:
            return [
                models.FileResult(
                    source=plan.project,
                    relative_path=Path(plan.target),
                    status='unchanged',
                    destination=_display_destination(plan.destination),
                )
            ]
    elif _matches_catalog(catalog, plan.project, Path(plan.identity), source):
        return [
            models.FileResult(
                source=plan.project, relative_path=Path(plan.target), status='unchanged'
            )
        ]
    if on_write is not None:
        on_write(
            models.FileResult(
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
            models.FileResult(
                source=plan.project,
                relative_path=Path(plan.target),
                status='failed',
                destination=_display_destination(plan.destination),
                detail=f'remote file present; catalog update failed: {error}',
            )
        ]
    return [
        models.FileResult(
            source=plan.project,
            relative_path=Path(plan.target),
            status=status,
            destination=_display_destination(plan.destination),
        )
    ]


def _materialize(
    plan: upload_plan.ArtifactPlan, source: Path, backup_root: Path
) -> Path:
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


def _upload(plan: upload_plan.ArtifactPlan, path: Path) -> bool:
    if isinstance(plan.destination, models.SshDestination):
        _upload_ssh(path, plan.target, plan.destination)
        return True
    else:
        return _upload_s3(path, plan.target, plan.destination, plan.identity)


def _upload_ssh(
    path: Path,
    target: PurePosixPath,
    destination: models.SshDestination,
) -> None:
    host, base = destination.host, destination.root
    remote_path = f'{base.rstrip("/")}/{target.as_posix()}'
    directory = str(PurePosixPath(remote_path).parent)
    _run(['ssh', *SSH_OPTIONS, host, f'mkdir -p {shlex.quote(directory)}'])
    _run(['scp', *SSH_OPTIONS, str(path), f'{destination.scp_host}:{remote_path}'])


def _upload_s3(
    path: Path,
    target: PurePosixPath,
    destination: models.S3Destination,
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
    destination: models.S3Destination, target: PurePosixPath
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


def _remote_hash(destination: models.Destination, target: PurePosixPath) -> str | None:
    if isinstance(destination, models.SshDestination):
        host, base = destination.host, destination.root
        path = shlex.quote(f'{base.rstrip("/")}/{target.as_posix()}')
        result = subprocess.run(
            [
                'ssh',
                *SSH_OPTIONS,
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


def _display_destination(destination: models.Destination) -> str:
    if isinstance(destination, models.S3Destination):
        suffix = f'/{destination.prefix}' if destination.prefix else ''
        return f's3:{destination.bucket}{suffix}'
    return f'ssh:{destination.address}'


def _remote_targets(destination: models.Destination) -> dict[str, int]:
    if isinstance(destination, models.SshDestination):
        host, base = destination.host, destination.root
        result = subprocess.run(
            [
                'ssh',
                *SSH_OPTIONS,
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
) -> list[models.FileResult]:
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
        models.FileResult(
            source=project, relative_path=path, status='failed', detail=detail
        )
    ]
