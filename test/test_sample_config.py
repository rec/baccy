from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from baccy.models import Settings

if TYPE_CHECKING:
    from reccy.pytest_plugin import SampleConfigRegression

pytest_plugins = ['reccy.pytest_plugin']


def test_sample_config(
    sample_config_regression: 'SampleConfigRegression', monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, 'home', staticmethod(lambda: Path('/home/sample')))
    sample_config_regression(Settings)
