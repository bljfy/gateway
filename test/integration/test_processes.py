"""Three independent CLI processes, actual loopback TCP and verified native GmSSL."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.crypto import GmSSLBackend
from gateway.runtime import prepare_demo
from test.integration.test_topology import ports


@pytest.mark.asyncio
async def test_three_process_cli_roundtrip_metrics_and_shutdown(
    tmp_path: Path, backend: GmSSLBackend
) -> None:
    manifest = tmp_path / "native.json"
    manifest.write_text(
        json.dumps({"library": os.environ["GMSSL_LIBRARY"], "sha256": os.environ["GMSSL_SHA256"]}),
        encoding="utf-8",
    )
    directory = tmp_path / "process-demo"
    gp, sp, mp = ports()
    prepare_demo(directory, manifest, gateway_port=gp, simulator_port=sp, metrics_port=mp)
    processes: list[subprocess.Popen[str]] = []
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    root = Path(__file__).resolve().parents[2]
    try:
        for role in ("simulator", "gateway"):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "gateway.cli",
                    role,
                    "--config",
                    str(directory / f"{role}.json"),
                    "--stop-file",
                    str(directory / f"{role}.stop"),
                ],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                creationflags=flags,
            )
            processes.append(process)
            assert process.stdout is not None
            assert (
                await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 5)
                == f"{role} ready\n"
            )
        for size, stream in ((1024, False), (16384, False), (70000, True)):
            marker = "synthetic-process-private-marker"
            prompt = marker + "x" * (size - len(marker))
            prompt_path = directory / "prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            command = [
                sys.executable,
                "-m",
                "gateway.cli",
                "client",
                "--config",
                str(directory / "client.json"),
                "--prompt-file",
                str(prompt_path),
                "--model",
                "m",
                "--max-output-tokens",
                str(size + 64),
            ]
            if stream:
                command.append("--stream")
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                creationflags=flags,
            )
            assert result.returncode == 0 and result.stderr == ""
            assert result.stdout == f"[mock:m] {prompt}\n"
        reader, writer = await asyncio.open_connection("127.0.0.1", mp)
        writer.write(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        metrics = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        await writer.wait_closed()
        assert b"200 OK" in metrics and b"gateway_requests_completed_total 3" in metrics
        assert b"private-marker" not in metrics
    finally:
        for role in ("gateway", "simulator"):
            (directory / f"{role}.stop").write_text("stop", encoding="ascii")
        for process in reversed(processes):
            try:
                _, errors = await asyncio.wait_for(asyncio.to_thread(process.communicate), 5)
                assert process.returncode == 0 and errors == ""
            finally:
                if process.poll() is None:
                    process.kill()
                    await asyncio.to_thread(process.wait)
    audit = (directory / "audit.jsonl").read_text(encoding="utf-8")
    assert "private-marker" not in audit
    assert sum(json.loads(line)["result"] == "ok" for line in audit.splitlines()) == 3
