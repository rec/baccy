"""Plan multi-disc show sessions and review unresolved recording dates."""

import re
import sys
import wave
from datetime import date
from pathlib import Path
from typing import Annotated

import tyro
from pydantic import BaseModel


class OpenLoopPlanCommand(BaseModel, frozen=True):
    """Review unresolved dates without changing collection files."""

    directory: Annotated[Path, tyro.conf.Positional]
    interactive: bool = False


class RecordingProposal(BaseModel, frozen=True):
    path: Path
    original_path: Path | None
    size_bytes: int
    title: str
    recording_date: date | None
    filename_date: date | None
    directory_date: date | None
    date_evidence: str
    stage: str
    disc_number: int | None


class SessionProposal(BaseModel, frozen=True):
    recording_date: date
    discs: list[RecordingProposal]


def main(arguments: list[str] | None = None) -> int:
    command = tyro.cli(OpenLoopPlanCommand, args=arguments)
    try:
        review_collection(command.directory, command.interactive)
    except (OSError, ValueError) as error:
        sys.exit(str(error))
    except EOFError, KeyboardInterrupt:
        sys.exit('Review cancelled; no collection files were changed.')
    return 0


def review_collection(directory: Path, interactive: bool = False) -> None:
    """Ask only date questions; report technical concerns separately on stderr."""
    decisions: list[date] = []
    for proposal in plan_collection(directory):
        path = directory / proposal.path
        if path.suffix.lower() == '.wav':
            try:
                with wave.open(str(path)) as audio:
                    frames = audio.getnframes()
                    duration = frames / audio.getframerate()
            except (wave.Error, EOFError) as error:
                print(f'{path.resolve()}: WAV header error: {error}', file=sys.stderr)
            else:
                if duration < 10:
                    continue
                if frames == 10_000_000:
                    print(
                        f'{path.resolve()}: exactly 10,000,000 declared frames; '
                        'needs technical investigation, not a memory-based decision.',
                        file=sys.stderr,
                    )
        if proposal.recording_date is not None:
            continue
        print(path.resolve())
        if proposal.filename_date and proposal.directory_date:
            print('  Filename and directory disagree about the date.')
        else:
            print('  Recording date is incomplete or unknown.')
        if not interactive:
            print('  What was the recording date?\n')
            continue
        while True:
            answer = input('Recording date (YYYY-MM-DD; Enter to defer; q to finish): ')
            if answer.strip().lower() == 'q':
                _print_decisions(decisions)
                return
            if not answer.strip():
                break
            try:
                decisions.append(date.fromisoformat(answer.strip()))
            except ValueError:
                print('Enter a valid recording date in YYYY-MM-DD format.')
                continue
            break
    if interactive:
        _print_decisions(decisions)


def plan_sessions(directory: Path) -> list[SessionProposal]:
    """Group dated discs into shows, without inventing inter-disc timings."""
    groups: dict[date, list[RecordingProposal]] = {}
    for proposal in plan_collection(directory):
        if proposal.recording_date is not None:
            groups.setdefault(proposal.recording_date, []).append(proposal)
    sessions: list[SessionProposal] = []
    for recording_date, discs in sorted(groups.items()):
        discs.sort(
            key=lambda p: (
                p.disc_number is None,
                p.disc_number or 0,
                p.path.as_posix(),
            )
        )
        sessions.append(SessionProposal(recording_date=recording_date, discs=discs))
    return sessions


def plan_collection(directory: Path) -> list[RecordingProposal]:
    """Associate converted WAVs with originals and propose dates, not timestamps."""
    source = directory / 'source'
    target = directory / 'target'
    if not source.is_dir() or not target.is_dir():
        raise ValueError('collection must contain source/ and target/ directories')
    originals = {
        p.relative_to(source): p
        for p in source.rglob('*')
        if p.is_file() and not p.is_symlink() and '.DS_Store' != p.name
    }
    converted: set[Path] = set()
    proposals: list[RecordingProposal] = []
    for path in sorted(target.rglob('*.wav')):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(target)
        original = relative.with_suffix('.Sd2f')
        if original not in originals:
            raise ValueError(f'target WAV has no matching source: {relative}')
        converted.add(original)
        proposals.append(
            _proposal(directory, path, originals[original], 'converted-wav')
        )
    for relative, path in sorted(originals.items()):
        if relative in converted or path.suffix.lower() == '.sh':
            continue
        stage = 'unconverted-sd2f' if path.suffix.lower() == '.sd2f' else 'review'
        proposals.append(_proposal(directory, path, None, stage))
    return proposals


def _print_decisions(decisions: list[date]) -> None:
    if decisions:
        print('\nDates supplied, in answer order (not saved):')
        for number, value in enumerate(decisions, 1):
            print(f'{number}. {value}')


def _proposal(
    directory: Path, path: Path, original: Path | None, stage: str
) -> RecordingProposal:
    relative = path.relative_to(directory)
    filename_date = _date_from_name(path.name)
    parts = relative.parts
    directory_date = None
    if len(parts) >= 5 and all(p.isdecimal() for p in parts[1:4]):
        directory_date = date(*[int(p) for p in parts[1:4]])
    recording_date = filename_date or directory_date
    evidence = 'filename' if filename_date is not None else 'directory'
    if filename_date and directory_date and filename_date != directory_date:
        recording_date = None
        evidence = 'conflicting dates: user decision required'
    elif recording_date is None:
        evidence = 'unknown: user decision required'
    title = path.stem
    if relative.as_posix() in {
        'source/unknown/xx-05-02-1.Sd2f',
        'source/unknown/xx-05-02-2.Sd2f',
    }:
        recording_date = date(2003, 5, 3)
        evidence = 'user-approved correction; original path retained'
        title = path.stem.replace('xx-05-02-', '2003-05-03-')
    return RecordingProposal(
        path=relative,
        original_path=original.relative_to(directory) if original else None,
        size_bytes=path.stat().st_size,
        title=title,
        recording_date=recording_date,
        filename_date=filename_date,
        directory_date=directory_date,
        date_evidence=evidence,
        stage=stage,
        disc_number=(
            int(match.group(1))
            if (
                match := re.fullmatch(r'(?:\d{4}-\d{2}-\d{2}|\d{6}|\d{8})-(\d+)', title)
            )
            else None
        ),
    )


def _date_from_name(name: str) -> date | None:
    if match := re.search(r'(?<!\d)(\d{4})[-.](\d{2})[-.](\d{2})(?!\d)', name):
        return date(*[int(p) for p in match.groups()])
    return None


if __name__ == '__main__':
    raise SystemExit(main())
