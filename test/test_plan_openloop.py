import wave
from datetime import date
from pathlib import Path

import pytest

from scripts.plan_openloop import (
    main,
    plan_collection,
    plan_sessions,
    review_collection,
)


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


def test_corrected_discs_keep_original_paths(tmp_path: Path) -> None:
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


def test_review_hides_automatic_items_and_redundant_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _collection(
        tmp_path,
        [
            'source/2002/09/28/2002-09-28-final.Sd2f',
            'target/2002/09/28/2002-09-28-final.wav',
            'source/unknown/xx-05-02-1.Sd2f',
            'source/2003/02/22/030222-4',
            'source/unknown/show.image',
        ],
    )

    review_collection(tmp_path)

    output = capsys.readouterr().out
    path = str(tmp_path / 'source/unknown/show.image')
    assert output.startswith(path + '\n')
    assert output.count('show.image') == 1
    assert 'Recording date is incomplete or unknown.' in output
    assert '030222-4' not in output
    assert 'final' not in output
    assert 'xx-05-02' not in output
    assert 'size' not in output
    assert 'original_path' not in output
    assert 'filename_date' not in output
    assert '{' not in output


def test_review_skips_wavs_under_ten_seconds_but_keeps_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _collection(tmp_path, [])
    short = tmp_path / 'source/short.wav'
    boundary = tmp_path / 'source/boundary.wav'
    _wav(short, 9)
    _wav(boundary, 10)

    review_collection(tmp_path)

    output = capsys.readouterr().out
    assert str(short) not in output
    assert output.startswith(str(boundary) + '\n')
    assert 'Recording date is incomplete or unknown.' in output
    assert 'Duration' not in output


def test_interactive_review_collects_dates_and_defers_without_changing_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _collection(tmp_path, ['source/a.image', 'source/b.image'])
    answers = iter(['bad-date', '2003-05-03', ''])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob('*'))

    assert main([str(tmp_path), '--interactive']) == 0

    output = capsys.readouterr().out
    assert output.startswith(str(tmp_path / 'source/a.image') + '\n')
    assert 'Enter a valid recording date' in output
    assert '1. 2003-05-03' in output
    assert '2.' not in output
    assert 'Include' not in output
    assert 'not saved' in output
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob('*')) == before


def test_interactive_quit_does_not_show_later_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _collection(tmp_path, ['source/a.image', 'source/b.image'])
    monkeypatch.setattr('builtins.input', lambda _: 'q')

    review_collection(tmp_path, interactive=True)

    output = capsys.readouterr().out
    assert 'a.image' in output
    assert 'b.image' not in output


def test_review_flags_round_frame_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _collection(tmp_path, ['source/2003/05/03/2003-05-03-1.wav'])
    _wav(tmp_path / 'source/2003/05/03/2003-05-03-1.wav', 10)
    monkeypatch.setattr(wave.Wave_read, 'getnframes', lambda _: 10_000_000)

    review_collection(tmp_path)

    output = capsys.readouterr()
    assert output.out == ''
    assert '10,000,000 declared frames' in output.err
    assert 'technical investigation' in output.err


def test_dated_discs_do_not_need_user_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _collection(
        tmp_path,
        ['source/2003/05/03/2003-05-03-1.mp3', 'source/2003/05/03/030503-2'],
    )

    review_collection(tmp_path, interactive=True)

    assert capsys.readouterr().out == ''


def test_sessions_keep_numeric_disc_order_and_group_date_corrections(
    tmp_path: Path,
) -> None:
    _collection(
        tmp_path,
        [
            'source/unknown/xx-05-02-1.Sd2f',
            'source/unknown/xx-05-02-2.Sd2f',
            'source/2003/05/03/2003-05-03-3.Sd2f',
            'source/2003/05/10/2003-05-10-10.Sd2f',
            'source/2003/05/10/2003-05-10-2.Sd2f',
            'source/2003/05/10/2003-05-10-1.Sd2f',
            'source/unknown/undated.Sd2f',
        ],
    )

    sessions = plan_sessions(tmp_path)

    assert [s.recording_date for s in sessions] == [date(2003, 5, 3), date(2003, 5, 10)]
    assert [d.disc_number for d in sessions[0].discs] == [1, 2, 3]
    assert [d.disc_number for d in sessions[1].discs] == [1, 2, 10]
    assert sessions[0].discs[0].path == Path('source/unknown/xx-05-02-1.Sd2f')


def test_interrupted_review_has_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _collection(tmp_path, ['source/recording.image'])

    def interrupt(prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr('builtins.input', interrupt)

    with pytest.raises(SystemExit, match='Review cancelled'):
        main([str(tmp_path), '--interactive'])


def _wav(path: Path, seconds: int) -> None:
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48_000)
        audio.writeframes(b'\0\0' * (48_000 * seconds))


def _collection(directory: Path, paths: list[str]) -> None:
    (directory / 'source').mkdir()
    (directory / 'target').mkdir()
    for name in paths:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # These are names-only inventory fixtures, not digital-audio fixtures.
        path.touch()
