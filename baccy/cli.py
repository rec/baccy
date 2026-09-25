import os
import sys
from pathlib import Path
from typing import Annotated

import tomlkit
import tyro
from pydantic import BaseModel, Field

from .application import Application
from .backup import run_backup
from .config import default_config_path, load_or_default
from .importer import import_recs
from .models import BackupSummary, Settings
from .network import NetworkDiscovery
from .server_test import test_destinations
from .sync import sync
from .watch import watch


class BackupCommand(BaseModel, frozen=True):
    pass


class WatchCommand(BackupCommand):
    """Run backup passes until interrupted."""


class ImportCommand(BaseModel, frozen=True):
    directories: Annotated[list[Path], tyro.conf.Positional]
    copy_directories: Annotated[bool, tyro.conf.arg(name='copy')] = False
    project: str | None = None


class SyncCommand(BaseModel, frozen=True):
    directories: Annotated[list[Path], tyro.conf.Positional] = Field(
        default_factory=list
    )


class TestCommand(BaseModel, frozen=True):
    """Test access to configured upload destinations."""


class InstallCommand(BaseModel, frozen=True):
    """Install the per-user baccy LaunchAgent."""


class ServiceCommand(BaseModel, frozen=True):
    """Manage the per-user baccy LaunchAgent."""


class SummaryReporter:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.previous: BackupSummary | None = None

    def print(self, summary: BackupSummary) -> None:
        value = _visible_summary(summary, self.verbose)
        if value != self.previous:
            _print_summary(value, verbose=True)
            self.previous = value


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        config, arguments = _config(arguments)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    dry_run, arguments = _dry_run(arguments)
    if not arguments or arguments[0] in {'-h', '--help'}:
        print(_usage())
        return 0
    command, rest = arguments[0], arguments[1:]
    if command == 'backup':
        value = tyro.cli(BackupCommand, args=rest, prog='baccy backup')
        return _backup(value, config, dry_run)
    if command == 'watch':
        value = tyro.cli(WatchCommand, args=rest, prog='baccy watch')
        return _watch(value, config, dry_run)
    if command == 'import':
        value = tyro.cli(ImportCommand, args=rest, prog='baccy import')
        return _import(value, config, dry_run)
    if command == 'sync':
        value = tyro.cli(SyncCommand, args=rest, prog='baccy sync')
        return _sync(value, config, dry_run)
    if command == 'test':
        value = tyro.cli(TestCommand, args=rest, prog='baccy test')
        return _test(value, config, dry_run)
    if command == 'service':
        return _service(rest, config, dry_run)
    print(f'unknown command: {command}', file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


def _backup(command: BackupCommand, config: Path, dry_run: bool) -> int:
    settings = load_or_default(config)
    summary = run_backup(settings, dry_run=dry_run)
    _print_summary(summary, settings.verbose)
    return 1 if summary.failed or summary.unavailable else 0


def _watch(command: WatchCommand, config: Path, dry_run: bool) -> int:
    settings = load_or_default(config)
    network = NetworkDiscovery(verbose=settings.verbose)
    reporter = SummaryReporter(settings.verbose)

    if os.environ.get('BACCY_DAEMON') == '1':
        application = Application()

        def action(value: Settings) -> BackupSummary:
            return run_backup(
                value,
                dry_run=dry_run,
                network=network,
                recognize=(
                    application.record_recognized_sources if value.verbose else None
                ),
                recognize_machines=(
                    application.record_recognized_machines if value.verbose else None
                ),
            )

        application.start()
        try:
            watch(
                settings,
                action=action,
                report=lambda summary: _report(application, reporter, summary),
            )
        finally:
            application.close()
    else:

        def action(value: Settings) -> BackupSummary:
            return run_backup(value, dry_run=dry_run, network=network)

        watch(
            settings,
            action=action,
            report=reporter.print,
        )
    return 0


def _import(command: ImportCommand, config: Path, dry_run: bool) -> int:
    settings = load_or_default(config)
    summary = import_recs(
        command.directories,
        settings,
        command.copy_directories,
        command.project,
        dry_run,
    )
    _print_summary(summary, settings.verbose)
    return 1 if summary.failed else 0


def _sync(command: SyncCommand, config: Path, dry_run: bool) -> int:
    settings = load_or_default(config)
    summary = sync(command.directories, settings, dry_run)
    _print_summary(summary, settings.verbose)
    return 1 if summary.failed else 0


def _test(command: TestCommand, config: Path, dry_run: bool) -> int:
    failures = test_destinations(load_or_default(config))
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        return -1
    print('ok')
    return 0


def _service(arguments: list[str], config: Path, dry_run: bool) -> int:
    if not arguments or arguments[0] in {'-h', '--help'}:
        print(_service_usage())
        return 0
    command, rest = arguments[0], arguments[1:]
    if dry_run:
        print(tomlkit.dumps({'command': command, 'dry_run': True}), end='')
        return 0
    application = Application()
    if command == 'install':
        tyro.cli(InstallCommand, args=rest, prog='baccy service install')
        result = application.install_service(['watch', '--config', str(config)])
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
    sys.stdout.write(tomlkit.dumps(result.model_dump(mode='json', exclude_none=True)))
    return 0


def _parse_service_command(arguments: list[str], command: str) -> None:
    tyro.cli(ServiceCommand, args=arguments, prog=f'baccy service {command}')


def _dry_run(arguments: list[str]) -> tuple[bool, list[str]]:
    flags = {'-d', '--dry-run'}
    return any(argument in flags for argument in arguments), [
        argument for argument in arguments if argument not in flags
    ]


def _config(arguments: list[str]) -> tuple[Path, list[str]]:
    config = default_config_path()
    values: list[str] = []
    iterator = iter(arguments)
    for value in iterator:
        if value == '--config':
            try:
                config = Path(next(iterator))
            except StopIteration as error:
                raise ValueError('--config requires a path') from error
        elif value.startswith('--config='):
            config = Path(value.removeprefix('--config='))
        else:
            values.append(value)
    return config, values


def _print_summary(summary: BackupSummary, verbose: bool = False) -> None:
    value = _visible_summary(summary, verbose)
    paths = [
        (
            f'{result.destination}/{result.relative_path.as_posix()}'
            if result.destination is not None
            else result.relative_path.as_posix()
        )
        for result in value.results
        if result.relative_path is not None and result.status != 'deferred'
    ]
    print('\n'.join(paths) if paths else '(no files)')


def _report(
    application: Application, reporter: SummaryReporter, summary: BackupSummary
) -> None:
    reporter.print(summary)
    application.record_summary(_visible_summary(summary, reporter.verbose))


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
    return (
        'Usage: baccy [--config PATH] [--dry-run|-d] '
        '{backup,watch,import,sync,test,service} ...'
    )


def _service_usage() -> str:
    return 'Usage: baccy service {install,uninstall,start,stop,restart,status}'
