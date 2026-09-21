import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pydantic import PrivateAttr
from reccy.reccy import Reccy, ReccyStatus
from reccy.services import models, spec

from .models import BackupSummary, RecognizedSource
from .notifications import notify, notify_failures

BACCY_SERVICE = spec.load(Path(__file__).with_name('service.toml'))
_LOGGER = logging.getLogger(__name__)


class BaccyStatus(ReccyStatus):
    summary: BackupSummary | None = None


class Application(Reccy):
    name = 'baccy'
    daemon_module = 'baccy'
    service_spec = BACCY_SERVICE
    status_model = BaccyStatus

    _summary: BackupSummary | None = PrivateAttr(default=None)
    _notified_failures: set[tuple[str, str | None, str | None]] = PrivateAttr(
        default_factory=set
    )
    _recognized_sources: dict[str, RecognizedSource] = PrivateAttr(default_factory=dict)
    _pending_completions: set[str] = PrivateAttr(default_factory=set)

    def service_metadata(
        self, daemon_argv: list[str], executable: Path | None = None
    ) -> models.DaemonMetadata:
        return (
            super()
            .service_metadata(daemon_argv)
            .model_copy(update={'executable': executable})
        )

    def install_service(self, daemon_argv: list[str]) -> models.StatusResult:
        executable = _install_service_release(self.paths.home)
        controller = self.service_controller()
        if controller.status().running:
            controller.stop()
        return controller.install(self.service_metadata(daemon_argv, executable))

    def record_recognized_sources(self, sources: list[RecognizedSource]) -> None:
        recognized = {source.source: source for source in sources}
        for source in recognized.values():
            if source.source not in self._recognized_sources:
                notify(f'Recognized {source.kind} {source.label}; starting backup.')
                _LOGGER.info(
                    'recognized %s %s; starting backup', source.kind, source.label
                )
                self._pending_completions.add(source.source)
        self._recognized_sources = recognized

    def record_summary(self, summary: BackupSummary) -> None:
        self._summary = summary
        failures = [result for result in summary.results if result.status == 'failed']
        fingerprints = {
            (
                result.source,
                result.relative_path.as_posix() if result.relative_path else None,
                result.detail,
            )
            for result in failures
        }
        if new_failures := fingerprints - self._notified_failures:
            notify_failures(
                [
                    result
                    for result in failures
                    if (
                        result.source,
                        result.relative_path.as_posix()
                        if result.relative_path
                        else None,
                        result.detail,
                    )
                    in new_failures
                ]
            )
        self._notified_failures = fingerprints
        for source in sorted(self._pending_completions):
            if (recognized := self._recognized_sources.get(source)) is not None:
                notify(f'Backup complete for {recognized.kind} {recognized.label}.')
                _LOGGER.info(
                    'backup complete for %s %s', recognized.kind, recognized.label
                )
        self._pending_completions.difference_update(self._recognized_sources)
        self.publish_status()

    def status_snapshot(self) -> BaccyStatus:
        return BaccyStatus(
            running=self._started,
            errors=self._errors.copy(),
            summary=self._summary,
        )


def _install_service_release(home: Path) -> Path:
    release_root = home / 'Library' / 'Application Support' / 'baccy' / 'releases'
    release = release_root / str(time.time_ns())
    environment = release / 'venv'
    executable = environment / 'bin' / 'python'
    project_root = Path(__file__).parents[2]
    reccy_root = project_root.parent / 'reccy'
    release_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary_directory:
        wheels = Path(temporary_directory)
        subprocess.run(
            ['uv', 'build', '--wheel', '--out-dir', str(wheels), str(reccy_root)],
            check=True,
        )
        subprocess.run(
            ['uv', 'build', '--wheel', '--out-dir', str(wheels), str(project_root)],
            check=True,
        )
        subprocess.run(
            [
                'uv',
                'venv',
                '--no-project',
                '--python',
                sys.executable,
                str(environment),
            ],
            check=True,
        )
        subprocess.run(
            [
                'uv',
                'pip',
                'install',
                '--python',
                str(executable),
                '--no-sources',
                *(str(path) for path in wheels.glob('*.whl')),
            ],
            check=True,
        )
    return executable
