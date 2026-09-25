import json
import shutil
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from baccy.cli import main
from baccy.config import load
from baccy.models import S3Destination

FIXTURES = Path(__file__).parent / 'fixtures' / 'axto'


def test_axto_config_dry_run_imports_recs_results_layout(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    results = tmp_path / 'results'
    _write_results(results)
    config = Path(__file__).parent / 'axto.toml'

    exit_code = main(
        [
            '--dry-run',
            'import',
            str(results),
            '--config',
            str(config),
        ]
    )

    paths = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    settings = load(config)
    destination = settings.destinations['axto']
    assert isinstance(destination, S3Destination)
    assert destination.endpoint_url is None
    assert settings.backup_root == tmp_path / 'baccy'
    assert paths == (FIXTURES / 'imports.txt').read_text().splitlines()
    assert not (settings.backup_root / 'events.jsonl').exists()


def test_axto_config_dry_run_syncs_all_expected_transfers(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    backup = tmp_path / 'baccy'
    _write_projects()
    _write_results(backup / 'audio')
    config = Path(__file__).parent / 'axto.toml'

    exit_code = main(['--dry-run', 'sync', '--config', str(config)])

    scheduled = sorted(capsys.readouterr().out.splitlines())
    assert exit_code == 0
    assert len(scheduled) > 500
    assert all(not path.startswith('audio/') for path in scheduled)
    assert all(
        path.startswith(('axto:', 'axto-private:', 'TODO:/TODO:')) for path in scheduled
    )
    assert (
        'axto-private:totm/2017/01/01/01-39-50/audio/1 + 20170101-013950.flac'
    ) in scheduled
    assert 'axto:totm/20250906-180118.mp3' in scheduled
    assert 'TODO:/TODO:totm/index.html' in scheduled
    assert scheduled == (FIXTURES / 'transfers.txt').read_text().splitlines()
    assert not (backup / 'events.jsonl').exists()


def _write_results(root: Path) -> None:
    shutil.copytree(FIXTURES / 'results', root)
    for journal in root.glob('**/session-record.jsonl'):
        _write_audio_stubs(journal)


def _write_audio_stubs(journal: Path) -> None:
    with journal.open() as source:
        for line in source:
            record = json.loads(line)
            stream_id = record.get('stream_id')
            if record.get('media_type') != 'audio' and (
                not isinstance(stream_id, str) or not stream_id.startswith('audio:')
            ):
                continue
            if isinstance(path := record.get('path'), str):
                audio = journal.parent / path
                audio.parent.mkdir(parents=True, exist_ok=True)
                audio.touch()


def _write_projects() -> None:
    for name in ['oderg in duo', 'totm']:
        path = Path.home() / '.config' / 'recs' / 'projects' / f'{name}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'name': name, 'templates': {}}))
