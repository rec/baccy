from pathlib import Path

from pydantic import PrivateAttr
from reccy.reccy import Reccy, ReccyStatus
from reccy.services import spec

from .models import BackupSummary
from .notifications import notify_failures

BACCY_SERVICE = spec.load(Path(__file__).with_name('service.toml'))


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
        self.publish_status()

    def status_snapshot(self) -> BaccyStatus:
        return BaccyStatus(
            running=self._started,
            errors=self._errors.copy(),
            summary=self._summary,
        )
