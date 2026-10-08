"""Print a read-only openLoop import proposal using names and file sizes."""

import re
import sys
from datetime import date
from pathlib import Path
from typing import Annotated

import tyro
from pydantic import BaseModel


class OpenLoopPlanCommand(BaseModel, frozen=True):
    """Inventory openLoop without reading audio or changing any files."""

    directory: Annotated[Path, tyro.conf.Positional]


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


def main(arguments: list[str] | None = None) -> int:
    command = tyro.cli(OpenLoopPlanCommand, args=arguments)
    try:
        proposals = plan_collection(command.directory)
    except (OSError, ValueError) as error:
        sys.exit(str(error))
    for proposal in proposals:
        print(proposal.model_dump_json())
    return 0


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
    )


def _date_from_name(name: str) -> date | None:
    if match := re.search(r'(?<!\d)(\d{4})[-.](\d{2})[-.](\d{2})(?!\d)', name):
        return date(*[int(p) for p in match.groups()])
    return None


if __name__ == '__main__':
    raise SystemExit(main())
