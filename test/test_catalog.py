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
