import subprocess
import sys

from .models import FileResult

_DISPLAY_NOTIFICATION = (
    'on run argv\ndisplay notification (item 1 of argv) with title "baccy"\nend run'
)


def notify(message: str) -> None:
    result = subprocess.run(
        ['osascript', '-e', _DISPLAY_NOTIFICATION, message],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        print(result.stderr.decode(errors='replace').strip(), file=sys.stderr)


def notify_failures(results: list[FileResult]) -> None:
    notify(_failure_message(results))


def _failure_message(results: list[FileResult]) -> str:
    first = results[0]
    path = first.relative_path.as_posix() if first.relative_path else 'source'
    message = f'{first.source}: {path}'
    if first.detail:
        message += f' ({first.detail})'
    if len(results) > 1:
        message += f' and {len(results) - 1} more'
    return message
