import json
import re
from datetime import UTC, datetime
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from .models import S3Destination, Settings
from .s3 import s3_client
from .upload import planned_source_uploads


class RenameFile(BaseModel, frozen=True):
    session: Path
    source: Path
    replacement: Path
    destinations: list[S3Destination]


def renamed_files(
    settings: Settings, pattern: str, replacement: str, regular_expression: bool
) -> list[RenameFile]:
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
        value = values.get(source)
        if value is None:
            values[source] = RenameFile(
                session=plan.session,
                source=source,
                replacement=source.with_name(name),
                destinations=[plan.destination],
            )
        else:
            values[source] = value.model_copy(
                update={'destinations': [*value.destinations, plan.destination]}
            )
    return sorted(values.values(), key=lambda value: value.source)


def rename_files(
    settings: Settings, files: list[RenameFile], pattern: str, replacement: str
) -> bool:
    root = settings.backup_root / 'audio'
    _validate_files(root, files)
    _log({'pattern': pattern, 'replacement': replacement})
    sessions: dict[Path, list[RenameFile]] = {}
    for value in files:
        for destination in value.destinations:
            old_key = value.source.as_posix()
            new_key = value.replacement.as_posix()
            _log({'s3': f's3:{destination.bucket}/{old_key}'})
            try:
                client = s3_client(destination)
                client.copy_object(
                    Bucket=destination.bucket,
                    Key=new_key,
                    CopySource={'Bucket': destination.bucket, 'Key': old_key},
                )
                client.delete_object(Bucket=destination.bucket, Key=old_key)
            except (BotoCoreError, ClientError) as error:
                _log({'error': str(error)})
                return False
            _log({'ok': True})
        (root / value.source).rename(root / value.replacement)
        sessions.setdefault(value.session, []).append(value)
    for session, values in sessions.items():
        _rewrite_session(root / session / 'session-record.jsonl', values)
        _log({'message': f'Session {session} rename complete'})
    return True


def _validate_files(root: Path, files: list[RenameFile]) -> None:
    sources = {value.source for value in files}
    replacements = {value.replacement for value in files}
    if len(replacements) != len(files):
        raise ValueError('rename replacements are not unique')
    for value in files:
        if not (root / value.source).is_file():
            raise ValueError(f'backup audio file does not exist: {value.source}')
        if value.replacement in sources or (root / value.replacement).exists():
            raise ValueError(f'rename target already exists: {value.replacement}')


def _rewrite_session(path: Path, files: list[RenameFile]) -> None:
    replacements = {
        value.source.relative_to(value.session).as_posix(): (
            value.replacement.relative_to(value.session).as_posix()
        )
        for value in files
    }
    values: list[str] = []
    for line in path.read_text().splitlines():
        value = json.loads(line)
        if isinstance(value, dict) and isinstance(name := value.get('path'), str):
            if name in replacements:
                value['path'] = replacements[name]
        values.append(json.dumps(value, separators=(',', ':')))
    path.write_text('\n'.join(values) + '\n')


def _log(value: dict[str, object]) -> None:
    timestamp = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(json.dumps({'timestamp': timestamp} | value))
