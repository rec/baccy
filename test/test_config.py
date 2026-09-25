from pathlib import Path

import pytest
from pydantic import ValidationError

from baccy.config import (
    default_backup_root,
    default_config_path,
    load,
    load_or_default,
)
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


def test_load_reads_upload_rules(tmp_path: Path) -> None:
    path = tmp_path / 'baccy.toml'
    path.write_text(
        'backup_root = "/backup"\n'
        '[[uploads]]\n'
        'name = "main"\n'
        'match = "main and duration > 90"\n'
        'encoding = { format = "mp3", bitrate_kbps = 128 }\n'
        'destination = "ssh:user@example.org:/srv/recs"\n'
    )

    settings = load(path)

    rule = settings.uploads[0]
    assert rule.match == 'main and duration > 90'
    assert rule.encoding.format == 'mp3'
    assert rule.destination == 'ssh:user@example.org:/srv/recs'


def test_settings_rejects_project_upload_tables(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match='projects'):
        Settings.model_validate({'backup_root': tmp_path / 'backup', 'projects': {}})


def test_default_config_path_uses_application_support(tmp_path: Path) -> None:
    assert default_config_path(tmp_path) == (
        tmp_path / 'Library' / 'Application Support' / 'baccy' / 'config.toml'
    )


def test_default_backup_root_uses_main_drive_home(tmp_path: Path) -> None:
    assert default_backup_root(tmp_path) == tmp_path / 'baccy'


def test_load_or_default_uses_defaults_when_standard_file_is_missing(
    tmp_path: Path,
) -> None:
    settings = load_or_default(default_config_path(tmp_path), tmp_path)

    assert settings.backup_root == tmp_path / 'baccy'
    assert settings.sources == []
    assert settings.discover_removable is True
    assert settings.verbose is True


def test_load_or_default_rejects_missing_explicit_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_or_default(tmp_path / 'missing.toml', tmp_path)


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
