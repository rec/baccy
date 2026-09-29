import os
import signal
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
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


@pytest.mark.parametrize('command', [['service', 'install'], ['install']])
@pytest.mark.parametrize('verbose_flag', [None, '--verbose', '-v'])
def test_service_install_waits_for_daemon_and_schedules_sync(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    command: list[str],
    verbose_flag: str | None,
) -> None:
    endpoint = tmp_path / 'gui.sock'
    installed: list[list[str]] = []
    calls: list[str] = []
    status_requests = 0

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> None:
            installed.append(arguments)
            os.write(1, b'wheel build output\n')
            os.write(2, b'package install output\n')

        def service_status(self) -> StatusResult:
            nonlocal status_requests
            status_requests += 1
            return StatusResult(installed=True)

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            assert value == endpoint
            assert role == 'baccy-cli'

        def call(self, command: str) -> dict[str, bool]:
            calls.append(command)
            return {'running': True} if command == 'status' else {'scheduled': True}

    monkeypatch.setattr('baccy.service_cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.service_cli.rpc.Client', Client)

    global_flags = ['--config', str(tmp_path / 'baccy.toml')]
    if verbose_flag is not None:
        global_flags.append(verbose_flag)
    assert main([*global_flags, *command]) == 0
    assert installed == [['--config', str(tmp_path / 'baccy.toml'), 'watch']]
    assert calls == ['status', 'status', 'sync']
    assert status_requests == (0 if verbose_flag is None else 1)
    captured = capsys.readouterr()
    assert captured.err == ''
    output = captured.out
    if verbose_flag is None:
        assert output == 'ok\n'
    else:
        assert tomllib.loads(output) == {'installed': True, 'details': ''}


def test_service_install_reports_daemon_start_failure(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> StatusResult:
            os.write(1, b'wheel build output\n')
            os.write(2, b'package install output\n')
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
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'wheel build output\n' in captured.err
    assert 'package install output\n' in captured.err
    assert 'warning: no baccy daemon is running; installing one\n' in captured.err
    assert 'baccy daemon failed to start: exited\n' in captured.err


def test_service_install_replays_build_output_on_failure(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> None:
            os.write(1, b'building wheel\n')
            os.write(2, b'build diagnostic\n')
            subprocess.run([sys.executable, '--version'], check=True)
            raise subprocess.CalledProcessError(1, ['uv', 'build'])

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            pass

        def call(self, command: str) -> dict[str, bool]:
            return {'running': True}

    monkeypatch.setattr('baccy.service_cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.service_cli.rpc.Client', Client)

    assert main(['--config', str(tmp_path / 'baccy.toml'), 'install']) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'building wheel\n' in captured.err
    assert 'build diagnostic\n' in captured.err
    assert 'Python ' in captured.err
    assert 'baccy service installation failed:' in captured.err


def test_service_install_replaces_an_absent_daemon(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'
    calls = 0

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> StatusResult:
            return StatusResult(installed=True)

        def service_status(self) -> StatusResult:
            return StatusResult(installed=False, running=False)

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            assert value == endpoint
            assert role == 'baccy-cli'

        def call(self, command: str) -> dict[str, bool]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionRefusedError()
            return {'running': True} if command == 'status' else {'scheduled': True}

    monkeypatch.setattr('baccy.service_cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.service_cli.rpc.Client', Client)

    assert main(['--config', str(tmp_path / 'baccy.toml'), 'service', 'install']) == 0
    assert capsys.readouterr() == ('ok\n', '')


def test_service_install_terminates_an_unresponsive_daemon(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'
    signals: list[str] = []
    sleeps: list[float] = []

    class Controller:
        def signal(self, value: signal.Signals) -> None:
            signals.append(value.name)

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> StatusResult:
            return StatusResult(installed=True)

        def service_controller(self) -> Controller:
            return Controller()

        def service_status(self) -> StatusResult:
            return StatusResult(installed=True, running=len(signals) < 3)

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            pass

        def call(self, command: str) -> dict[str, bool]:
            if command == 'status' and len(signals) < 3:
                raise ConnectionRefusedError()
            return {'running': True} if command == 'status' else {'scheduled': True}

    monkeypatch.setattr('baccy.service_cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.service_cli.rpc.Client', Client)
    monkeypatch.setattr('baccy.service_cli.time.sleep', sleeps.append)

    assert (
        main(
            [
                '--config',
                str(tmp_path / 'baccy.toml'),
                'service',
                'install',
                '--shutdown-wait-seconds',
                '0.5',
            ]
        )
        == 0
    )
    assert signals == ['SIGINT', 'SIGTERM', 'SIGKILL']
    assert sleeps == [0.5, 0.5, 0.5]
    assert capsys.readouterr() == ('ok\n', '')


def test_service_install_reports_a_daemon_that_will_not_stop(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    endpoint = tmp_path / 'gui.sock'
    signals: list[str] = []

    class Controller:
        def signal(self, value: signal.Signals) -> None:
            signals.append(value.name)

    class ServiceApplication:
        control_endpoint = endpoint

        def install_service(self, arguments: list[str]) -> StatusResult:
            raise AssertionError('unresponsive daemon must stop before installation')

        def service_controller(self) -> Controller:
            return Controller()

        def service_status(self) -> StatusResult:
            return StatusResult(installed=True, running=True)

    class Client:
        def __init__(self, value: Path, *, role: str) -> None:
            pass

        def call(self, command: str) -> dict[str, bool]:
            raise ConnectionRefusedError()

    monkeypatch.setattr('baccy.service_cli.Application', ServiceApplication)
    monkeypatch.setattr('baccy.service_cli.rpc.Client', Client)
    monkeypatch.setattr('baccy.service_cli.time.sleep', lambda seconds: None)

    assert main(['--config', str(tmp_path / 'baccy.toml'), 'service', 'install']) == 1
    assert signals == ['SIGINT', 'SIGTERM', 'SIGKILL']
    assert capsys.readouterr().err == (
        'baccy daemon is unresponsive; sending SIGINT\n'
        'baccy daemon is unresponsive; sending SIGTERM\n'
        'baccy daemon is unresponsive; sending SIGKILL\n'
        'baccy service installation failed: baccy daemon did not stop after SIGKILL\n'
    )


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
    assert calls == ['status', 'status']


def test_service_dry_run_rejects_unknown_action(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    config = tmp_path / 'baccy.toml'
    config.write_text('')

    assert main(['--config', str(config), '--dry-run', 'service', 'unknown']) == 2
    assert 'unknown service command: unknown' in capsys.readouterr().err
