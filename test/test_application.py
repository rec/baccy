import plistlib
from pathlib import Path

import pytest
from reccy.services import models, renderers

from baccy.application import BACCY_SERVICE, Application
from baccy.models import BackupSummary, FileResult


def test_application_renders_launch_agent(tmp_path: Path) -> None:
    application = Application(home=tmp_path, platform=models.Platform.macos)
    metadata = application.service_metadata(['watch', '--config', '/tmp/baccy.toml'])

    definition = renderers.macos_launch_agent(
        metadata, application.paths, BACCY_SERVICE
    )
    plist = plistlib.loads(definition.content.encode())

    assert definition.path == tmp_path / 'Library/LaunchAgents/com.swirly.baccy.plist'
    assert plist['Label'] == 'com.swirly.baccy'
    assert plist['RunAtLoad'] is True
    assert plist['KeepAlive'] is True
    assert plist['ProgramArguments'][-3:] == ['watch', '--config', '/tmp/baccy.toml']


def test_application_persists_last_backup_summary(tmp_path: Path) -> None:
    application = Application(home=tmp_path, platform=models.Platform.macos)
    application.start()
    try:
        application.record_summary(BackupSummary(copied=3))
        assert application.status_snapshot().summary == BackupSummary(copied=3)
    finally:
        application.close()


def test_application_notifies_each_new_failure_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifications: list[list[FileResult]] = []
    monkeypatch.setattr('baccy.application.notify_failures', notifications.append)
    application = Application(home=tmp_path, platform=models.Platform.macos)
    failure = FileResult(source='source', status='failed', detail='disk full')
    application.start()
    try:
        application.record_summary(BackupSummary(failed=1, results=[failure]))
        application.record_summary(BackupSummary(failed=1, results=[failure]))
        application.record_summary(BackupSummary())
        application.record_summary(BackupSummary(failed=1, results=[failure]))
    finally:
        application.close()

    assert notifications == [[failure], [failure]]
