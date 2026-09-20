from pathlib import Path

from baccy.models import PathSource, ResolvedSource
from baccy.scan import scan


def test_scan_prioritizes_recs_metadata_and_omits_symlinks(tmp_path: Path) -> None:
    source_root = tmp_path / 'session'
    source_root.mkdir()
    (source_root / 'audio.wav').write_bytes(b'audio')
    (source_root / 'recording.toml').write_text('format = "recs"\n')
    (source_root / 'session-record.jsonl').write_text('{"type":"header"}\n')
    (source_root / 'link').symlink_to(source_root / 'audio.wav')
    source = ResolvedSource(
        source=PathSource(kind='path', name='session', path=source_root),
        root=source_root,
    )

    candidates = scan(source)

    assert [c.relative_path for c in candidates] == [
        Path('recording.toml'),
        Path('session-record.jsonl'),
        Path('audio.wav'),
    ]
