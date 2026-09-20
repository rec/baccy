import hashlib
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

from .catalog import Catalog
from .models import Candidate, FileResult

_CHUNK_SIZE = 1024 * 1024


def copy_candidate(
    candidate: Candidate,
    backup_root: Path,
    catalog: Catalog,
    stability_seconds: float,
    now_ns: int | None = None,
) -> FileResult:
    now = time.time_ns() if now_ns is None else now_ns
    source = candidate.path
    before = source.stat(follow_symlinks=False)
    if not _is_regular_file(before):
        return _result(candidate, 'deferred', 'source is no longer a regular file')
    if _matches_catalog(candidate, before, backup_root, catalog):
        return _result(candidate, 'unchanged')
    if (
        source.suffix != '.jsonl'
        and now - before.st_mtime_ns < stability_seconds * 1_000_000_000
    ):
        return _result(
            candidate, 'deferred', 'source is still within the stability interval'
        )

    destination = (
        backup_root / 'sources' / candidate.source.source.name / candidate.relative_path
    )
    _ensure_destination_parent(backup_root, destination.parent)
    temporary, digest = _snapshot(source, before, destination.parent)
    try:
        if temporary is None:
            return _result(candidate, 'deferred', digest)
        return _commit(
            candidate, before, temporary, digest, destination, backup_root, catalog
        )
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def preview_candidate(
    candidate: Candidate,
    backup_root: Path,
    catalog: Catalog,
    stability_seconds: float,
    now_ns: int | None = None,
) -> FileResult:
    now = time.time_ns() if now_ns is None else now_ns
    source = candidate.path
    before = source.stat(follow_symlinks=False)
    if not _is_regular_file(before):
        return _result(candidate, 'deferred', 'source is no longer a regular file')
    if _matches_catalog(candidate, before, backup_root, catalog):
        return _result(candidate, 'unchanged')
    if (
        source.suffix != '.jsonl'
        and now - before.st_mtime_ns < stability_seconds * 1_000_000_000
    ):
        return _result(
            candidate, 'deferred', 'source is still within the stability interval'
        )
    if source.suffix == '.jsonl' and not _has_complete_final_line(
        source, before.st_size
    ):
        return _result(candidate, 'deferred', 'JSONL source has a partial final line')
    return _result(candidate, 'would_copy')


def _snapshot(
    source: Path, before: os.stat_result, directory: Path
) -> tuple[Path | None, str]:
    descriptor, name = tempfile.mkstemp(prefix='.baccy-', suffix='.tmp', dir=directory)
    temporary = Path(name)
    try:
        if source.suffix == '.jsonl':
            return _snapshot_jsonl(source, before, descriptor, temporary)
        return _snapshot_regular(source, before, descriptor, temporary)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _snapshot_regular(
    source: Path, before: os.stat_result, destination_descriptor: int, temporary: Path
) -> tuple[Path | None, str]:
    source_descriptor = _open_source(source)
    try:
        with os.fdopen(source_descriptor, 'rb', closefd=False) as input_file:
            opened = os.fstat(source_descriptor)
            if not _same_file(before, opened):
                return None, 'source changed before copying'
            with os.fdopen(destination_descriptor, 'wb', closefd=False) as output_file:
                digest, _ = _write_stream(input_file, output_file, opened.st_size)
                output_file.flush()
                os.fsync(output_file.fileno())
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)
    after = source.stat(follow_symlinks=False)
    if not _same_file(before, after):
        return None, 'source changed while copying'
    os.utime(temporary, ns=(after.st_atime_ns, after.st_mtime_ns))
    return temporary, digest


def _snapshot_jsonl(
    source: Path, before: os.stat_result, destination_descriptor: int, temporary: Path
) -> tuple[Path | None, str]:
    source_descriptor = _open_source(source)
    try:
        with os.fdopen(source_descriptor, 'rb', closefd=False) as input_file:
            opened = os.fstat(source_descriptor)
            if not _same_file(before, opened):
                return None, 'source changed before copying'
            with os.fdopen(destination_descriptor, 'wb', closefd=False) as output_file:
                digest, last = _write_stream(input_file, output_file, opened.st_size)
                output_file.flush()
                os.fsync(output_file.fileno())
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)
    after = source.stat(follow_symlinks=False)
    if not _same_identity(before, after) or after.st_size < before.st_size:
        return None, 'JSONL source was replaced or truncated while copying'
    if before.st_size and last != b'\n':
        return None, 'JSONL source has a partial final line'
    if _sha256_prefix(source, before.st_size) != digest:
        return None, 'JSONL source changed while copying'
    os.utime(temporary, ns=(after.st_atime_ns, after.st_mtime_ns))
    return temporary, digest


def _open_source(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)


def _has_complete_final_line(path: Path, size: int) -> bool:
    if not size:
        return True
    descriptor = _open_source(path)
    try:
        with os.fdopen(descriptor, 'rb', closefd=False) as file:
            file.seek(-1, os.SEEK_END)
            return file.read(1) == b'\n'
    finally:
        os.close(descriptor)


def _write_stream(
    input_file: BinaryIO,
    output_file: BinaryIO,
    size: int,
) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    remaining = size
    last = b''
    while remaining:
        chunk = input_file.read(min(_CHUNK_SIZE, remaining))
        if not chunk:
            raise OSError('source ended while copying')
        output_file.write(chunk)
        digest.update(chunk)
        last = chunk[-1:]
        remaining -= len(chunk)
    return digest.hexdigest(), last


def _commit(
    candidate: Candidate,
    before: os.stat_result,
    temporary: Path,
    digest: str,
    destination: Path,
    backup_root: Path,
    catalog: Catalog,
) -> FileResult:
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise OSError(f'destination is not a regular file: {destination}')
        existing_digest = _sha256(destination)
        if existing_digest == digest:
            return _result(candidate, 'unchanged')
        _retain(destination, candidate, existing_digest, backup_root)
    os.replace(temporary, destination)
    catalog.append(
        {
            'source': candidate.source.source.name,
            'relative_path': candidate.relative_path.as_posix(),
            'size': before.st_size,
            'mtime_ns': before.st_mtime_ns,
            'sha256': digest,
            'destination': destination.relative_to(backup_root).as_posix(),
            'copied_at_ns': time.time_ns(),
            'result': 'copied',
        }
    )
    return _result(candidate, 'copied')


def _retain(
    destination: Path, candidate: Candidate, digest: str, backup_root: Path
) -> None:
    relative = candidate.relative_path
    version_directory = (
        backup_root
        / '.baccy'
        / 'versions'
        / candidate.source.source.name
        / relative.parent
    )
    _ensure_directory(backup_root, version_directory)
    version = version_directory / f'{relative.name}.{time.time_ns()}.{digest}'
    os.link(destination, version)


def _matches_catalog(
    candidate: Candidate, source: os.stat_result, backup_root: Path, catalog: Catalog
) -> bool:
    record = catalog.latest(candidate.source.source.name, candidate.relative_path)
    if record is None:
        return False
    if (
        record.get('size') != source.st_size
        or record.get('mtime_ns') != source.st_mtime_ns
    ):
        return False
    destination = (
        backup_root / 'sources' / candidate.source.source.name / candidate.relative_path
    )
    return destination.is_file() and destination.stat().st_size == source.st_size


def _ensure_destination_parent(backup_root: Path, parent: Path) -> None:
    _ensure_directory(backup_root, parent)


def _ensure_directory(backup_root: Path, directory: Path) -> None:
    backup_root.mkdir(parents=True, exist_ok=True)
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise OSError(f'backup root is not a directory: {backup_root}')
    relative = directory.relative_to(backup_root)
    current = backup_root
    for part in relative.parts:
        current /= part
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise OSError(f'destination parent is not a directory: {current}')
        else:
            current.mkdir()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(_CHUNK_SIZE), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_prefix(path: Path, size: int) -> str:
    descriptor = _open_source(path)
    try:
        with os.fdopen(descriptor, 'rb', closefd=False) as file:
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = file.read(min(_CHUNK_SIZE, remaining))
                if not chunk:
                    raise OSError('JSONL source ended while verifying')
                digest.update(chunk)
                remaining -= len(chunk)
            return digest.hexdigest()
    finally:
        os.close(descriptor)


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return _same_identity(first, second) and (
        first.st_size == second.st_size and first.st_mtime_ns == second.st_mtime_ns
    )


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _is_regular_file(value: os.stat_result) -> bool:
    return stat.S_ISREG(value.st_mode)


def _result(candidate: Candidate, status: str, detail: str | None = None) -> FileResult:
    return FileResult(
        source=candidate.source.source.name,
        relative_path=candidate.relative_path,
        status=status,
        detail=detail,
    )
