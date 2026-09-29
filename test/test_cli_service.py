import tomllib
from pathlib import Path
from types import SimpleNamespace

from pytest import CaptureFixture, MonkeyPatch
from reccy.services.models import StatusResult

from baccy.cli import main


def test_service_commands_print_toml(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    metadata = tmp_path / 'daemon.json'
    metadata.write_text('{"argv": ["watch", "--config", "baccy.toml"]}')

    class ServiceApplication:
        paths = SimpleNamespace(metadata=metadata)

        def service_status(self) -> StatusResult:
            return StatusResult(installed=True, running=True, details='ready')

    monkeypatch.setattr('baccy.service_cli.Application', ServiceApplication)

    exit_code = main(['service', 'status'])

    assert exit_code == 0
    assert tomllib.loads(capsys.readouterr().out) == {
        'installed': True,
        'running': True,
        'details': 'ready',
    }


def test_service_install_waits_for_daemon_and_schedules_sync(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'
    installed: list[list[str]] = []
    calls: list[str] = []

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> StatusResult:
            installed.append(arguments)
            return StatusResult(installed=True)

        def service_status(self) -> StatusResult:
            raise AssertionError('running daemon should not need a status check')

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            assert value == endpoint
            assert role == 'baccy-cli'

        def call(self, command: str) -> dict[str, bool]:
            calls.append(command)
            return {'running': True} if command == 'status' else {'scheduled': True}

    monkeypatch.setattr('baccy.service_cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.service_cli.rpc.Client', Client)

    assert main(['--config', str(tmp_path / 'baccy.toml'), 'service', 'install']) == 0
    assert installed == [['--config', str(tmp_path / 'baccy.toml'), 'watch']]
    assert calls == ['status', 'sync']
    assert tomllib.loads(capsys.readouterr().out) == {
        'installed': True,
        'details': '',
    }


def test_service_install_reports_daemon_start_failure(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> StatusResult:
            return StatusResult(installed=True)

        def service_status(self) -> StatusResult:
            return StatusResult(installed=True, running=False, details='exited')

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            pass

        def call(self, command: str) -> dict[str, bool]:
            raise FileNotFoundError('socket missing')

    monkeypatch.setattr('baccy.service_cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.service_cli.rpc.Client', Client)

    assert main(['--config', str(tmp_path / 'baccy.toml'), 'service', 'install']) == 1
    assert capsys.readouterr().err == 'baccy daemon failed to start: exited\n'


def test_service_install_checks_release_identity_and_rolls_back(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'
    current = tmp_path / 'new' / 'python'
    previous = tmp_path / 'old' / 'python'
    calls: list[str] = []

    class ServiceApplication:
        control_endpoint = endpoint
        installed_executable = current

        def install_service(self, arguments: list[str]) -> StatusResult:
            return StatusResult(installed=True)

        def rollback_service(self) -> None:
            calls.append('rollback')
            self.installed_executable = previous

        def service_status(self) -> StatusResult:
            return StatusResult(installed=True, running=False, details='exited')

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            pass

        def call(self, command: str) -> dict[str, object]:
            return {'running': True, 'executable': str(previous)}

    monkeypatch.setattr('baccy.service_cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.service_cli.rpc.Client', Client)
    monkeypatch.setattr('baccy.service_cli.time.sleep', lambda duration: None)

    assert main(['--config', str(tmp_path / 'baccy.toml'), 'service', 'install']) == 1
    assert calls == ['rollback']
    assert 'older release' in capsys.readouterr().err


def test_service_install_can_skip_sync(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'
    calls: list[str] = []

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> StatusResult:
            return StatusResult(installed=True)

        def service_status(self) -> StatusResult:
            raise AssertionError('running daemon should not need a status check')

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            pass

        def call(self, command: str) -> dict[str, bool]:
            calls.append(command)
            return {'running': True}

    monkeypatch.setattr('baccy.service_cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.service_cli.rpc.Client', Client)

    assert (
        main(
            [
                '--config',
                str(tmp_path / 'baccy.toml'),
                'service',
                'install',
                '--no-sync',
            ]
        )
        == 0
    )
    assert calls == ['status']


def test_service_dry_run_rejects_unknown_action(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text('')

    assert main(['--config', str(config), '--dry-run', 'service', 'unknown']) == 2
    assert 'unknown service command: unknown' in capsys.readouterr().err
