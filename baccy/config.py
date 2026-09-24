import tomllib
from pathlib import Path

from .models import Settings


def default_config_path(home: Path | None = None) -> Path:
    root = Path.home() if home is None else home
    return root / 'Library' / 'Application Support' / 'baccy' / 'config.toml'


def default_backup_root(home: Path | None = None) -> Path:
    root = Path.home() if home is None else home
    return root / 'baccy'


def default_settings(home: Path | None = None) -> Settings:
    return Settings(backup_root=default_backup_root(home))


def load(path: Path) -> Settings:
    with path.open('rb') as file:
        return Settings.model_validate(tomllib.load(file))


def load_or_default(path: Path, home: Path | None = None) -> Settings:
    if path != default_config_path(home):
        return load(path)
    try:
        return load(path)
    except FileNotFoundError:
        return default_settings(home)
