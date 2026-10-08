from datetime import date
from pathlib import Path

import pytest

from scripts.plan_openloop import plan_collection


def test_proposal_pairs_conversions_and_keeps_other_sources(tmp_path: Path) -> None:
    _collection(
        tmp_path,
        [
            'source/2002/09/28/2002-09-28-final.Sd2f',
            'target/2002/09/28/2002-09-28-final.wav',
            'source/2002/09/28/2002-09-28-remix.Sd2f',
            'source/2003/02/22/030222-4',
            'source/.DS_Store',
            'source/rename.sh',
        ],
    )

    proposals = plan_collection(tmp_path)

    assert len(proposals) == 3
    assert proposals[0].original_path == Path('source/2002/09/28/2002-09-28-final.Sd2f')
    assert proposals[0].title == '2002-09-28-final'
    assert proposals[0].recording_date == date(2002, 9, 28)
    assert proposals[1].title == '2002-09-28-remix'
    assert proposals[1].stage == 'unconverted-sd2f'
    assert proposals[2].stage == 'review'
    assert proposals[2].recording_date == date(2003, 2, 22)


def test_corrected_takes_keep_original_paths(tmp_path: Path) -> None:
    _collection(tmp_path, ['source/unknown/xx-05-02-1.Sd2f'])

    proposal = plan_collection(tmp_path)[0]

    assert proposal.path == Path('source/unknown/xx-05-02-1.Sd2f')
    assert proposal.recording_date == date(2003, 5, 3)
    assert proposal.title == '2003-05-03-1'
    assert 'user-approved' in proposal.date_evidence


def test_conflicting_and_incomplete_dates_need_review(tmp_path: Path) -> None:
    _collection(
        tmp_path,
        [
            'source/2007/08/30/2007-07-30 open loop.wav',
            'source/2007/02/open loop!!!.wav',
        ],
    )

    incomplete, conflicting = plan_collection(tmp_path)

    assert incomplete.recording_date is None
    assert conflicting.recording_date is None
    assert conflicting.filename_date == date(2007, 7, 30)
    assert conflicting.directory_date == date(2007, 8, 30)


def test_unpaired_target_is_reported_before_any_proposal(tmp_path: Path) -> None:
    _collection(tmp_path, ['target/2002/02/23/2002-02-23-1.wav'])

    with pytest.raises(ValueError, match='no matching source'):
        plan_collection(tmp_path)


def _collection(directory: Path, paths: list[str]) -> None:
    (directory / 'source').mkdir()
    (directory / 'target').mkdir()
    for name in paths:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # These are names-only inventory fixtures, not digital-audio fixtures.
        path.touch()
