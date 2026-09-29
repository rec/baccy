import errno
import logging
import signal
import threading
import time
from collections.abc import Callable
from typing import cast

from .backup import run_backup
from .models import BackupSummary, FileResult, Settings

NETWORK_POLL_SECONDS = 10.0
_LOGGER = logging.getLogger(__name__)


def watch(
    settings: Settings,
    stop: threading.Event | None = None,
    trigger: threading.Event | None = None,
    action: Callable[[Settings], BackupSummary] = run_backup,
    report: Callable[[BackupSummary], None] | None = None,
) -> None:
    stopping = threading.Event() if stop is None else stop
    previous_handlers = _install_signal_handlers(stopping)
    try:
        while not stopping.is_set():
            if trigger is not None:
                trigger.clear()
            try:
                summary = action(settings)
            except OSError as error:
                if error.errno in {errno.ENOSPC, errno.EROFS}:
                    raise
                _LOGGER.exception('backup pass failed')
                summary = BackupSummary().with_result(
                    FileResult(source='watch', status='failed', detail=str(error))
                )
            if report is not None:
                try:
                    report(summary)
                except OSError:
                    _LOGGER.exception('backup report failed')
            timeout = min(settings.poll_seconds, NETWORK_POLL_SECONDS)
            if trigger is None:
                stopping.wait(timeout)
            else:
                deadline = time.monotonic() + timeout
                while not stopping.is_set() and not trigger.is_set():
                    if (remaining := deadline - time.monotonic()) <= 0:
                        break
                    stopping.wait(min(remaining, 0.1))
    finally:
        _restore_signal_handlers(previous_handlers)


def _install_signal_handlers(stop: threading.Event) -> dict[int, signal.Handlers]:
    handlers: dict[int, signal.Handlers] = {}

    def stop_handler(signum: int, frame: object) -> None:
        stop.set()

    for value in (signal.SIGINT, signal.SIGTERM):
        handlers[value] = cast(signal.Handlers, signal.signal(value, stop_handler))
    return handlers


def _restore_signal_handlers(handlers: dict[int, signal.Handlers]) -> None:
    for value, handler in handlers.items():
        signal.signal(value, handler)
