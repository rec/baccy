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

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    settings = load(config)
    destination = settings.destinations['axto']
    assert isinstance(destination, S3Destination)
    assert destination.endpoint_url is None
    assert settings.backup_root == tmp_path / 'baccy'
    assert output['would_copy'] == 61
    paths = {result['relative_path'] for result in output['results']}
    assert 'audio/totm/2017/01/01/00-00-02' in paths
    assert 'audio/oderg in duo/2026/03/28/10-45-52' in paths
    sources = {result['source'] for result in output['results']}
    assert sources == {'oderg in duo', 'totm'}
    assert not (settings.backup_root / 'events.jsonl').exists()


def _write_results(root: Path) -> None:
    shutil.copytree(FIXTURES / 'results', root)
