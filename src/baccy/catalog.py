import json
import os
import time
from pathlib import Path


class Catalog:
    def __init__(self, root: Path) -> None:
        self.path = root / 'events.jsonl'
        self._latest, self._deferred = self._load_state()

    def latest(
        self, source: str, relative_path: Path, operation: str = 'backup'
    ) -> dict[str, object] | None:
        return self._latest.get((operation, source, relative_path.as_posix()))

    def append(self, value: dict[str, object]) -> None:
        event = value | {'recorded_at_ns': time.time_ns()}
        operation = str(event.get('operation', 'backup'))
        key = (operation, str(event['source']), str(event['relative_path']))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a') as file:
            file.write(json.dumps(event, sort_keys=True) + '\n')
            file.flush()
            os.fsync(file.fileno())
        if event.get('result') in {'copied', 'uploaded', 'unchanged'}:
            self._latest[key] = event
            self._deferred.discard(key)
        elif event.get('result') == 'deferred':
            self._deferred.add(key)

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
    ]:
        if not self.path.exists():
            return {}, set()
        latest: dict[tuple[str, str, str], dict[str, object]] = {}
        deferred: set[tuple[str, str, str]] = set()
        with self.path.open() as file:
            for line in file:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, dict):
                    continue
                source = value.get('source')
                relative_path = value.get('relative_path')
                if isinstance(source, str) and isinstance(relative_path, str):
                    operation = str(value.get('operation', 'backup'))
                    key = (operation, source, relative_path)
                    if value.get('result') in {'copied', 'uploaded', 'unchanged'}:
                        latest[key] = value
                        deferred.discard(key)
                    elif value.get('result') == 'deferred':
                        deferred.add(key)
        return latest, deferred
