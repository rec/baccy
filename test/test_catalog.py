import logging
from pathlib import Path

import pytest

from baccy.catalog import Catalog


def test_catalog_rejects_corrupt_record_in_middle(tmp_path: Path) -> None:
    (tmp_path / 'events.jsonl').write_text(
        '{"source":"one","relative_path":"one","result":"copied"}\n'
        'not json\n'
        '{"source":"two","relative_path":"two","result":"copied"}\n'
    )

    with pytest.raises(ValueError, match='events.jsonl:2'):
        Catalog(tmp_path)


def test_catalog_warns_about_incomplete_final_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / 'events.jsonl').write_text(
        '{"source":"one","relative_path":"one","result":"copied"}\n{"source":"two"'
    )
    caplog.set_level(logging.WARNING, logger='baccy.catalog')

    catalog = Catalog(tmp_path)

    assert catalog.latest('one', Path('one')) is not None
    assert catalog.latest('two', Path('two')) is None
    assert 'incomplete final catalog line' in caplog.text


def test_catalog_compacts_and_restores_current_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('baccy.catalog._COMPACT_AFTER', 2)
    catalog = Catalog(tmp_path)
    catalog.append(
        {
            'source': 'one',
            'relative_path': 'a',
            'result': 'uploaded',
            'destination': 's3:bucket',
            'target': 'a',
        }
    )
    catalog.append_deferred('one', Path('b'), 'waiting')

    assert (tmp_path / 'catalog.json').exists()
    assert (tmp_path / 'events.previous.jsonl').exists()
    assert (tmp_path / 'events.jsonl').read_text() == ''

    restored = Catalog(tmp_path)
    assert restored.latest('one', Path('a')) is not None
    assert restored.latest_target('s3:bucket', 'a') is not None
    restored.append_deferred('one', Path('b'), 'waiting')
    assert (tmp_path / 'events.jsonl').read_text() == ''


def test_catalog_compacts_existing_log_without_a_new_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('baccy.catalog._COMPACT_AFTER', 1)
    (tmp_path / 'events.jsonl').write_text(
        '{"source":"one","relative_path":"a","result":"copied"}\n'
    )

    catalog = Catalog(tmp_path)
    catalog.compact_if_needed()

    assert catalog.latest('one', Path('a')) is not None
    assert (tmp_path / 'events.jsonl').read_text() == ''


def test_catalog_recovers_if_rotation_fails_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('baccy.catalog._COMPACT_AFTER', 1)
    real_replace = __import__('os').replace

    def replace(source: Path, destination: Path) -> None:
        if destination.name == 'events.previous.jsonl':
            raise OSError('rotation failed')
        real_replace(source, destination)

    monkeypatch.setattr('baccy.catalog.os.replace', replace)

    with pytest.raises(OSError, match='rotation failed'):
        Catalog(tmp_path).append(
            {
                'source': 'one',
                'relative_path': 'a',
                'result': 'copied',
            }
        )

    assert Catalog(tmp_path).latest('one', Path('a')) is not None
