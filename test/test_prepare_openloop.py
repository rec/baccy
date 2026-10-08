import json
import shutil
import wave
from pathlib import Path

import pytest

from baccy.importer import import_recs
from baccy.models import Settings
from baccy.upload_plan import planned_source_uploads
from scripts.prepare_openloop import main, prepare_sessions, write_sessions


def test_preview_is_read_only_and_groups_discs_in_numeric_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    collection, decisions = _collection(tmp_path, [10, 2, 1])
    destination = tmp_path / 'prepared'

    assert main([str(collection), str(destination), '--decisions', str(decisions)]) == 0

    output = capsys.readouterr().out
    assert output.index('-1.wav') < output.index('-2.wav') < output.index('-10.wav')
    assert 'Preview only' in output
    assert not destination.exists()


def test_prepared_journal_is_accepted_by_existing_import_and_upload_planning(
    tmp_path: Path,
) -> None:
    collection, decisions = _collection(tmp_path, [2, 1])
    destination = tmp_path / 'prepared'
    sessions = prepare_sessions(collection, destination, decisions)

    write_sessions(sessions)

    journal = sessions[0].destination / 'session-record.jsonl'
    records = [json.loads(r) for r in journal.read_text().splitlines()]
    metadata = records[0]['metadata']
    assert metadata['actual_start_time_known'] is False
    assert metadata['gaps_between_discs_known'] is False
    assert [s['disc_number'] for s in metadata['sources']] == [1, 2]
    assert records[0]['started_at'] == '2003-05-03T00:00:00Z'
    assert records[-1]['type'] == 'footer'
    settings = Settings.model_validate(
        {
            'backup_root': tmp_path / 'backup',
            'uploads': [
                {
                    'name': 'source',
                    'match': 'True',
                    'encoding': {'format': 'source'},
                    'destination': 's3:private',
                }
            ],
        }
    )
    summary = import_recs([destination], settings, True, None, dry_run=True)
    assert summary.would_copy == 1
    # Treat the prepared root as an offline backup layout for planning only.
    offline = tmp_path / 'offline'
    offline.mkdir()
    (offline / 'audio').symlink_to(destination, target_is_directory=True)
    plans = planned_source_uploads(settings.model_copy(update={'backup_root': offline}))
    assert len(plans) == 2
    assert [p.segment.frame_count for p in plans] == [480_000, 480_000]
    assert all(p.segment.sample_rate == 48_000 for p in plans)
    assert all(
        (collection / p).exists()
        for p in [
            'target/2003/05/03/2003-05-03-1.wav',
            'target/2003/05/03/2003-05-03-2.wav',
        ]
    )
    with pytest.raises(FileExistsError, match='already exists'):
        prepare_sessions(collection, destination, decisions)


def test_truncated_wav_blocks_entire_batch_before_any_output_is_written(
    tmp_path: Path,
) -> None:
    collection, decisions = _collection(tmp_path, [1, 2])
    path = collection / 'target/2003/05/03/2003-05-03-2.wav'
    with path.open('r+b') as audio:
        audio.truncate(path.stat().st_size - 2)
    destination = tmp_path / 'prepared'

    with pytest.raises(ValueError, match='truncated WAV'):
        prepare_sessions(collection, destination, decisions)

    assert not destination.exists()


def test_short_discs_are_skipped_and_deferred_or_unconverted_files_are_not_copied(
    tmp_path: Path,
) -> None:
    collection, decisions = _collection(tmp_path, [1, 2])
    _wav(collection / 'target/2003/05/03/2003-05-03-2.wav', 9)
    (collection / 'source/2003/05/03/2003-05-03-3.Sd2f').touch()
    decisions.write_text('{"dates":{"target/2003/05/03/2003-05-03-1.wav":null}}')

    with pytest.raises(ValueError, match='no eligible converted WAVs'):
        prepare_sessions(collection, tmp_path / 'prepared', decisions)


def test_copy_failure_retains_staging_but_never_publishes_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    collection, decisions = _collection(tmp_path, [1])
    sessions = prepare_sessions(collection, tmp_path / 'prepared', decisions)

    def fail(source: Path, destination: Path) -> None:
        event = json.loads(capsys.readouterr().err)
        assert next(iter(event)) == 'timestamp'
        assert event['status'] == 'writing'
        raise OSError('disk full')

    monkeypatch.setattr(shutil, 'copy2', fail)
    with pytest.raises(OSError, match='disk full'):
        write_sessions(sessions)

    assert not sessions[0].destination.exists()
    assert list(sessions[0].destination.parent.glob('.openLoop-preparing-*'))
    assert not list((tmp_path / 'prepared').rglob('session-record.jsonl'))


def test_changed_source_cannot_be_written_from_stale_preview(tmp_path: Path) -> None:
    collection, decisions = _collection(tmp_path, [1])
    sessions = prepare_sessions(collection, tmp_path / 'prepared', decisions)
    _wav(sessions[0].discs[0].source, 11)

    with pytest.raises(ValueError, match='source changed'):
        write_sessions(sessions)

    assert not (tmp_path / 'prepared').exists()


def _collection(tmp_path: Path, discs: list[int]) -> tuple[Path, Path]:
    collection = tmp_path / 'collection'
    for number in discs:
        name = f'2003-05-03-{number}'
        source = collection / 'source/2003/05/03' / f'{name}.Sd2f'
        target = collection / 'target/2003/05/03' / f'{name}.wav'
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.touch()
        _wav(target, 10)
    decisions = tmp_path / 'decisions.json'
    decisions.write_text('{"dates":{}}')
    return collection, decisions


def _wav(path: Path, seconds: int) -> None:
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(48_000)
        audio.writeframes(b'\0' * (48_000 * seconds * 4))
