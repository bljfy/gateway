"""One-command secure demo, configuration reuse and failure cleanup."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from gateway.cli import main
from gateway.crypto import GmSSLBackend
from test.fixtures.simulator_output import response_body
from test.integration.test_topology import ports


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("quiet", [False, True])
def test_demo_reuses_identities_and_releases_ports(
    tmp_path: Path, backend: GmSSLBackend, blocked: bool, quiet: bool
) -> None:
    manifest = tmp_path / "native.json"
    manifest.write_text(
        json.dumps({"library": os.environ["GMSSL_LIBRARY"], "sha256": os.environ["GMSSL_SHA256"]}),
        encoding="utf-8",
    )
    directory = tmp_path / "demo"
    gp, sp, mp = ports()
    command = [
        sys.executable,
        "-m",
        "gateway.cli",
        "demo",
        "--directory",
        str(directory),
        "--manifest",
        str(manifest),
        "--gateway-port",
        str(gp),
        "--simulator-port",
        str(sp),
        "--metrics-port",
        str(mp),
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    if quiet:
        command.insert(3, "--quiet")

    def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command + arguments,
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            creationflags=flags,
        )

    if blocked:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", gp))
            listener.listen()
            failure = run(["--prompt", "synthetic-demo-marker"])
        assert failure.returncode == 1
        assert failure.stdout == "" and failure.stderr.endswith("command failed\n")
        assert "server_failed role=gateway" in failure.stderr
        assert "synthetic-demo-marker" not in failure.stderr
    request_id = uuid4()
    success = run(
        ["--prompt", "synthetic-demo-marker", "--model", "m", "--request-id", str(request_id)]
    )
    assert success.returncode == 0
    assert response_body(success.stdout, request_id) == "[mock:m] synthetic-demo-marker\n"
    for stage in (
        "server_started",
        "client_authenticated",
        "upstream_authenticated",
        "request_forwarded",
        "response_started",
        "request_finished result=ok",
        "server_stopped",
    ):
        if quiet:
            assert success.stderr == ""
        else:
            assert stage in success.stderr
    assert "synthetic-demo-marker" not in success.stderr
    keys = {path.name: path.read_bytes() for path in directory.glob("*.der")}
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("synthetic-demo-marker-file", encoding="utf-8")
    streamed = run(["--prompt-file", str(prompt), "--stream"])
    assert streamed.returncode == 0
    assert response_body(streamed.stdout) == "[mock:mock-model] synthetic-demo-marker-file\n"
    assert "synthetic-demo-marker" not in streamed.stderr
    assert keys == {path.name: path.read_bytes() for path in directory.glob("*.der")}
    audit = (directory / "audit.jsonl").read_text(encoding="utf-8")
    assert "synthetic-demo-marker" not in audit
    assert sum(json.loads(line)["result"] == "ok" for line in audit.splitlines()) == 2
    for port in (gp, sp, mp):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))


def test_demo_rejects_existing_incomplete_directory(tmp_path: Path) -> None:
    existing = tmp_path / "identity.der"
    existing.write_bytes(b"synthetic-identity")
    assert main(["demo", "--directory", str(tmp_path)]) == 1
    assert existing.read_bytes() == b"synthetic-identity"
