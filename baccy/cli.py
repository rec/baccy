import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import tomlkit
import tyro
from pydantic import BaseModel, Field
from reccy.protocol import rpc

from .application import Application, DaemonApplication
from .backup import run_backup
from .config import default_config_path, load_or_default
from .importer import import_recs
from .models import BackupSummary, FileResult, Settings
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


class DaemonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value: dict[str, object] = {
            'timestamp': datetime.fromtimestamp(record.created, UTC).strftime(
                '%Y-%m-%dT%H:%M:%SZ'
            ),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        if record.exc_info is not None:
            value['exception'] = self.formatException(record.exc_info)
        return json.dumps(value, separators=(',', ':'))


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        config, daemon, arguments = _config(arguments)
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
        return _sync(value, config, dry_run, daemon)
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
        application = DaemonApplication()
        _configure_daemon_logging()

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
                on_write=_report_write,
            )

        application.start()
        try:
            watch(
                settings,
                trigger=application.sync_requested,
                action=action,
                report=lambda summary: _report(application, summary),
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


def _sync(command: SyncCommand, config: Path, dry_run: bool, daemon: bool) -> int:
    if daemon and not dry_run:
        if command.directories:
            print('--daemon sync does not accept directories', file=sys.stderr)
            return 2
        endpoint = Application().control_endpoint
        for attempt in range(20):
            try:
                rpc.Client(endpoint, role='baccy-cli').call('sync')
                return 0
            except FileNotFoundError as error:
                if attempt == 19:
                    print(
                        f'could not request baccy daemon sync: {error}', file=sys.stderr
                    )
                    return 1
                time.sleep(0.1)
            except (BrokenPipeError, ConnectionError, OSError, TimeoutError) as error:
                print(f'could not request baccy daemon sync: {error}', file=sys.stderr)
                return 1
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


def _config(arguments: list[str]) -> tuple[Path, bool, list[str]]:
    config: Path | None = None
    daemon = False
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
        elif value == '--daemon':
            daemon = True
        else:
            values.append(value)
    if daemon:
        if config is not None:
            raise ValueError('--daemon cannot be used with --config')
        return _daemon_config(), True, values
    return config or default_config_path(), False, values


def _daemon_config() -> Path:
    path = Application().paths.metadata
    try:
        metadata = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise ValueError(f'baccy daemon metadata does not exist: {path}') from error
    except json.JSONDecodeError as error:
        raise ValueError(f'invalid baccy daemon metadata: {path}') from error
    argv = metadata.get('argv')
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        raise ValueError(f'invalid baccy daemon metadata: {path}')
    for index, value in enumerate(argv):
        if value == '--config' and index + 1 < len(argv):
            return Path(argv[index + 1])
        if value.startswith('--config='):
            return Path(value.removeprefix('--config='))
    raise ValueError(f'baccy daemon configuration is missing: {path}')


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


def _report(application: Application, summary: BackupSummary) -> None:
    summary = _visible_summary(summary, False)
    for result in summary.results:
        print(result.model_dump_json(exclude_none=True))
    application.record_summary(summary)


def _report_write(result: FileResult) -> None:
    print(result.model_dump_json(exclude_none=True))


def _configure_daemon_logging() -> None:
    formatter = DaemonLogFormatter()
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


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
        'Usage: baccy [--config PATH|--daemon] [--dry-run|-d] '
        '{backup,watch,import,sync,test,service} ...'
    )


def _service_usage() -> str:
    return 'Usage: baccy service {install,uninstall,start,stop,restart,status}'
