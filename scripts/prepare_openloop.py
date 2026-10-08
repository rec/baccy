"""Prepare converted openLoop WAVs as offline, baccy-compatible sessions."""

import json
import shutil
import struct
import sys
import tempfile
import wave
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import tyro
from pydantic import BaseModel
from reccy.paths import legal_url_path

from baccy.config import default_backup_root

from . import plan_openloop


class PrepareOpenLoopCommand(BaseModel, frozen=True):
    """Preview offline sessions; use --apply only after reviewing destinations."""

    collection: Annotated[Path, tyro.conf.Positional]
    destination: Annotated[Path, tyro.conf.Positional]
    decisions: Path = (
        Path(__file__).resolve().parents[1] / 'plan/openLoop-decisions.json'
    )
    apply: bool = False


class PreparedDisc(BaseModel, frozen=True):
    source: Path
    provenance: Path
    original: Path | None
    relative_path: Path
    disc_number: int | None
    frames: int
    sample_rate: int
    channels: int
    bit_depth: int
    size_bytes: int
    mtime_ns: int


class PreparedSession(BaseModel, frozen=True):
    recording_date: date
    destination: Path
    discs: list[PreparedDisc]


def main(arguments: list[str] | None = None) -> int:
    command = tyro.cli(PrepareOpenLoopCommand, args=arguments)
    try:
        sessions = prepare_sessions(
            command.collection, command.destination, command.decisions
        )
        for session in sessions:
            for disc in session.discs:
                print(f'{disc.source} -> {session.destination / disc.relative_path}')
        print(f'{len(sessions)} sessions; {sum(len(s.discs) for s in sessions)} WAVs')
        if command.apply:
            write_sessions(sessions)
            print('Prepared offline sessions. No import or upload was requested.')
        else:
            print('Preview only. No files changed. Rerun with --apply to prepare.')
    except (OSError, ValueError, wave.Error, EOFError) as error:
        sys.exit(str(error))
    except KeyboardInterrupt:
        sys.exit(
            'Interrupted; original audio is unchanged; partial staging is retained.'
        )
    return 0


def prepare_sessions(
    collection: Path, destination: Path, decisions: Path
) -> list[PreparedSession]:
    """Read headers and check all selected destinations before writing anything."""
    collection = collection.resolve()
    destination = destination.resolve()
    if destination.is_relative_to(collection) or collection.is_relative_to(destination):
        raise ValueError('preparation destination must be separate from the collection')
    if destination.is_relative_to(default_backup_root().resolve()):
        raise ValueError('prepare offline, outside the live baccy backup directory')
    for proposal in plan_openloop.plan_collection(collection, decisions):
        if proposal.stage == 'converted-wav' and proposal.recording_date is None:
            raise ValueError(f'converted WAV has an unresolved date: {proposal.path}')
    sessions: list[PreparedSession] = []
    remote_paths: set[str] = set()
    for session in plan_openloop.plan_sessions(collection, decisions):
        target = (
            destination / 'openLoop' / f'{session.recording_date:%Y/%m/%d}' / '00-00-00'
        )
        discs: list[PreparedDisc] = []
        for proposal in session.discs:
            if proposal.stage != 'converted-wav':
                continue
            disc = _inspect_wav(collection, proposal)
            if disc.frames / disc.sample_rate < 10:
                print(
                    f'{disc.source}: skipped (shorter than ten seconds)',
                    file=sys.stderr,
                )
                continue
            mp3 = Path(
                disc.relative_path.name.rsplit(' + ', maxsplit=1)[-1]
            ).with_suffix('.mp3')
            remote = legal_url_path(Path('openLoop') / mp3).as_posix()
            if remote in remote_paths:
                raise ValueError(f'listening-file destination collision: {remote}')
            remote_paths.add(remote)
            if any(d.relative_path == disc.relative_path for d in discs):
                raise ValueError(f'duplicate disc destination: {disc.relative_path}')
            discs.append(disc)
        if not discs:
            continue
        if target.exists() or target.is_symlink():
            raise FileExistsError(f'prepared session already exists: {target}')
        sessions.append(
            PreparedSession(
                recording_date=session.recording_date, destination=target, discs=discs
            )
        )
    if not sessions:
        raise ValueError('no eligible converted WAVs to prepare')
    return sessions


def write_sessions(sessions: list[PreparedSession]) -> None:
    """Copy into private staging and publish each session only when complete."""
    for session in sessions:
        if session.destination.exists() or session.destination.is_symlink():
            raise FileExistsError(
                f'prepared session already exists: {session.destination}'
            )
        for disc in session.discs:
            _verify_source(disc)
    existing = sessions[0].destination.parent
    while not existing.exists():
        existing = existing.parent
    needed = sum(d.size_bytes for s in sessions for d in s.discs)
    needed += sum(len(_journal(s).encode()) for s in sessions)
    if shutil.disk_usage(existing).free < needed:
        raise OSError(f'insufficient free space: need {needed} bytes')
    for session in sessions:
        session.destination.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(
            tempfile.mkdtemp(
                prefix='.openLoop-preparing-', dir=session.destination.parent
            )
        )
        (staged / 'audio').mkdir()
        for disc in session.discs:
            target = staged / disc.relative_path
            _verify_source(disc)
            _log('writing', source=str(disc.source), destination=str(target))
            shutil.copy2(disc.source, target)
            _verify_source(disc)
            if target.stat().st_size != disc.size_bytes:
                raise OSError(f'copied file has incorrect size: {target}')
            _log('copied', destination=str(target))
        (staged / '.session-record.pending').write_text(_journal(session))
        if session.destination.exists() or session.destination.is_symlink():
            raise FileExistsError(
                f'prepared session already exists: {session.destination}'
            )
        staged.rename(session.destination)
        (session.destination / '.session-record.pending').rename(
            session.destination / 'session-record.jsonl'
        )
        _log('session_complete', destination=str(session.destination))


def _inspect_wav(
    collection: Path, proposal: plan_openloop.RecordingProposal
) -> PreparedDisc:
    path = collection / proposal.path
    before = path.stat()
    with path.open('rb') as file:
        header = file.read(12)
        if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WAVE':
            raise ValueError(f'not a RIFF WAV: {path}')
        riff_end = 8 + struct.unpack('<I', header[4:8])[0]
        file.seek(0)
        with wave.open(file) as audio:
            frames = audio.getnframes()
            rate = audio.getframerate()
            channels = audio.getnchannels()
            width = audio.getsampwidth()
            offset = file.tell()
            file.seek(offset - 4)
            data_size = struct.unpack('<I', file.read(4))[0]
    if riff_end > before.st_size or offset + data_size > min(riff_end, before.st_size):
        raise ValueError(f'truncated WAV: {path}')
    if data_size != frames * channels * width:
        raise ValueError(f'incomplete PCM frame: {path}')
    if before.st_size != riff_end:
        raise ValueError(f'bytes outside declared RIFF boundary: {path}')
    if frames == 10_000_000:
        raise ValueError(
            f'exactly 10,000,000 frames; investigate before import: {path}'
        )
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f'WAV changed during inspection: {path}')
    return PreparedDisc(
        source=path,
        provenance=proposal.path,
        original=proposal.original_path,
        relative_path=Path('audio') / path.name,
        disc_number=proposal.disc_number,
        frames=frames,
        sample_rate=rate,
        channels=channels,
        bit_depth=width * 8,
        size_bytes=before.st_size,
        mtime_ns=before.st_mtime_ns,
    )


def _verify_source(disc: PreparedDisc) -> None:
    metadata = disc.source.stat()
    if (metadata.st_size, metadata.st_mtime_ns) != (disc.size_bytes, disc.mtime_ns):
        raise ValueError(f'source changed since preview: {disc.source}')


def _journal(session: PreparedSession) -> str:
    placeholder = f'{session.recording_date}T00:00:00Z'
    records: list[dict[str, object]] = [
        {
            'type': 'header',
            'version': 4,
            'project_name': 'openLoop',
            'started_at': placeholder,
            'metadata': {
                'imported': True,
                'timing_source': 'date_only_midnight_placeholder',
                'actual_start_time_known': False,
                'disc_start_times_known': False,
                'actual_end_time_known': False,
                'duration_source': 'sum_of_disc_audio_not_wall_clock',
                'gaps_between_discs_known': False,
                'grouping_rule': 'show_date_with_ordered_cd_discs',
                'sources': [
                    {
                        'path': d.provenance.as_posix(),
                        'original_path': d.original.as_posix() if d.original else None,
                        'disc_number': d.disc_number,
                        'prepared_path': d.relative_path.as_posix(),
                    }
                    for d in session.discs
                ],
            },
        }
    ]
    for disc in session.discs:
        track = (
            f'disc {disc.disc_number}'
            if disc.disc_number is not None
            else disc.relative_path.stem
        )
        common: dict[str, object] = {
            'timestamp': placeholder,
            'stream_id': f'audio:CD:{quote(track, safe="")}',
            'path': disc.relative_path.as_posix(),
            'media_type': 'audio',
            'source': 'CD',
            'track_name': track,
            'format': 'wav',
            'source_channels': list(range(1, disc.channels + 1)),
            'channels': disc.channels,
            'sample_rate': disc.sample_rate,
            'bit_depth': disc.bit_depth,
        }
        records.extend(
            [
                {**common, 'type': 'file_started', 'frame_count': 0},
                {
                    **common,
                    'type': 'file_finished',
                    'frame_count': disc.frames,
                    'quantity_count': disc.frames,
                },
            ]
        )
    records.append(
        {
            'type': 'footer',
            'ended_at': placeholder,
            'duration_seconds': sum(d.frames / d.sample_rate for d in session.discs),
        }
    )
    return ''.join(json.dumps(r) + '\n' for r in records)


def _log(status: str, **values: str) -> None:
    print(
        json.dumps(
            {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'status': status,
                **values,
            }
        ),
        file=sys.stderr,
        flush=True,
    )


if __name__ == '__main__':
    raise SystemExit(main())
