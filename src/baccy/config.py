import tomllib
from pathlib import Path

from .models import Settings


def default_config_path(home: Path | None = None) -> Path:
    root = Path.home() if home is None else home
    return root / 'Library' / 'Application Support' / 'baccy' / 'config.toml'


def load(path: Path) -> Settings:
    with path.open('rb') as file:
        return Settings.model_validate(tomllib.load(file))
