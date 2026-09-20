from pathlib import Path

import pytest
from pydantic import ValidationError

from baccy.config import default_config_path, load
from baccy.models import Settings


def test_load_reads_sources(tmp_path: Path) -> None:
    path = tmp_path / 'baccy.toml'
    path.write_text(
        'backup_root = "/backup"\n'
        'stability_seconds = 30\n'
        '[[sources]]\n'
        'kind = "path"\n'
        'name = "recordings"\n'
        'path = "/recordings"\n'
    )

    settings = load(path)

    assert settings.backup_root == Path('/backup')
    assert settings.sources[0].name == 'recordings'
    assert settings.stability_seconds == 30


def test_default_config_path_uses_application_support(tmp_path: Path) -> None:
    assert default_config_path(tmp_path) == (
        tmp_path / 'Library' / 'Application Support' / 'baccy' / 'config.toml'
    )


def test_settings_reject_duplicate_source_names() -> None:
    with pytest.raises(ValidationError, match='source names must be unique'):
        Settings.model_validate(
            {
                'backup_root': '/backup',
                'sources': [
                    {'kind': 'path', 'name': 'same', 'path': '/first'},
                    {'kind': 'path', 'name': 'same', 'path': '/second'},
                ],
            }
        )


def test_settings_allow_automatic_sources_only() -> None:
    settings = Settings(backup_root=Path('/backup'))

    assert settings.sources == []
    assert settings.discover_removable is True
