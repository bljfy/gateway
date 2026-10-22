"""Prepare native paths before pytest starts; preserve E2E failure after diagnostics."""

import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values


def environment(root: Path) -> dict[str, str]:
    values = os.environ.copy()
    for name, value in dotenv_values(root / ".env", interpolate=False).items():
        if value is not None:
            values.setdefault(name, value)
    if not values.get("GMSSL_LIBRARY") or not values.get("GMSSL_SHA256"):
        native = json.loads((root / ".tools/gmssl/manifest.json").read_text(encoding="utf-8"))
        library, digest = native["library"], native["sha256"]
        if not isinstance(library, str) or not isinstance(digest, str):
            raise ValueError("invalid native manifest")
        values["GMSSL_LIBRARY"], values["GMSSL_SHA256"] = library, digest
    directory = str(Path(values["GMSSL_LIBRARY"]).resolve(strict=True).parent)
    variable = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
    normalized = os.path.normcase(os.path.normpath(directory))
    remaining = [
        entry
        for entry in values.get(variable, "").split(os.pathsep)
        if entry and os.path.normcase(os.path.normpath(entry.strip('"'))) != normalized
    ]
    values[variable] = os.pathsep.join([directory, *remaining])
    return values


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        values = environment(root)
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "E2E environment preparation failed; check the verified native manifest.",
            file=sys.stderr,
        )
        return 2
    command = [sys.executable, "-m", "pytest", "test/", "--tb=short", "-q"]
    result = subprocess.run(
        [*command, "-m", "e2e", "--junitxml=artifacts/e2e-junit.xml"],
        cwd=root,
        env=values,
        check=False,
    )
    if result.returncode != 0:
        print("E2E failed; running non-E2E diagnostics. The E2E failure remains final.", flush=True)
        subprocess.run(
            [*command, "-m", "not e2e", "--junitxml=artifacts/fallback-junit.xml"],
            cwd=root,
            env=values,
            check=False,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
