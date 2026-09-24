import json
import shutil
import sys
from pathlib import Path
from typing import Annotated

import tyro
from pydantic import BaseModel


class RepairCommand(BaseModel, frozen=True):
    """Repair imported recs metadata after reviewing the default report."""

    session: Annotated[Path, tyro.conf.Positional]
    apply: Annotated[
        bool,
        tyro.conf.arg(
            help='Replace the import-stub journal when every audio file resolves.'
        ),
    ] = False


class RepairRecord(BaseModel, frozen=True):
    path: str
    status: str
    resolved_path: str | None = None
    reason: str | None = None


def main(arguments: list[str] | None = None) -> int:
    command = tyro.cli(RepairCommand, args=arguments)
    session = command.session.resolve()
    journal = session / 'session-record.jsonl'
    evidence = session / 'evidence' / 'session-record-v3.jsonl'
    _read_records(journal)
    records = _read_records(evidence)
    repaired, report = _repair_records(session, records)
    result = {
        'proposed_journal': ''.join(json.dumps(record) + '\n' for record in repaired),
        'report': [record.model_dump(exclude_none=True) for record in report],
    }
    print(json.dumps(result, indent=2))
    if not command.apply:
        return 0
    if any(record.status == 'deferred' for record in report):
        print('refusing to apply unresolved repair', file=sys.stderr)
        return 1
    _replace_journal(journal, repaired)
    return 0


def _read_records(journal: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with journal.open() as source:
        for line in source:
            if not line.endswith('\n'):
                break
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f'invalid recs record in {journal}')
            records.append(record)
    return records


def _repair_records(
    session: Path, records: list[dict[str, object]]
) -> tuple[list[dict[str, object]], list[RepairRecord]]:
    starts: dict[tuple[str, str], dict[str, object]] = {}
    repaired: list[dict[str, object]] = []
    report: list[RepairRecord] = []
    for record in records:
        record_type = record.get('type')
        if record_type not in {'file_started', 'file_finished', 'file_discarded'}:
            continue
        stream_id, path = _validate_lifecycle_path(session, record)
        identity = stream_id, path
        if record_type == 'file_started':
            if record.get('media_type') == 'audio':
                starts[identity] = record
        elif (
            record_type == 'file_finished'
            and (start := starts.get(identity)) is not None
        ):
            replacement, item = _resolve_path(session, path)
            report.append(item)
            if replacement is not None:
                repaired.extend(
                    [
                        {**start, 'path': replacement},
                        {**record, 'path': replacement},
                    ]
                )
    return repaired, report


def _validate_lifecycle_path(
    session: Path, record: dict[str, object]
) -> tuple[str, str]:
    stream_id = record.get('stream_id')
    path = record.get('path')
    if not isinstance(stream_id, str) or not isinstance(path, str):
        raise ValueError('invalid file lifecycle record')
    resolved = (session / path).resolve()
    if not resolved.is_relative_to(session):
        raise ValueError(f'file path escapes session directory: {path}')
    return stream_id, path


def _resolve_path(session: Path, path: str) -> tuple[str | None, RepairRecord]:
    recorded = (session / path).resolve()
    if recorded.is_file():
        return path, RepairRecord(path=path, status='unchanged', resolved_path=path)
    audio = session / 'audio'
    children = audio.iterdir() if audio.is_dir() else []
    matches = sorted(
        candidate
        for candidate in children
        if candidate.is_file() and candidate.name == Path(path).name
    )
    if len(matches) == 1:
        resolved_path = matches[0].relative_to(session).as_posix()
        return resolved_path, RepairRecord(
            path=path, status='repaired', resolved_path=resolved_path
        )
    reason = (
        'no regular file in audio has this basename'
        if not matches
        else 'multiple regular files in audio have this basename'
    )
    return None, RepairRecord(path=path, status='deferred', reason=reason)


def _replace_journal(journal: Path, records: list[dict[str, object]]) -> None:
    backup = journal.with_name('session-record.import-stub.jsonl')
    if backup.exists():
        raise FileExistsError(f'refusing to overwrite journal backup: {backup}')
    replacement = journal.with_name('session-record.repaired.jsonl')
    if replacement.exists():
        raise FileExistsError(
            f'refusing to overwrite replacement journal: {replacement}'
        )
    replacement.write_text(''.join(json.dumps(record) + '\n' for record in records))
    shutil.copy2(journal, backup)
    replacement.replace(journal)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
