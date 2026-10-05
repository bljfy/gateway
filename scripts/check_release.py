"""Fail a release job when its tag does not match project metadata."""

import os
import tomllib
from pathlib import Path


def main() -> None:
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project.open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    if os.environ.get("RELEASE_TAG") != f"v{version}":
        raise SystemExit("Release tag does not match the project version")


if __name__ == "__main__":
    main()
