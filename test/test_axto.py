import json
import shutil
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from baccy.cli import main
from baccy.config import load
from baccy.models import S3Destination

FIXTURES = Path(__file__).parent / 'fixtures' / 'axto'


def test_axto_config_dry_run_syncs_recs_results_layout(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    backup = tmp_path / 'baccy'
    _write_results(backup / 'audio')
    config = Path(__file__).parent / 'axto.toml'

    exit_code = main(['--dry-run', 'sync', '--config', str(config)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    settings = load(config)
    destination = settings.destinations['axto']
    assert isinstance(destination, S3Destination)
    assert destination.endpoint_url is None
    assert settings.backup_root == backup
    assert output['would_upload'] == 515
    scheduled = sorted(result['relative_path'] for result in output['results'])
    expected = json.loads((FIXTURES / 'transfers.json').read_text())
    assert scheduled == expected
    assert not (backup / 'events.jsonl').exists()


def _write_results(root: Path) -> None:
    shutil.copytree(FIXTURES / 'results', root)
    for journal in root.glob('**/session-record.jsonl'):
        _write_audio_stubs(journal)


def _write_audio_stubs(journal: Path) -> None:
    with journal.open() as source:
        for line in source:
            record = json.loads(line)
            if record.get('media_type') != 'audio':
                continue
            path = record.get('path')
            if isinstance(path, str):
                audio = journal.parent / path
                audio.parent.mkdir(parents=True, exist_ok=True)
                audio.touch()
