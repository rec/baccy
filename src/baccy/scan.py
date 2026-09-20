from pathlib import Path, PurePosixPath

from .models import Candidate, ResolvedSource


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
    return sorted(
        candidates.values(), key=lambda c: (c.priority, c.relative_path.as_posix())
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
