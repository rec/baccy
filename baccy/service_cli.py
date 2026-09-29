import signal
import subprocess
import sys
import time
from pathlib import Path

import tomlkit
import tyro
from pydantic import BaseModel
from reccy.protocol import rpc

from .application import Application


class InstallCommand(BaseModel, frozen=True):
    """Install the per-user baccy LaunchAgent."""

    sync: bool = True
    shutdown_wait_seconds: float = 2.0


class ServiceCommand(BaseModel, frozen=True):
    """Manage the per-user baccy LaunchAgent."""


def service(arguments: list[str], config: Path, dry_run: bool) -> int:
    if not arguments or arguments[0] in {'-h', '--help'}:
        print(_service_usage())
        return 0
    command, rest = arguments[0], arguments[1:]
    if command not in {'install', 'uninstall', 'start', 'stop', 'restart', 'status'}:
        print(f'unknown service command: {command}', file=sys.stderr)
        print(_service_usage(), file=sys.stderr)
        return 2
    if dry_run:
        print(tomlkit.dumps({'command': command, 'dry_run': True}), end='')
        return 0
    application = Application()
    if command == 'install':
        install = tyro.cli(InstallCommand, args=rest, prog='baccy service install')
        try:
            _stop_unresponsive_daemon(application, install.shutdown_wait_seconds)
            result = application.install_service(['--config', str(config), 'watch'])
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print(f'baccy service installation failed: {error}', file=sys.stderr)
            return 1
        if error := _wait_for_daemon(application):
            if hasattr(application, 'rollback_service'):
                try:
                    application.rollback_service()
                except (OSError, ValueError, subprocess.SubprocessError) as rollback:
                    print(
                        f'baccy daemon failed to start: {error}; '
                        f'rollback failed: {rollback}',
                        file=sys.stderr,
                    )
                    return 1
                if getattr(application, 'installed_executable', None) is not None:
                    if rollback_error := _wait_for_daemon(application):
                        print(
                            f'baccy daemon failed to start: {error}; '
                            f'previous daemon did not restart: {rollback_error}',
                            file=sys.stderr,
                        )
                        return 1
            print(f'baccy daemon failed to start: {error}', file=sys.stderr)
            return 1
        if hasattr(application, 'prune_releases'):
            try:
                application.prune_releases()
            except OSError as error:
                print(f'baccy release cleanup failed: {error}', file=sys.stderr)
        if result is None:
            result = application.service_status()
        if install.sync and (
            error := request_daemon_sync(application.control_endpoint, attempts=1)
        ):
            print(f'could not request baccy daemon sync: {error}', file=sys.stderr)
            return 1
    elif command == 'uninstall':
        _parse_service_command(rest, command)
        result = application.uninstall_service()
    elif command == 'start':
        _parse_service_command(rest, command)
        result = application.start_service()
    elif command == 'stop':
        _parse_service_command(rest, command)
        result = application.stop_service()
    elif command == 'restart':
        _parse_service_command(rest, command)
        result = application.restart_service()
    elif command == 'status':
        _parse_service_command(rest, command)
        result = application.service_status()
    else:
        print(f'unknown service command: {command}', file=sys.stderr)
        print(_service_usage(), file=sys.stderr)
        return 2
    if result is None:
        result = application.service_status()
    sys.stdout.write(tomlkit.dumps(result.model_dump(mode='json', exclude_none=True)))
    return 0


def _parse_service_command(arguments: list[str], command: str) -> None:
    tyro.cli(ServiceCommand, args=arguments, prog=f'baccy service {command}')


def _stop_unresponsive_daemon(application: Application, wait_seconds: float) -> None:
    try:
        rpc.Client(application.control_endpoint, role='baccy-cli').call('status')
        return
    except BrokenPipeError, ConnectionError, OSError, TimeoutError:
        service = application.service_status()
    if service.running is not True:
        print('warning: no baccy daemon is running; installing one', file=sys.stderr)
        return
    controller = application.service_controller()
    for value in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        print(f'baccy daemon is unresponsive; sending {value.name}', file=sys.stderr)
        controller.signal(value)
        time.sleep(wait_seconds)
        if application.service_status().running is not True:
            return
    raise RuntimeError('baccy daemon did not stop after SIGKILL')


def _wait_for_daemon(application: Application) -> str | None:
    error = 'daemon control socket did not appear'
    for attempt in range(50):
        try:
            status = rpc.Client(application.control_endpoint, role='baccy-cli').call(
                'status'
            )
        except (BrokenPipeError, ConnectionError, OSError, TimeoutError) as value:
            error = str(value)
            service = application.service_status()
            if service.running is False:
                return service.details or error
            if attempt < 49:
                time.sleep(0.1)
            continue
        if isinstance(status, dict) and status.get('running') is True:
            expected = getattr(application, 'installed_executable', None)
            if expected is not None and status.get('executable') != str(expected):
                error = 'daemon is running an older release'
                if attempt < 49:
                    time.sleep(0.1)
                    continue
                return error
            return None
        return 'daemon reported that it is not running'
    return error


def request_daemon_sync(endpoint: Path | str, attempts: int = 20) -> str | None:
    error = 'daemon control socket did not appear'
    for attempt in range(attempts):
        try:
            rpc.Client(endpoint, role='baccy-cli').call('sync')
            return None
        except FileNotFoundError as value:
            error = str(value)
            if attempt == attempts - 1:
                return error
            time.sleep(0.1)
        except (BrokenPipeError, ConnectionError, OSError, TimeoutError) as value:
            return str(value)
    return error


def _service_usage() -> str:
    return 'Usage: baccy service {install,uninstall,start,stop,restart,status}'
