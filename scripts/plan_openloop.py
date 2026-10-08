"""Plan multi-disc show sessions and review unresolved recording dates."""

import re
import sys
import tempfile
import wave
from datetime import date
from pathlib import Path
from typing import Annotated

import tyro
from pydantic import BaseModel

_DECISIONS_PATH = Path(__file__).resolve().parents[1] / 'plan/openLoop-decisions.json'


class OpenLoopPlanCommand(BaseModel, frozen=True):
    """Review unresolved dates without changing collection files."""

    directory: Annotated[Path, tyro.conf.Positional]
    interactive: bool = False
    decisions: Path = _DECISIONS_PATH
    sessions: bool = False


class ReviewDecisions(BaseModel, frozen=True):
    dates: dict[str, date | None]


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
        if command.interactive or not command.sessions:
            review_collection(command.directory, command.interactive, command.decisions)
        if command.sessions:
            for session in plan_sessions(command.directory, command.decisions):
                print(f'{session.recording_date} ({len(session.discs)} discs/files)')
                for disc in session.discs:
                    print(f'  {(command.directory / disc.path).resolve()}')
    except (OSError, ValueError) as error:
        sys.exit(str(error))
    except EOFError, KeyboardInterrupt:
        sys.exit('Review cancelled; completed answers were saved; audio was unchanged.')
    return 0


def review_collection(
    directory: Path, interactive: bool = False, decisions_path: Path = _DECISIONS_PATH
) -> None:
    """Ask only date questions; report technical concerns separately on stderr."""
    saved = _read_decisions(decisions_path)
    decisions: list[str] = []
    for proposal in plan_collection(directory, decisions_path):
        if proposal.stage == 'deferred':
            continue
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
            try:
                value = date.fromisoformat(answer.strip()) if answer.strip() else None
            except ValueError:
                print('Enter a valid recording date in YYYY-MM-DD format.')
                continue
            saved = ReviewDecisions(
                dates={**saved.dates, proposal.path.as_posix(): value}
            )
            _save_decisions(decisions_path, saved)
            decisions.append(str(value) if value else 'Deferred')
            break
    if interactive:
        _print_decisions(decisions)


def plan_sessions(
    directory: Path, decisions_path: Path = _DECISIONS_PATH
) -> list[SessionProposal]:
    """Group dated discs into shows, without inventing inter-disc timings."""
    groups: dict[date, list[RecordingProposal]] = {}
    for proposal in plan_collection(directory, decisions_path):
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


def plan_collection(
    directory: Path, decisions_path: Path = _DECISIONS_PATH
) -> list[RecordingProposal]:
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
    decisions = _read_decisions(decisions_path)
    for path in sorted(target.rglob('*.wav')):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(target)
        original = relative.with_suffix('.Sd2f')
        if original not in originals:
            raise ValueError(f'target WAV has no matching source: {relative}')
        converted.add(original)
        proposals.append(
            _proposal(directory, path, originals[original], 'converted-wav', decisions)
        )
    for relative, path in sorted(originals.items()):
        if relative in converted or path.suffix.lower() == '.sh':
            continue
        stage = 'unconverted-sd2f' if path.suffix.lower() == '.sd2f' else 'review'
        proposals.append(_proposal(directory, path, None, stage, decisions))
    return proposals


def _print_decisions(decisions: list[str]) -> None:
    if decisions:
        print('\nDecisions saved, in answer order:')
        for number, value in enumerate(decisions, 1):
            print(f'{number}. {value}')


def _read_decisions(path: Path) -> ReviewDecisions:
    if not path.exists():
        return ReviewDecisions(dates={})
    return ReviewDecisions.model_validate_json(path.read_text())


def _save_decisions(path: Path, decisions: ReviewDecisions) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as file:
        file.write(decisions.model_dump_json(indent=2) + '\n')
        temporary = Path(file.name)
    temporary.replace(path)


def _proposal(
    directory: Path,
    path: Path,
    original: Path | None,
    stage: str,
    decisions: ReviewDecisions,
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
    if relative.as_posix() in decisions.dates:
        recording_date = decisions.dates[relative.as_posix()]
        evidence = 'saved user decision'
        if recording_date is None:
            stage = 'deferred'
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
