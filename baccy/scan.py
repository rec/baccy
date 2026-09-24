import json
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from .models import Candidate, ResolvedSource
from .recs import files_being_written


def scan(source: ResolvedSource) -> list[Candidate]:
    candidates: dict[Path, Candidate] = {}
    for selection in source.selections:
        for path in _files(source.root / selection.relative_root):
            relative_path = path.relative_to(source.root)
            if selection.extensions is not None and (
                path.suffix.casefold() not in selection.extensions
            ):
                continue
            if not _selected(relative_path, source):
                continue
            candidates[relative_path] = Candidate(
                source=source,
                path=path,
                relative_path=relative_path,
                priority=_priority(path),
            )
    active = _active_recs_audio(candidates.values())
    return sorted(
        [
            candidate.model_copy(update={'active': candidate.path.resolve() in active})
            for candidate in candidates.values()
        ],
        key=lambda c: (c.priority, c.relative_path.as_posix()),
    )


def _files(directory: Path) -> list[Path]:
    paths: list[Path] = []
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name)
    except FileNotFoundError, PermissionError:
        return paths
    for path in children:
        if path.is_symlink():
            continue
        if path.is_dir():
            paths.extend(_files(path))
        elif path.is_file():
            paths.append(path)
    return paths


def _selected(relative_path: Path, source: ResolvedSource) -> bool:
    value = PurePosixPath(relative_path)
    includes = source.source.include
    excludes = source.source.exclude
    included = any(value.match(p) for p in includes)
    excluded = any(value.match(p) for p in excludes)
    return included and not excluded


def _priority(path: Path) -> int:
    if path.suffix == '.toml':
        return 0
    if path.suffix == '.jsonl':
        return 1
    return 2


def _active_recs_audio(candidates: Iterable[Candidate]) -> set[Path]:
    values = list(candidates)
    active: set[Path] = set()
    for candidate in values:
        if candidate.path.name != 'session-record.jsonl':
            continue
        try:
            active.update(files_being_written(candidate.path.parent))
        except OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError:
            active.update(
                value.path.resolve()
                for value in values
                if value.path.is_relative_to(candidate.path.parent)
                and value.path.suffix.casefold() in {'.wav', '.flac'}
            )
    return {path for path in active if path.suffix.casefold() in {'.wav', '.flac'}}
