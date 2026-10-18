import io
import os
import subprocess
import threading
from pathlib import Path
from typing import BinaryIO

import pytest

from baccy import recs_monitor
from baccy.recs_monitor import BackupPaused, RecsMonitor


@pytest.mark.parametrize(
    'transition', [b'true', b'null', b'true}\n{"listening":false', b'broken']
)
def test_busy_or_unknown_transition_abandons_transfer(
    transition: bytes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reader, writer = os.pipe()
    stream = os.fdopen(reader, 'rb')
    os.write(writer, b'{"listening":false}\n')
    processes: list[object] = []

    class Process:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            self.stdin = io.BytesIO()
            self.stdout = stream if command == ['state'] else None
            self.returncode: int | None = None
            processes.append(self)
            if self.stdout is None:
                kwargs['stdout'].write(b'partial')
                os.write(writer, b'{"listening":' + transition + b'}\n')

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            assert self.stdin.closed
            self.returncode = 0
            return 0

    monkeypatch.setattr(recs_monitor.subprocess, 'Popen', Process)
    try:
        with RecsMonitor(['state'], threading.Event()) as monitor:
            with (tmp_path / 'partial').open('wb') as output:
                with pytest.raises(BackupPaused):
                    monitor.run(['copy'], check=False, timeout=10, stdout=output)
        assert all(p.returncode == 0 for p in processes)
    finally:
        os.close(writer)


def test_shutdown_or_closed_state_stream_blocks_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    stop.set()
    monitor = RecsMonitor(['state'], stop)
    with pytest.raises(BackupPaused, match='stopped'):
        monitor.poll()
    stop.clear()
    with pytest.raises(BackupPaused, match='closed'):
        monitor.poll()
    monitor.close()


@pytest.mark.parametrize('cancel', [False, True])
def test_remote_reader_finishes_or_is_cancelled_via_stdin_eof(
    cancel: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reader, writer = os.pipe()
    stream = os.fdopen(reader, 'rb')
    os.write(writer, b'{"listening":false}\n')
    popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    class StateProcess:
        stdin = io.BytesIO()
        stdout = stream

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def spawn(command: list[str], **kwargs: object) -> object:
        if command == ['state']:
            return StateProcess()
        child = popen(['sh', '-c', command[-1]], **kwargs)
        children.append(child)
        if cancel:
            os.write(writer, b'{"listening":true}\n')
        return child

    monkeypatch.setattr(recs_monitor.subprocess, 'Popen', spawn)
    try:
        with RecsMonitor(['state'], threading.Event()) as monitor:
            with (tmp_path / 'output').open('wb') as output:
                if cancel:
                    with pytest.raises(BackupPaused):
                        monitor.run(
                            ['ssh', 'host', 'exec sleep 60'],
                            check=False,
                            timeout=10,
                            stdout=output,
                        )
                else:
                    result = monitor.run(
                        ['ssh', 'host', 'exec printf data'],
                        check=False,
                        timeout=10,
                        stdout=output,
                    )
                    assert result.returncode == 0
        # The remote shell's wait reports its child's SIGTERM. Killing local
        # SSH alone would instead leave the child running and return -SIGTERM.
        assert children[0].returncode == (143 if cancel else 0)
        if not cancel:
            assert (tmp_path / 'output').read_bytes() == b'data'
    finally:
        os.close(writer)


def test_cancelled_network_copy_keeps_completed_files_and_retries_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from baccy.catalog import Catalog
    from baccy.network import NetworkRecsSource, backup_network_source

    source = NetworkRecsSource(mac='aa:bb:cc:dd:ee:ff', host='recorder')
    busy = True
    interrupt = True
    copies = 0

    class Monitor:
        def __init__(self, command: list[str], stop: threading.Event) -> None:
            pass

        def __enter__(self) -> object:
            if busy:
                raise BackupPaused('listening')
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def run(
            self, command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal copies
            if 'find . -type f' in command[-1]:
                return subprocess.CompletedProcess(
                    command, 0, b'p/a.txt\x001\x004\x00p/b.txt\x001\x004\x00', b''
                )
            if 'stdout' in kwargs:
                copies += 1
                output: BinaryIO = kwargs['stdout']
                output.write(b'data')
                if interrupt and copies == 2:
                    raise BackupPaused('listening started')
                return subprocess.CompletedProcess(command, 0, b'', b'')
            return subprocess.CompletedProcess(command, 0, b'1 4', b'')

    monkeypatch.setattr('baccy.network.RecsMonitor', Monitor)
    root = tmp_path / 'backup'
    catalog = Catalog(root)
    assert backup_network_source(source, root, catalog, False)[0].status == 'deferred'
    assert copies == 0
    busy = False
    results = backup_network_source(source, root, catalog, False)
    assert [r.status for r in results] == ['copied', 'deferred']
    assert (root / 'audio/p/a.txt').read_bytes() == b'data'
    assert not (root / 'audio/p/b.txt').exists()
    assert not list(root.rglob('.baccy-*.tmp'))
    interrupt = False
    results = backup_network_source(source, root, catalog, False)
    assert [r.status for r in results] == ['unchanged', 'copied']
    assert copies == 3
