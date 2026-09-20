import json
import os
from pathlib import Path


class Catalog:
    def __init__(self, root: Path, name: str = 'catalog.jsonl') -> None:
        self.path = root / '.baccy' / name
        self._latest = self._load_latest()

    def latest(self, source: str, relative_path: Path) -> dict[str, object] | None:
        return self._latest.get((source, relative_path.as_posix()))

    def append(self, value: dict[str, object]) -> None:
        key = (str(value['source']), str(value['relative_path']))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a') as file:
            file.write(json.dumps(value, sort_keys=True) + '\n')
            file.flush()
            os.fsync(file.fileno())
        if value.get('result') == 'copied':
            self._latest[key] = value

    def _load_latest(self) -> dict[tuple[str, str], dict[str, object]]:
        if not self.path.exists():
            return {}
        latest: dict[tuple[str, str], dict[str, object]] = {}
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
                if (
                    value.get('result') == 'copied'
                    and isinstance(source, str)
                    and isinstance(relative_path, str)
                ):
                    latest[(source, relative_path)] = value
        return latest
