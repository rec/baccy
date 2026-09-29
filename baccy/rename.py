import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field
from reccy.paths import legal_url_path

from .backup import BackupLock
from .models import S3Destination, Settings
from .s3 import s3_client, s3_endpoint_url, s3_transfer_config
from .upload import planned_source_uploads


class RemoteRename(BaseModel, frozen=True):
    destination: S3Destination
    source_key: str
    replacement_key: str


class RenameFile(BaseModel, frozen=True):
    session: Path
    source: Path
    replacement: Path
    targets: list[RemoteRename]


class RenameProgress(BaseModel, frozen=True):
    pattern: str
    replacement: str
    regular_expression: bool
    files: list[RenameFile]
    copied: list[int] = Field(default_factory=list)
    renamed: list[str] = Field(default_factory=list)
    journals: list[str] = Field(default_factory=list)
    deleted: list[int] = Field(default_factory=list)


def renamed_files(
    settings: Settings, pattern: str, replacement: str, regular_expression: bool
) -> list[RenameFile]:
    if (pending := _read_progress(settings.backup_root)) is not None:
        if (pending.pattern, pending.replacement, pending.regular_expression) != (
            pattern,
            replacement,
            regular_expression,
        ):
            raise ValueError('finish the pending rename with its original arguments')
        return pending.files
    expression = re.compile(pattern) if regular_expression else None
    values: dict[Path, RenameFile] = {}
    for plan in planned_source_uploads(settings):
        if not isinstance(plan.destination, S3Destination):
            continue
        name = (
            expression.sub(replacement, plan.segment.path.name)
            if expression is not None
            else plan.segment.path.name.replace(pattern, replacement)
        )
        if name == plan.segment.path.name:
            continue
        source = plan.session / plan.segment.path
        new_path = source.with_name(name)
        prefix = plan.destination.prefix
        destination = plan.destination.model_copy(
            update={'endpoint_url': s3_endpoint_url(plan.destination)}
        )
        target = RemoteRename(
            destination=destination,
            source_key='/'.join(p for p in (prefix, plan.target.as_posix()) if p),
            replacement_key='/'.join(
                p for p in (prefix, legal_url_path(new_path).as_posix()) if p
            ),
        )
        value = values.get(source)
        if value is None:
            values[source] = RenameFile(
                session=plan.session,
                source=source,
                replacement=new_path,
                targets=[target],
            )
        elif target not in value.targets:
            values[source] = value.model_copy(
                update={'targets': [*value.targets, target]}
            )
    return sorted(values.values(), key=lambda value: value.source)


def rename_files(
    settings: Settings,
    files: list[RenameFile],
    pattern: str,
    replacement: str,
    regular_expression: bool = False,
) -> bool:
    root = settings.backup_root / 'audio'
    with BackupLock(settings.backup_root):
        progress = _read_progress(settings.backup_root)
        if progress is None:
            _validate_files(root, files)
            try:
                _validate_remote_targets(root, files)
            except (BotoCoreError, ClientError, ValueError) as error:
                _log(settings.backup_root, {'error': str(error)})
                return False
            progress = RenameProgress(
                pattern=pattern,
                replacement=replacement,
                regular_expression=regular_expression,
                files=files,
            )
            _write_progress(settings.backup_root, progress)
            _log(settings.backup_root, {'pattern': pattern, 'replacement': replacement})
        elif (progress.pattern, progress.replacement, progress.regular_expression) != (
            pattern,
            replacement,
            regular_expression,
        ) or progress.files != files:
            raise ValueError('finish the pending rename with its original arguments')
        try:
            _finish_rename(settings.backup_root, progress)
        except (BotoCoreError, ClientError, OSError, ValueError) as error:
            _log(settings.backup_root, {'error': str(error)})
            return False
        _progress_path(settings.backup_root).unlink()
    return True


def _finish_rename(backup_root: Path, progress: RenameProgress) -> None:
    root = backup_root / 'audio'
    targets = [target for value in progress.files for target in value.targets]
    for index, target in enumerate(targets):
        if index in progress.copied:
            continue
        _log(backup_root, {'s3': f's3:{target.destination.bucket}/{target.source_key}'})
        client = s3_client(target.destination)
        old = _head(client, target.destination.bucket, target.source_key)
        new = _head(client, target.destination.bucket, target.replacement_key)
        if old is None:
            raise ValueError(f'S3 source is missing: {target.source_key}')
        if new is None:
            client.copy(
                Bucket=target.destination.bucket,
                Key=target.replacement_key,
                CopySource={
                    'Bucket': target.destination.bucket,
                    'Key': target.source_key,
                },
                Config=s3_transfer_config(target.destination),
            )
            new = _head(client, target.destination.bucket, target.replacement_key)
        if new is None or not _same_object(old, new):
            raise ValueError(f'S3 copy could not be verified: {target.replacement_key}')
        progress = progress.model_copy(update={'copied': [*progress.copied, index]})
        _write_progress(backup_root, progress)
        _log(backup_root, {'ok': True})
    for value in progress.files:
        key = value.source.as_posix()
        if key in progress.renamed:
            continue
        old = root / value.source
        new = root / value.replacement
        if old.exists() and not new.exists():
            old.rename(new)
        elif old.exists() or not new.is_file():
            raise ValueError(f'local rename state is ambiguous: {value.source}')
        progress = progress.model_copy(update={'renamed': [*progress.renamed, key]})
        _write_progress(backup_root, progress)
    sessions: dict[Path, list[RenameFile]] = {}
    for value in progress.files:
        sessions.setdefault(value.session, []).append(value)
    for session, values in sessions.items():
        key = session.as_posix()
        if key in progress.journals:
            continue
        if _rewrite_session(root / session / 'session-record.jsonl', values):
            _log(backup_root, {'message': f'Session {session} rename complete'})
        progress = progress.model_copy(update={'journals': [*progress.journals, key]})
        _write_progress(backup_root, progress)
    for index, target in enumerate(targets):
        if index in progress.deleted:
            continue
        client = s3_client(target.destination)
        if _head(client, target.destination.bucket, target.replacement_key) is None:
            raise ValueError(f'S3 replacement is missing: {target.replacement_key}')
        if _head(client, target.destination.bucket, target.source_key) is not None:
            client.delete_object(
                Bucket=target.destination.bucket, Key=target.source_key
            )
        progress = progress.model_copy(update={'deleted': [*progress.deleted, index]})
        _write_progress(backup_root, progress)


def _validate_files(root: Path, files: list[RenameFile]) -> None:
    sources = {value.source for value in files}
    replacements = {value.replacement for value in files}
    if len(replacements) != len(files):
        raise ValueError('rename replacements are not unique')
    for value in files:
        if any(
            p.is_absolute() or '..' in p.parts
            for p in (value.source, value.replacement)
        ):
            raise ValueError('rename paths must stay within the backup audio directory')
        if (root / value.source).is_symlink() or not (root / value.source).is_file():
            raise ValueError(f'backup audio file does not exist: {value.source}')
        if value.replacement in sources or (root / value.replacement).exists():
            raise ValueError(f'rename target already exists: {value.replacement}')
        journal = root / value.session / 'session-record.jsonl'
        if journal.is_symlink():
            raise ValueError(f'session journal is a symlink: {journal}')
        old = value.source.relative_to(value.session).as_posix()
        if not any(
            isinstance(record := json.loads(line), dict) and record.get('path') == old
            for line in journal.read_text().splitlines()
        ):
            raise ValueError(f'session does not name the file: {value.source}')


def _validate_remote_targets(root: Path, files: list[RenameFile]) -> None:
    targets = [
        (target.destination.bucket, target.replacement_key)
        for value in files
        for target in value.targets
    ]
    if len(set(targets)) != len(targets):
        raise ValueError('S3 rename targets are not unique')
    for value in files:
        local_size = (root / value.source).stat().st_size
        for target in value.targets:
            client = s3_client(target.destination)
            source = _head(client, target.destination.bucket, target.source_key)
            if source is None:
                raise ValueError(f'S3 source is missing: {target.source_key}')
            if source.get('ContentLength') != local_size:
                raise ValueError(
                    f'S3 source size differs from local file: {target.source_key}'
                )
            if (
                _head(client, target.destination.bucket, target.replacement_key)
                is not None
            ):
                raise ValueError(
                    f'S3 replacement already exists: {target.replacement_key}'
                )


def _head(client: BaseClient, bucket: str, key: str) -> dict[str, object] | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response['Error'].get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
            return None
        raise


def _same_object(old: dict[str, object], new: dict[str, object]) -> bool:
    if old.get('ContentLength') != new.get('ContentLength'):
        return False
    old_metadata = old.get('Metadata')
    new_metadata = new.get('Metadata')
    old_identity = (
        old_metadata.get('baccy-identity') if isinstance(old_metadata, dict) else None
    )
    new_identity = (
        new_metadata.get('baccy-identity') if isinstance(new_metadata, dict) else None
    )
    return (old_identity is not None and old_identity == new_identity) or (
        old.get('ETag') is not None and old.get('ETag') == new.get('ETag')
    )


def _rewrite_session(path: Path, files: list[RenameFile]) -> bool:
    replacements = {
        value.source.relative_to(value.session).as_posix(): (
            value.replacement.relative_to(value.session).as_posix()
        )
        for value in files
    }
    values: list[str] = []
    present: set[str] = set()
    changed = False
    for line in path.read_text().splitlines():
        value = json.loads(line)
        if isinstance(value, dict) and isinstance(name := value.get('path'), str):
            if name in replacements:
                value['path'] = replacements[name]
                changed = True
                present.add(replacements[name])
            else:
                present.add(name)
        values.append(json.dumps(value, separators=(',', ':')))
    if not set(replacements.values()).issubset(present):
        raise ValueError(f'session no longer names every replacement: {path}')
    if changed:
        _atomic_write(path, '\n'.join(values) + '\n')
    return changed


def _progress_path(backup_root: Path) -> Path:
    return backup_root / 'rename-progress.json'


def _read_progress(backup_root: Path) -> RenameProgress | None:
    path = _progress_path(backup_root)
    if not path.exists():
        return None
    return RenameProgress.model_validate_json(path.read_text())


def _write_progress(backup_root: Path, progress: RenameProgress) -> None:
    _atomic_write(_progress_path(backup_root), progress.model_dump_json())


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _log(backup_root: Path, value: dict[str, object]) -> None:
    timestamp = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    backup_root.mkdir(parents=True, exist_ok=True)
    with (backup_root / 'events.jsonl').open('a') as file:
        file.write(json.dumps({'timestamp': timestamp} | value) + '\n')
        file.flush()
        os.fsync(file.fileno())
