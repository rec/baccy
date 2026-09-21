import plistlib
import subprocess
from pathlib import Path

import pytest
from reccy.services import models, renderers

from baccy.application import BACCY_SERVICE, Application, _install_service_release
from baccy.models import BackupSummary, FileResult, RecognizedSource


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


def test_application_renders_launch_agent_with_release_interpreter(
    tmp_path: Path,
) -> None:
    application = Application(home=tmp_path, platform=models.Platform.macos)
    executable = tmp_path / 'release' / 'venv' / 'bin' / 'python'
    metadata = application.service_metadata(['watch'], executable)

    definition = renderers.macos_launch_agent(
        metadata, application.paths, BACCY_SERVICE
    )
    plist = plistlib.loads(definition.content.encode())

    assert plist['ProgramArguments'][0] == str(executable)


def test_install_service_release_builds_isolated_wheels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if command[1] == 'build':
            wheels = Path(command[command.index('--out-dir') + 1])
            wheels.mkdir(exist_ok=True)
            name = 'reccy' if command[-1].endswith('/reccy') else 'baccy'
            (wheels / f'{name}-0.1.0-py3-none-any.whl').touch()
        elif command[1] == 'venv':
            executable = Path(command[-1]) / 'bin' / 'python'
            executable.parent.mkdir(parents=True)
            executable.touch()
        return subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr('baccy.application.subprocess.run', run)
    monkeypatch.setattr('baccy.application.time.time_ns', lambda: 123)

    executable = _install_service_release(tmp_path)

    assert executable == (
        tmp_path
        / 'Library'
        / 'Application Support'
        / 'baccy'
        / 'releases'
        / '123'
        / 'venv'
        / 'bin'
        / 'python'
    )
    assert [command[1] for command in commands] == ['build', 'build', 'venv', 'pip']
    assert commands[-1][2] == 'install'
    assert '--no-sources' in commands[-1]


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


def test_application_notifies_recognized_sources_and_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifications: list[str] = []
    monkeypatch.setattr('baccy.application.notify', notifications.append)
    application = Application(home=tmp_path, platform=models.Platform.macos)
    source = RecognizedSource(
        source='network-aabb', label='studio.local', kind='machine'
    )
    application.start()
    try:
        application.record_recognized_sources([source])
        application.record_summary(BackupSummary())
        application.record_recognized_sources([source])
        application.record_summary(BackupSummary())
    finally:
        application.close()

    assert notifications == [
        'Recognized machine studio.local; starting backup.',
        'Backup complete for machine studio.local.',
    ]
