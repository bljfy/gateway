"""Verify the delivery gate independently of workflow dispatch."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


@pytest.mark.parametrize("matches", [True, False])
def test_release_tag_must_match_project_version(matches: bool) -> None:
    root = Path(__file__).resolve().parents[3]
    with (root / "pyproject.toml").open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/check_release.py")],
        env={**os.environ, "RELEASE_TAG": f"v{version}" if matches else "v-invalid"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is matches
    if not matches:
        assert "does not match" in result.stderr
