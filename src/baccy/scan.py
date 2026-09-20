from __future__ import annotations

from pathlib import Path, PurePosixPath

from .models import Candidate, ResolvedSource


def scan(source: ResolvedSource) -> list[Candidate]:
    candidates = [
        Candidate(
            source=source,
            path=path,
            relative_path=path.relative_to(source.root),
            priority=_priority(path),
        )
        for path in _files(source.root)
        if _selected(path.relative_to(source.root), source)
    ]
    return sorted(candidates, key=lambda c: (c.priority, c.relative_path.as_posix()))


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
