"""Exercise the Linux bootstrap version gate without downloads or installations."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("version", "expected_code"),
    [
        ("uv 0.8.22", 0),
        ("uv 0.8.22 (ade2bdb 2025-09-23)", 0),
        ("uv 0.8.2", 1),
        ("uv 0.8.220", 1),
        ("other 0.8.22", 1),
    ],
)
def test_linux_bootstrap_version_gate(tmp_path: Path, version: str, expected_code: int) -> None:
    bash = os.environ.get("GATEWAY_TEST_BASH") or shutil.which("bash")
    if bash is None or (os.name == "nt" and "GATEWAY_TEST_BASH" not in os.environ):
        pytest.skip("Requires Linux bash or an explicitly selected Git Bash on Windows")
    root = Path(__file__).resolve().parents[3]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(root / "scripts/bootstrap.sh", scripts / "bootstrap.sh")
    (tmp_path / ".uv-version").write_text("0.8.22\n", encoding="utf-8")
    (tmp_path / ".python-version").write_text("3.12.11\n", encoding="utf-8")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    uname = binaries / "uname"
    uname.write_text(
        '#!/usr/bin/env bash\nif [[ "$1" == -s ]]; then echo Linux; else echo x86_64; fi\n',
        encoding="utf-8",
    )
    uname.chmod(0o755)
    uv = tmp_path / ".tools/uv/uv"
    uv.parent.mkdir(parents=True)
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == --version ]]; then printf "%s\\n" "$TEST_UV_VERSION"; '
        'else printf "%s\\n" "$*" >> uv-calls.txt; fi\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    result = subprocess.run(
        [bash, "-c", 'export PATH="$PWD/bin:$PATH"; bash scripts/bootstrap.sh --skip-checks'],
        cwd=tmp_path,
        env={**os.environ, "TEST_UV_VERSION": version},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected_code, result.stdout + result.stderr
    calls = tmp_path / "uv-calls.txt"
    if expected_code == 0:
        assert calls.read_text().splitlines() == [
            "python install --no-bin 3.12.11",
            "sync --locked --dev",
        ]
        assert "Environment ready" in result.stdout
    else:
        assert not calls.exists()
        assert "Local uv version does not match" in result.stderr
