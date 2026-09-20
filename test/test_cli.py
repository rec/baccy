import json
from pathlib import Path

from pytest import CaptureFixture

from baccy.cli import main


def test_backup_command_runs_one_pass(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'recording.toml').write_text('format = "recs"\n')
    config = tmp_path / 'baccy.toml'
    config.write_text(
        f'backup_root = "{tmp_path / "backup"}"\n'
        'stability_seconds = 0\n'
        '[[sources]]\n'
        'kind = "path"\n'
        'name = "source"\n'
        f'path = "{source}"\n'
    )

    exit_code = main(['backup', '--config', str(config)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output['copied'] == 1
