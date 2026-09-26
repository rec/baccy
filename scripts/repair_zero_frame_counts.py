import json
import sys
import tempfile
import wave
from pathlib import Path, PurePosixPath
from typing import Annotated

import tyro
from pydantic import BaseModel


class RepairZeroFrameCountsCommand(BaseModel, frozen=True):
    """Replace zero audio frame counts in completed recs sessions."""

    directory: Annotated[Path, tyro.conf.Positional]


def main(arguments: list[str] | None = None) -> int:
    command = tyro.cli(RepairZeroFrameCountsCommand, args=arguments)
    repaired = 0
    failures = 0
    for journal in sorted(command.directory.resolve().glob('**/session-record.jsonl')):
        try:
            changed = _repair_journal(journal)
        except (OSError, ValueError, wave.Error) as error:
            print(f'{journal}: {error}', file=sys.stderr)
            failures += 1
            continue
        if changed:
            repaired += changed
            print(f'{journal}: repaired {changed} frame counts')
    return 1 if failures else 0


def _repair_journal(journal: Path) -> int:
    lines = journal.read_text().splitlines(keepends=True)
    repaired = 0
    replacement: list[str] = []
    for line in lines:
        if not line.endswith('\n'):
            replacement.append(line)
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError('invalid recs record')
        if _is_zero_frame_audio_finish(record):
            path = _audio_path(journal.parent, record)
            frames = _frame_count(path)
            if frames > 0:
                record['frame_count'] = frames
                line = json.dumps(record) + '\n'
                repaired += 1
        replacement.append(line)
    if repaired:
        _replace(journal, ''.join(replacement))
    return repaired


def _is_zero_frame_audio_finish(record: dict[str, object]) -> bool:
    stream_id = record.get('stream_id')
    return (
        record.get('type') == 'file_finished'
        and record.get('frame_count') == 0
        and (
            record.get('media_type') == 'audio'
            or isinstance(stream_id, str)
            and stream_id.startswith('audio:')
        )
    )


def _audio_path(session: Path, record: dict[str, object]) -> Path:
    path = record.get('path')
    if not isinstance(path, str):
        raise ValueError('completed audio record has no path')
    value = PurePosixPath(path)
    if value.is_absolute() or '..' in value.parts:
        raise ValueError(f'completed audio path escapes session: {path}')
    audio = session.joinpath(*value.parts)
    if not audio.is_file():
        raise ValueError(f'completed audio file is missing: {path}')
    return audio


def _frame_count(audio: Path) -> int:
    match audio.suffix.casefold():
        case '.flac':
            return _flac_frame_count(audio)
        case '.wav':
            with wave.open(str(audio)) as file:
                return file.getnframes()
        case suffix:
            raise ValueError(f'unsupported audio format: {suffix}')


def _flac_frame_count(audio: Path) -> int:
    with audio.open('rb') as file:
        if file.read(4) != b'fLaC':
            raise ValueError('not a FLAC file')
        while True:
            header = file.read(4)
            if len(header) != 4:
                raise ValueError('FLAC metadata is truncated')
            final = bool(header[0] & 0x80)
            block_type = header[0] & 0x7F
            size = int.from_bytes(header[1:], 'big')
            block = file.read(size)
            if len(block) != size:
                raise ValueError('FLAC metadata is truncated')
            if block_type == 0:
                if len(block) != 34:
                    raise ValueError('FLAC STREAMINFO has an invalid length')
                return int.from_bytes(block[10:18], 'big') & ((1 << 36) - 1)
            if final:
                raise ValueError('FLAC file has no STREAMINFO block')


def _replace(journal: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        dir=journal.parent, mode='w', delete=False
    ) as file:
        file.write(content)
        replacement = Path(file.name)
    replacement.replace(journal)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
