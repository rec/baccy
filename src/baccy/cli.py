import json
import os
import sys
from pathlib import Path
from typing import Annotated

import tyro
from pydantic import BaseModel, Field

from .application import Application
from .backup import run_backup
from .config import default_config_path, load_or_default
from .models import BackupSummary, Settings
from .network import NetworkDiscovery
from .watch import watch


class ConfigCommand(BaseModel, frozen=True):
    config: Annotated[
        Path, tyro.conf.arg(help='Path to the baccy TOML configuration.')
    ] = Field(default_factory=default_config_path)


class BackupCommand(ConfigCommand):
    dry_run: Annotated[bool, tyro.conf.arg(aliases=('-d',))] = False


class WatchCommand(BackupCommand):
    """Run backup passes until interrupted."""


class InstallCommand(ConfigCommand):
    """Install the per-user baccy LaunchAgent."""


class ServiceCommand(BaseModel, frozen=True):
    """Manage the per-user baccy LaunchAgent."""


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments or arguments[0] in {'-h', '--help'}:
        print(_usage())
        return 0
    command, rest = arguments[0], arguments[1:]
    if command == 'backup':
        value = tyro.cli(BackupCommand, args=rest, prog='baccy backup')
        return _backup(value)
    if command == 'watch':
        value = tyro.cli(WatchCommand, args=rest, prog='baccy watch')
        return _watch(value)
    if command == 'service':
        return _service(rest)
    print(f'unknown command: {command}', file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


def _backup(command: BackupCommand) -> int:
    settings = load_or_default(command.config)
    summary = run_backup(settings, dry_run=command.dry_run)
    _print_summary(summary, settings.verbose)
    return 1 if summary.failed or summary.unavailable else 0


def _watch(command: WatchCommand) -> int:
    settings = load_or_default(command.config)
    network = NetworkDiscovery()

    def action(value: Settings) -> BackupSummary:
        return run_backup(value, dry_run=command.dry_run, network=network)

    if os.environ.get('BACCY_DAEMON') == '1':
        application = Application()
        application.start()
        try:
            watch(
                settings,
                action=action,
                report=lambda summary: _report(application, summary, settings.verbose),
            )
        finally:
            application.close()
    else:
        watch(
            settings,
            action=action,
            report=lambda summary: _print_summary(summary, settings.verbose),
        )
    return 0


def _service(arguments: list[str]) -> int:
    if not arguments or arguments[0] in {'-h', '--help'}:
        print(_service_usage())
        return 0
    command, rest = arguments[0], arguments[1:]
    application = Application()
    if command == 'install':
        value = tyro.cli(InstallCommand, args=rest, prog='baccy service install')
        result = application.install_service(['watch', '--config', str(value.config)])
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
    print(result.model_dump_json())
    return 0


def _parse_service_command(arguments: list[str], command: str) -> None:
    tyro.cli(ServiceCommand, args=arguments, prog=f'baccy service {command}')


def _print_summary(summary: BackupSummary, verbose: bool = False) -> None:
    value = _visible_summary(summary, verbose)
    print(json.dumps(value.model_dump(mode='json'), sort_keys=True))


def _report(application: Application, summary: BackupSummary, verbose: bool) -> None:
    value = _visible_summary(summary, verbose)
    _print_summary(value, verbose=True)
    application.record_summary(value)


def _visible_summary(summary: BackupSummary, verbose: bool) -> BackupSummary:
    if verbose:
        return summary
    return summary.model_copy(
        update={
            'results': [
                result for result in summary.results if result.status != 'unchanged'
            ]
        }
    )


def _usage() -> str:
    return 'Usage: baccy {backup,watch,service} ...'


def _service_usage() -> str:
    return 'Usage: baccy service {install,uninstall,start,stop,restart,status}'
