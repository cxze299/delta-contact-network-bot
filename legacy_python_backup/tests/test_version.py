import tomllib
from pathlib import Path

import contactbot


def test_runtime_version_matches_project_metadata():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text("utf-8"))
    assert contactbot.__version__ == project["project"]["version"]
