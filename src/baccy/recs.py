import json
from pathlib import Path


def files_being_written(directory: Path) -> list[Path]:
    root = directory.resolve()
    journal = root / 'session-record.jsonl'
    open_files: set[tuple[str, str]] = set()
    has_footer = False
    with journal.open() as source:
        for line in source:
            if not line.endswith('\n'):
                break
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f'invalid recs record in {journal}')
            record_type = record.get('type')
            if record_type == 'footer':
                has_footer = True
                continue
            if record_type not in {'file_started', 'file_finished', 'file_discarded'}:
                continue
            stream_id = record.get('stream_id')
            relative_path = record.get('path')
            if not isinstance(stream_id, str) or not isinstance(relative_path, str):
                raise ValueError(f'invalid file lifecycle record in {journal}')
            path = (root / relative_path).resolve()
            if not path.is_relative_to(root):
                raise ValueError(
                    f'file path escapes session directory: {relative_path}'
                )
            identity = stream_id, relative_path
            if record_type == 'file_started':
                open_files.add(identity)
            else:
                open_files.discard(identity)
    paths = {root / path for _, path in open_files}
    if not has_footer:
        paths.add(journal)
    return sorted(paths)
