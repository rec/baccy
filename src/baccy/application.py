from pathlib import Path

from reccy.reccy import Reccy, ReccyStatus
from reccy.services import spec

BACCY_SERVICE = spec.load(Path(__file__).with_name('service.toml'))


class Application(Reccy):
    name = 'baccy'
    daemon_module = 'baccy'
    service_spec = BACCY_SERVICE
    status_model = ReccyStatus
