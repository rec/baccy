"""Cancel SSH backup operations when the recorder becomes busy or unreachable."""

from __future__ import annotations

import json
import os
import selectors
import shlex
import subprocess
import tempfile
import threading
import time
from typing import BinaryIO


class BackupPaused(RuntimeError):
    pass


class RecsMonitor:
    def __init__(self, command: list[str], stop: threading.Event) -> None:
        self.command = command
        self.stop = stop
        self.listening: bool | None = None
        self.cancelled = False
        self.updated = time.monotonic()
        self.buffer = b''
        self.selector = selectors.DefaultSelector()
        self.process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> RecsMonitor:
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert self.process.stdout is not None
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 10
            while self.listening is None and time.monotonic() < deadline:
                self.poll(0.1)
            self.check()
            return self
        except (OSError, BackupPaused, KeyboardInterrupt):
            self.close()
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def poll(self, timeout: float = 0) -> None:
        if self.stop.is_set():
            raise BackupPaused('backup stopped')
        if self.process is None or self.process.poll() is not None:
            raise BackupPaused('recs state connection closed')
        for key, _ in self.selector.select(timeout):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                raise BackupPaused('recs state connection closed')
            self.buffer += chunk
            if len(self.buffer) > 65536:
                raise BackupPaused('invalid recs state stream')
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                try:
                    value = json.loads(line)
                except ValueError as error:
                    raise BackupPaused('invalid recs state stream') from error
                if (
                    not isinstance(value, dict)
                    or 'listening' not in value
                    or (
                        value['listening'] is not None
                        and type(value['listening']) is not bool
                    )
                ):
                    raise BackupPaused('invalid recs state stream')
                self.listening = value['listening']
                self.updated = time.monotonic()
                # Never erase an intervening busy/unknown transition when several
                # messages arrive together before the transfer checks its state.
                self.cancelled |= self.listening is not False
        if time.monotonic() - self.updated > 10:
            raise BackupPaused('recs state connection is unresponsive')

    def check(self) -> None:
        self.poll()
        if self.cancelled or self.listening is not False:
            raise BackupPaused('recs is listening or its state is unknown')

    def run(
        self,
        command: list[str],
        *,
        check: bool,
        timeout: float,
        capture_output: bool = False,
        stdout: BinaryIO | None = None,
        stderr: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        self.check()
        if stdout is not None:
            command = [
                *command[:-1],
                'exec 3<&0; '
                f'sh -c {shlex.quote(command[-1])} </dev/null & copy=$!; '
                '(while IFS= read -r line; do :; done; '
                'kill "$copy" 2>/dev/null) <&3 & guard=$!; '
                'trap \'kill "$copy" "$guard" 2>/dev/null\' EXIT HUP INT TERM; '
                'wait "$copy"',
            ]
        with tempfile.TemporaryFile() as captured, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=captured if stdout is None else stdout,
                stderr=errors,
            )
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    self.poll(0.1)
                    self.check()
                    if time.monotonic() >= deadline:
                        raise OSError('SSH backup operation timed out')
                self.check()
                captured.seek(0)
                errors.seek(0)
                return subprocess.CompletedProcess(
                    command, process.returncode, captured.read(), errors.read()
                )
            finally:
                _finish(process)

    def close(self) -> None:
        self.selector.close()
        if self.process is not None:
            _finish(self.process)
            if self.process.stdout is not None:
                self.process.stdout.close()


def _finish(process: subprocess.Popen[bytes]) -> None:
    # Closing stdin tells the remote bridge/reader to stop, including its cat
    # process. Only terminate local SSH if that graceful cancellation stalls.
    if process.stdin is not None:
        process.stdin.close()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
