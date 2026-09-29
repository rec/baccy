import json
import logging
import os
import tempfile
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
_COMPACT_AFTER = 10_000


class Catalog:
    def __init__(self, root: Path) -> None:
        self.path = root / 'events.jsonl'
        self.snapshot_path = root / 'catalog.json'
        self.previous_path = root / 'events.previous.jsonl'
        self._event_count = 0
        self._latest, self._deferred, self._targets = self._load_state()

    def latest(
        self, source: str, relative_path: Path, operation: str = 'backup'
    ) -> dict[str, object] | None:
        return self._latest.get((operation, source, relative_path.as_posix()))

    def latest_target(self, destination: str, target: str) -> dict[str, object] | None:
        return self._targets.get((destination, target))

    def append(self, value: dict[str, object]) -> None:
        event = value | {'recorded_at_ns': time.time_ns()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a') as file:
            file.write(json.dumps(event, sort_keys=True) + '\n')
            file.flush()
            os.fsync(file.fileno())
        self._apply(event)
        self._event_count += 1
        self.compact_if_needed()

    def compact_if_needed(self) -> None:
        if self._event_count >= _COMPACT_AFTER:
            self._compact()

    def _apply(self, event: dict[str, object]) -> None:
        source = event.get('source')
        relative_path = event.get('relative_path')
        if not isinstance(source, str) or not isinstance(relative_path, str):
            return
        key = (str(event.get('operation', 'backup')), source, relative_path)
        if event.get('result') in {'copied', 'uploaded', 'unchanged'}:
            self._latest[key] = event
            self._deferred.discard(key)
            if isinstance(event.get('destination'), str) and isinstance(
                event.get('target'), str
            ):
                self._targets[(str(event['destination']), str(event['target']))] = event
        elif event.get('result') == 'deferred':
            self._deferred.add(key)

    def _compact(self) -> None:
        snapshot = {
            'latest': list(self._latest.values()),
            'deferred': [list(key) for key in sorted(self._deferred)],
            'targets': list(self._targets.values()),
        }
        with tempfile.NamedTemporaryFile(
            mode='w', dir=self.path.parent, prefix='catalog-', delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(snapshot, file, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.snapshot_path)
        os.replace(self.path, self.previous_path)
        self.path.touch()
        self._event_count = 0

    def append_deferred(
        self, source: str, relative_path: Path, detail: str | None
    ) -> None:
        self._append_deferred('backup', source, relative_path, detail)

    def append_upload_deferred(
        self, source: str, relative_path: Path, detail: str | None
    ) -> None:
        self._append_deferred('upload', source, relative_path, detail)

    def _append_deferred(
        self, operation: str, source: str, relative_path: Path, detail: str | None
    ) -> None:
        key = (operation, source, relative_path.as_posix())
        if key not in self._deferred:
            self.append(
                {
                    'operation': operation,
                    'source': source,
                    'relative_path': relative_path.as_posix(),
                    'result': 'deferred',
                    'detail': detail,
                }
            )

    def _load_state(
        self,
    ) -> tuple[
        dict[tuple[str, str, str], dict[str, object]],
        set[tuple[str, str, str]],
        dict[tuple[str, str], dict[str, object]],
    ]:
        latest: dict[tuple[str, str, str], dict[str, object]] = {}
        deferred: set[tuple[str, str, str]] = set()
        targets: dict[tuple[str, str], dict[str, object]] = {}
        if self.snapshot_path.exists():
            snapshot = json.loads(self.snapshot_path.read_text())
            for value in snapshot['latest']:
                key = (
                    str(value.get('operation', 'backup')),
                    str(value['source']),
                    str(value['relative_path']),
                )
                latest[key] = value
            deferred = {tuple(key) for key in snapshot['deferred']}
            for value in snapshot['targets']:
                targets[(str(value['destination']), str(value['target']))] = value
        if not self.path.exists():
            return latest, deferred, targets
        self._latest, self._deferred, self._targets = latest, deferred, targets
        with self.path.open() as file:
            line = file.readline()
            line_number = 0
            while line:
                line_number += 1
                self._event_count += 1
                following = file.readline()
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    if not following and not line.endswith('\n'):
                        _LOGGER.warning(
                            'ignoring incomplete final catalog line %s:%d',
                            self.path,
                            line_number,
                        )
                        break
                    raise ValueError(
                        f'invalid catalog record at {self.path}:{line_number}'
                    ) from error
                if not isinstance(value, dict):
                    raise ValueError(
                        f'invalid catalog record at {self.path}:{line_number}'
                    )
                self._apply(value)
                line = following
        return latest, deferred, targets
