from pathlib import Path

import pytest

from baccy.importer import import_recs
from baccy.models import Settings


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('project', ['../escape', '/outside', '.', '..'])
def test_import_rejects_project_paths(
    tmp_path: Path, dry_run: bool, project: str
) -> None:
    settings = Settings(backup_root=tmp_path / 'backup')

    with pytest.raises(ValueError, match='single path component'):
        import_recs(
            [], settings, copy_directories=False, project=project, dry_run=dry_run
        )

    assert not settings.backup_root.exists()
