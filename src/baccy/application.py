from pathlib import Path

from pydantic import PrivateAttr
from reccy.reccy import Reccy, ReccyStatus
from reccy.services import spec

from .models import BackupSummary

BACCY_SERVICE = spec.load(Path(__file__).with_name('service.toml'))


class BaccyStatus(ReccyStatus):
    summary: BackupSummary | None = None


class Application(Reccy):
    name = 'baccy'
    daemon_module = 'baccy'
    service_spec = BACCY_SERVICE
    status_model = BaccyStatus

    _summary: BackupSummary | None = PrivateAttr(default=None)

    def record_summary(self, summary: BackupSummary) -> None:
        self._summary = summary
        self.publish_status()

    def status_snapshot(self) -> BaccyStatus:
        return BaccyStatus(
            running=self._started,
            errors=self._errors.copy(),
            summary=self._summary,
        )
