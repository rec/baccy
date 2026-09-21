import json
import os
import time
from pathlib import Path

import pytest

from baccy.backup import BackupLock, run_backup
from baccy.models import PathSource, Settings


class NoNetworkDiscovery:
    def discover(self) -> list[object]:
        return []


@pytest.fixture(autouse=True)
def disable_network_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('baccy.backup.NetworkDiscovery', NoNetworkDiscovery)


def _settings(
    source: Path, destination: Path, stability_seconds: float = 0
) -> Settings:
    return Settings(
        backup_root=destination,
        discover_removable=False,
        stability_seconds=stability_seconds,
        sources=[PathSource(kind='path', name='source', path=source)],
    )


def test_backup_copies_metadata_before_media_and_then_skips_it(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'audio.wav').write_bytes(b'audio')
    (source / 'recording.toml').write_text('format = "recs"\n')
    (source / 'session-record.jsonl').write_text('{"type":"header"}\n')
    destination = tmp_path / 'backup'

    first = run_backup(_settings(source, destination))
    second = run_backup(_settings(source, destination))

    assert [r.relative_path for r in first.results] == [
        Path('recording.toml'),
        Path('session-record.jsonl'),
        Path('audio.wav'),
    ]
    assert first.copied == 3
    assert second.unchanged == 3
    assert (destination / 'sources' / 'source' / 'audio.wav').read_bytes() == b'audio'


def test_backup_defers_recent_ordinary_files(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'audio.wav').write_bytes(b'audio')

    result = run_backup(_settings(source, tmp_path / 'backup', stability_seconds=3600))

    assert result.deferred == 1
    assert result.copied == 0


def test_backup_lock_is_at_backup_root(tmp_path: Path) -> None:
    with BackupLock(tmp_path) as lock:
        assert lock.path == tmp_path / '.lock'
        assert lock.path.exists()


def test_backup_records_one_deferred_event_per_deferral_cycle(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    path = source / 'audio.wav'
    path.write_bytes(b'audio')
    destination = tmp_path / 'backup'
    settings = _settings(source, destination, stability_seconds=3600)

    run_backup(settings)
    run_backup(settings)
    events = [
        json.loads(line)
        for line in (destination / 'events.jsonl').read_text().splitlines()
    ]
    assert [event['result'] for event in events] == ['deferred']

    old = time.time_ns() - 7_200_000_000_000
    os.utime(path, ns=(old, old))
    run_backup(settings)
    path.write_bytes(b'changed')
    run_backup(settings)
    events = [
        json.loads(line)
        for line in (destination / 'events.jsonl').read_text().splitlines()
    ]

    assert [event['result'] for event in events] == ['deferred', 'copied', 'deferred']


def test_backup_dry_run_does_not_write(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'recording.toml').write_text('format = "recs"\n')
    destination = tmp_path / 'backup'

    result = run_backup(_settings(source, destination), dry_run=True)

    assert result.would_copy == 1
    assert result.copied == 0
    assert result.results[0].status == 'would_copy'
    assert not destination.exists()


def test_backup_defers_jsonl_with_partial_final_line(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'session-record.jsonl').write_text('{"type":"header"}')

    result = run_backup(_settings(source, tmp_path / 'backup'))

    assert result.deferred == 1
    assert result.copied == 0


def test_backup_appends_jsonl_without_creating_dot_baccy(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    journal = source / 'session-record.jsonl'
    journal.write_text('{"type":"header"}\n')
    destination = tmp_path / 'backup'
    settings = _settings(source, destination)
    run_backup(settings)
    journal.write_text('{"type":"header"}\n{"type":"event"}\n')

    result = run_backup(settings)

    assert result.copied == 1
    assert (
        destination / 'sources' / 'source' / 'session-record.jsonl'
    ).read_text() == '{"type":"header"}\n{"type":"event"}\n'
    assert not (destination / '.baccy').exists()


def test_backup_defers_active_recs_audio_until_finished(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    journal = source / 'session-record.jsonl'
    journal.write_text('{"type":"file_started","stream_id":"mic","path":"audio.wav"}\n')
    (source / 'audio.wav').write_bytes(b'audio')
    destination = tmp_path / 'backup'
    settings = _settings(source, destination)

    active = run_backup(settings)
    journal.write_text(
        '{"type":"file_started","stream_id":"mic","path":"audio.wav"}\n'
        '{"type":"file_finished","stream_id":"mic","path":"audio.wav"}\n'
    )
    finished = run_backup(settings)

    assert active.deferred == 1
    assert finished.copied == 2
    assert (destination / 'sources' / 'source' / 'audio.wav').read_bytes() == b'audio'


def test_backup_replaces_changed_destination_without_retaining_version(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    path = source / 'recording.toml'
    path.write_text('version = 1\n')
    destination = tmp_path / 'backup'
    settings = _settings(source, destination)
    run_backup(settings)
    path.write_text('version = 2\n')

    result = run_backup(settings)

    assert result.copied == 1
    assert (
        destination / 'sources' / 'source' / 'recording.toml'
    ).read_text() == 'version = 2\n'
    assert not (destination / '.baccy').exists()


def test_backup_preserves_destination_when_source_disappears(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'recording.toml').write_text('format = "recs"\n')
    destination = tmp_path / 'backup'
    settings = _settings(source, destination)
    run_backup(settings)
    (source / 'recording.toml').unlink()
    source.rmdir()

    result = run_backup(settings)

    assert result.unavailable == 1
    assert (destination / 'sources' / 'source' / 'recording.toml').exists()


def test_backup_rejects_overlapping_source_and_destination_without_writing(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    settings = _settings(source, source / 'backup')

    with pytest.raises(ValueError, match='must not overlap'):
        run_backup(settings)

    assert not (source / 'backup').exists()
