import logging
import subprocess

import pytest

from baccy.notifications import notify


def test_notify_uses_system_osascript_and_logs_failures(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, b'', b'notification denied')

    monkeypatch.setattr('baccy.notifications.subprocess.run', run)
    caplog.set_level(logging.ERROR, logger='baccy.notifications')

    notify('hello')

    assert commands[0][0] == '/usr/bin/osascript'
    assert 'macOS notification failed: notification denied' in caplog.text
