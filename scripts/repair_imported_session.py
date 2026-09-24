import json
import shutil
import sys
from datetime import datetime
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
    include_unrecorded_audio: Annotated[
        bool,
        tyro.conf.arg(
            help='Add completed records for regular audio files absent from the '
            'journal.'
        ),
    ] = False
    discard_missing_audio: Annotated[
        bool,
        tyro.conf.arg(
            help='Omit completed records whose audio file cannot be resolved.'
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
    journal_records = _read_records(journal)
    evidence = session / 'evidence' / 'session-record-v3.jsonl'
    records = (
        _read_records(evidence)
        if not _has_audio_records(journal_records) and evidence.is_file()
        else journal_records
    )
    repaired, report = _repair_records(
        session,
        records,
        command.include_unrecorded_audio,
        command.discard_missing_audio,
    )
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


def _has_audio_records(records: list[dict[str, object]]) -> bool:
    return any(record.get('media_type') == 'audio' for record in records)


def _repair_records(
    session: Path,
    records: list[dict[str, object]],
    include_unrecorded_audio: bool,
    discard_missing_audio: bool,
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
            replacement, item = _resolve_path(session, path, start)
            report.append(item)
            source = _source_name(start, stream_id)
            if replacement is not None and source is not None:
                repaired.extend(
                    [
                        {**start, 'path': replacement, 'source': source},
                        {**record, 'path': replacement},
                    ]
                )
            elif replacement is None and discard_missing_audio:
                report[-1] = RepairRecord(path=path, status='discarded')
            elif replacement is not None:
                report[-1] = RepairRecord(
                    path=path,
                    status='deferred',
                    reason='audio source is missing from the evidence record',
                )
    if include_unrecorded_audio:
        repaired, additions = _add_unrecorded_audio(session, repaired)
        report.extend(additions)
    return repaired, report


def _source_name(record: dict[str, object], stream_id: str) -> str | None:
    source = record.get('source')
    if isinstance(source, str):
        return source
    prefix = 'audio:'
    if not stream_id.startswith(prefix):
        return None
    source, separator, track = stream_id.removeprefix(prefix).rpartition(':')
    if not separator or not source or not track:
        return None
    return source


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


def _resolve_path(
    session: Path, path: str, start: dict[str, object]
) -> tuple[str | None, RepairRecord]:
    recorded = (session / path).resolve()
    if recorded.is_file():
        return path, RepairRecord(path=path, status='unchanged', resolved_path=path)
    audio = session / 'audio'
    children = audio.iterdir() if audio.is_dir() else []
    candidates = [candidate for candidate in children if candidate.is_file()]
    basename = Path(path).name
    exact = [candidate for candidate in candidates if candidate.name == basename]
    if exact:
        matches = exact
    else:
        matches = []
    if not matches and (suffix := _recorded_suffix(basename)) is not None:
        matches = [candidate for candidate in candidates if candidate.name == suffix]
    if (
        not matches
        and (channels := start.get('source_channels'))
        and isinstance(channels, list)
    ):
        label = '-'.join(str(channel) for channel in channels)
        timestamp = Path(path).stem.rsplit(' + ', 1)[-1]
        matches = [
            candidate
            for candidate in candidates
            if candidate.stem == f'{label} + {timestamp}'
        ]
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


def _recorded_suffix(basename: str) -> str | None:
    parts = basename.split(' + ', 1)
    return parts[1] if len(parts) == 2 else None


def _add_unrecorded_audio(
    session: Path, records: list[dict[str, object]]
) -> tuple[list[dict[str, object]], list[RepairRecord]]:
    recorded = {
        record['path']
        for record in records
        if record.get('type') == 'file_finished' and isinstance(record.get('path'), str)
    }
    additions: list[RepairRecord] = []
    for audio in sorted((session / 'audio').glob('*')):
        if not audio.is_file():
            continue
        path = audio.relative_to(session).as_posix()
        if path in recorded:
            continue
        start, finish = _audio_records(audio, path)
        records.extend([start, finish])
        additions.append(RepairRecord(path=path, status='added', resolved_path=path))
    return records, additions


def _audio_records(
    audio: Path, path: str
) -> tuple[dict[str, object], dict[str, object]]:
    source, track, channels, timestamp = _audio_details(audio)
    stream_id = f'audio:{source}:{track}'
    start = {
        'type': 'file_started',
        'timestamp': timestamp,
        'stream_id': stream_id,
        'format': audio.suffix.removeprefix('.'),
        'path': path,
        'source': source,
        'media_type': 'audio',
        'frame_count': 0,
        'track_name': track,
        'source_channels': channels,
        'channels': len(channels),
        'sample_rate': 48000,
    }
    return start, {**start, 'type': 'file_finished'}


def _audio_details(audio: Path) -> tuple[str, str, list[int], str]:
    parts = audio.stem.rsplit(' + ', 2)
    if len(parts) == 3:
        source, track, stamp = parts
    elif len(parts) == 2:
        track, stamp = parts
        source = track
    else:
        raise ValueError(f'cannot infer audio metadata from {audio.name}')
    channels = [int(channel) for channel in track.split('-') if channel.isdigit()]
    if not channels:
        channels = [1, 2] if track == 'master' else [1]
    timestamp = datetime.strptime(stamp, '%Y%m%d-%H%M%S').isoformat() + 'Z'
    return source, track, channels, timestamp


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
