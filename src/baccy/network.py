import base64
import logging
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel

from .catalog import Catalog
from .copy import commit_snapshot, ensure_destination_parent, sha256
from .models import Candidate, FileResult, NetworkSource, ResolvedSource

_ARP_NODE = re.compile(
    r'\((?P<host>[^)]+)\) at (?P<mac>(?:[0-9a-f]{1,2}:){5}[0-9a-f]{1,2}) ',
    re.IGNORECASE,
)
_SSH_OPTIONS = [
    '-o',
    'BatchMode=yes',
    '-o',
    'ConnectTimeout=1',
    '-o',
    'StrictHostKeyChecking=no',
    '-o',
    'UserKnownHostsFile=/dev/null',
]
_LIST_RECS_FILES = (
    'cd "$HOME/recs" || exit\n'
    "find . -type f -exec sh -c '\n"
    'for path do\n'
    '    mtime=$(stat -f %m "$path" 2>/dev/null || stat -c %Y "$path") || exit\n'
    '    size=$(wc -c < "$path") || exit\n'
    '    printf "%s\\0%s\\0%s\\0" "$path" "$mtime" "$size"\n'
    'done\n'
    "' sh {} +"
)
NETWORK_SCAN_SECONDS = 10.0
_LOGGER = logging.getLogger(__name__)


class NetworkMachine(BaseModel, frozen=True):
    mac: str
    host: str

    @property
    def name(self) -> str:
        return f'network-{self.mac.replace(":", "").casefold()}'


class NetworkRecsSource(NetworkMachine):
    pass


class RemoteFile(BaseModel, frozen=True):
    relative_path: Path
    size: int
    mtime_ns: int


class NetworkDiscovery:
    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        verbose: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.run = run
        self.verbose = verbose
        self.sleep = sleep
        self.seen: set[str] = set()
        self.machines: dict[str, NetworkMachine] = {}
        self.new_machines: list[NetworkMachine] = []
        self.sources: dict[str, NetworkRecsSource] = {}
        self.last_scan: float | None = None

    def discover(self) -> list[NetworkRecsSource]:
        now = time.monotonic()
        if self.last_scan is not None and now - self.last_scan < NETWORK_SCAN_SECONDS:
            return list(self.sources.values())
        self.last_scan = now
        self.new_machines = []
        active: list[NetworkRecsSource] = []
        pending: list[tuple[str, str]] = []
        for mac, host in _network_nodes(self.run):
            if (source := self.sources.get(mac)) is not None:
                active.append(source.model_copy(update={'host': host}))
                self.sources[mac] = active[-1]
                self.machines[mac] = NetworkMachine(mac=mac, host=host)
                continue
            if mac in self.machines:
                self.machines[mac] = NetworkMachine(mac=mac, host=host)
                pending.append((mac, host))
                continue
            if mac in self.seen:
                continue
            self.seen.add(mac)
            self._log('network host %s (%s) discovered', host, mac)
            pending.append((mac, host))
        if pending:
            with ThreadPoolExecutor(max_workers=len(pending)) as executor:
                probes = [
                    (mac, host, executor.submit(self._probe_recs, host, mac))
                    for mac, host in pending
                ]
                for mac, host, probe in probes:
                    result = probe.result()
                    if result.returncode != 255 and not _is_authentication_failure(
                        result
                    ):
                        machine = NetworkMachine(mac=mac, host=host)
                        self.machines[mac] = machine
                        self.new_machines.append(machine)
                        self._log('network SSH accepted for %s (%s)', host, mac)
                    if result.returncode == 0:
                        source = NetworkRecsSource(mac=mac, host=host)
                        self.sources[mac] = source
                        active.append(source)
                        self._log('network host %s (%s) has recs', host, mac)
                    elif _is_authentication_failure(result):
                        detail = result.stderr.decode(errors='replace').strip()
                        self._log(
                            'network SSH authentication rejected for %s (%s): %s',
                            host,
                            mac,
                            detail,
                        )
                    elif result.returncode == 255:
                        detail = result.stderr.decode(errors='replace').strip()
                        self._log(
                            'network SSH failed for %s (%s): %s', host, mac, detail
                        )
                    else:
                        self._log(
                            'network host %s (%s) has no recs directory', host, mac
                        )
        return active

    def _probe_recs(self, host: str, mac: str) -> subprocess.CompletedProcess[bytes]:
        result = _ssh(self.run, host, 'test -d "$HOME/recs"')
        for delay in (2.0, 4.0):
            if not _is_connection_failure(result):
                break
            self._log(
                'network SSH unavailable for %s (%s); retrying in %s seconds',
                host,
                mac,
                int(delay),
            )
            self.sleep(delay)
            result = _ssh(self.run, host, 'test -d "$HOME/recs"')
        return result

    def _log(self, message: str, *values: object) -> None:
        if self.verbose:
            _LOGGER.info(message, *values)


def backup_network_source(
    source: NetworkRecsSource,
    backup_root: Path,
    catalog: Catalog,
    dry_run: bool,
    projects: set[str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> list[FileResult]:
    try:
        files = _recs_files(run, source.host)
    except OSError as error:
        if not dry_run:
            catalog.append(
                {
                    'source': source.name,
                    'relative_path': '.',
                    'result': 'failed',
                    'detail': str(error),
                }
            )
        return [FileResult(source=source.name, status='failed', detail=str(error))]
    results: list[FileResult] = []
    for file in files:
        try:
            result = _backup_remote_file(
                source, file, backup_root, catalog, dry_run, projects or set(), run
            )
        except OSError as error:
            result = FileResult(
                source=source.name,
                relative_path=file.relative_path,
                status='failed',
                detail=str(error),
            )
            if not dry_run:
                catalog.append(
                    {
                        'source': source.name,
                        'relative_path': file.relative_path.as_posix(),
                        'result': 'failed',
                        'detail': str(error),
                    }
                )
        results.append(result)
    return results


def _network_nodes(
    run: Callable[..., subprocess.CompletedProcess[bytes]],
) -> list[tuple[str, str]]:
    result = run(['arp', '-an'], capture_output=True, check=False)
    if result.returncode != 0:
        return []
    nodes: dict[str, str] = {}
    for match in _ARP_NODE.finditer(result.stdout.decode(errors='replace')):
        mac = ':'.join(f'{int(part, 16):02x}' for part in match['mac'].split(':'))
        if mac == 'ff:ff:ff:ff:ff:ff' or mac.startswith('01:00:5e:'):
            continue
        nodes[mac] = match['host']
    return sorted(nodes.items())


def _is_authentication_failure(result: subprocess.CompletedProcess[bytes]) -> bool:
    return b'permission denied' in result.stderr.lower()


def _is_connection_failure(result: subprocess.CompletedProcess[bytes]) -> bool:
    detail = result.stderr.lower()
    return any(
        value in detail
        for value in (
            b'connection refused',
            b'connection timed out',
            b'connection reset by peer',
            b'network is unreachable',
            b'no route to host',
            b'operation timed out',
        )
    )


def _recs_files(
    run: Callable[..., subprocess.CompletedProcess[bytes]], host: str
) -> list[RemoteFile]:
    result = _ssh(run, host, _LIST_RECS_FILES)
    if result.returncode != 0:
        raise OSError(result.stderr.decode(errors='replace').strip())
    values = result.stdout.split(b'\0')
    if values.pop() != b'':
        raise OSError('remote recs file list is incomplete')
    files: list[RemoteFile] = []
    if len(values) % 3:
        raise OSError('remote recs file list is incomplete')
    for path, mtime, size in zip(values[::3], values[1::3], values[2::3], strict=True):
        relative_path = Path(path.decode(errors='surrogateescape'))
        if relative_path.is_absolute() or '..' in relative_path.parts:
            raise OSError(f'invalid remote path: {relative_path}')
        files.append(
            RemoteFile(
                relative_path=relative_path,
                mtime_ns=int(mtime) * 1_000_000_000,
                size=int(size),
            )
        )
    return files


def _backup_remote_file(
    source: NetworkRecsSource,
    file: RemoteFile,
    backup_root: Path,
    catalog: Catalog,
    dry_run: bool,
    projects: set[str],
    run: Callable[..., subprocess.CompletedProcess[bytes]],
) -> FileResult:
    project = (
        file.relative_path.parts[0] if file.relative_path.parts[0] in projects else None
    )
    candidate = _candidate(source, file, project)
    destination = (
        backup_root / 'audio' / file.relative_path
        if project is not None
        else backup_root / 'photo' / source.name / file.relative_path
    )
    if _matches_catalog(source, file, destination, catalog):
        return _result(candidate, 'unchanged')
    if dry_run:
        return _result(candidate, 'would_copy')
    ensure_destination_parent(backup_root, destination.parent)
    descriptor, name = tempfile.mkstemp(
        prefix='.baccy-', suffix='.tmp', dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            result = _ssh(
                run,
                source.host,
                _read_command(file.relative_path),
                stdout=output,
            )
        if result.returncode != 0:
            raise OSError(result.stderr.decode(errors='replace').strip())
        if temporary.stat().st_size != file.size:
            return _result(candidate, 'deferred', 'remote source changed while copying')
        os.utime(temporary, ns=(file.mtime_ns, file.mtime_ns))
        return commit_snapshot(
            candidate,
            temporary.stat(),
            temporary,
            sha256(temporary),
            destination,
            backup_root,
            catalog,
        )
    finally:
        if temporary.exists():
            temporary.unlink()


def _candidate(
    source: NetworkRecsSource, file: RemoteFile, project: str | None
) -> Candidate:
    network_source = NetworkSource(
        kind='network', name=source.name, mac=source.mac, host=source.host
    )
    return Candidate(
        source=ResolvedSource(source=network_source, root=Path('/network')),
        path=Path('/network') / file.relative_path,
        relative_path=file.relative_path,
        priority=0,
        project=project,
    )


def _matches_catalog(
    source: NetworkRecsSource,
    file: RemoteFile,
    destination: Path,
    catalog: Catalog,
) -> bool:
    record = catalog.latest(source.name, file.relative_path)
    return (
        record is not None
        and record.get('size') == file.size
        and record.get('mtime_ns') == file.mtime_ns
        and destination.is_file()
        and destination.stat().st_size == file.size
    )


def _read_command(relative_path: Path) -> str:
    value = base64.b64encode(relative_path.as_posix().encode()).decode()
    return (
        f'path=$(printf %s {value} | (base64 -D 2>/dev/null || base64 -d)); '
        'cat "$HOME/recs/$path"'
    )


def _ssh(
    run: Callable[..., subprocess.CompletedProcess[bytes]],
    host: str,
    command: str,
    **kwargs: object,
) -> subprocess.CompletedProcess[bytes]:
    if 'stdout' not in kwargs:
        kwargs['capture_output'] = True
    else:
        kwargs['stderr'] = subprocess.PIPE
    return run(['ssh', *_SSH_OPTIONS, host, command], check=False, **kwargs)


def _result(candidate: Candidate, status: str, detail: str | None = None) -> FileResult:
    return FileResult(
        source=candidate.source.source.name,
        relative_path=candidate.relative_path,
        status=status,
        detail=detail,
    )
