import signal
import threading
from collections.abc import Callable
from typing import cast

from .backup import run_backup
from .models import BackupSummary, Settings

NETWORK_POLL_SECONDS = 10.0


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
            summary = action(settings)
            if report is not None:
                report(summary)
            timeout = min(settings.poll_seconds, NETWORK_POLL_SECONDS)
            if trigger is None:
                stopping.wait(timeout)
            else:
                trigger.wait(timeout)
                trigger.clear()
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
