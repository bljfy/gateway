"""Process restarts use persisted recovery credentials; pool reuse is observable."""

import asyncio
import json
import os
import subprocess
import sys

import pytest

from gateway.contracts import GatewayError, PeerRole
from gateway.runtime import RuntimeStatus, load_runtime, prepare_demo, run_server
from test.fixtures.mock_provider import MockProvider
from test.integration.test_topology import ports
from test.integration.test_webui import demo_directory as demo_directory
from test.integration.test_webui import uis as uis


@pytest.mark.asyncio
async def test_client_process_restart_recovers_and_upstream_reuses(tmp_path, backend):
    manifest = tmp_path / "native.json"
    manifest.write_text(
        json.dumps(dict(library=os.environ["GMSSL_LIBRARY"], sha256=os.environ["GMSSL_SHA256"]))
    )
    directory = tmp_path / "demo"
    gp, ap, mp = ports()
    prepare_demo(directory, manifest, gateway_port=gp, adapter_port=ap, metrics_port=mp)
    adapter = load_runtime(directory / "adapter.json", PeerRole.ADAPTER)
    gateway = load_runtime(directory / "gateway.json", PeerRole.GATEWAY)
    statuses = [RuntimeStatus(), RuntimeStatus()]
    stops = [asyncio.Event(), asyncio.Event()]
    ready = [asyncio.Event(), asyncio.Event()]
    tasks = [
        asyncio.create_task(
            run_server(
                runtime,
                stop=stop,
                ready=signal.set,
                provider=MockProvider() if runtime is adapter else None,
                status=status,
            )
        )
        for runtime, stop, signal, status in zip(
            (adapter, gateway), stops, ready, statuses, strict=True
        )
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*(s.wait() for s in ready)), 5)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        for _ in range(2):
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "gateway.cli",
                    "client",
                    "--config",
                    str(directory / "client.json"),
                    "--prompt",
                    "synthetic-persistent-session",
                ],
                capture_output=True,
                timeout=15,
                creationflags=flags,
            )
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            assert b"synthetic-persistent-session" in result.stdout
        assert gateway.manager.handshakes["full"] == 2  # inbound plus one upstream
        assert gateway.manager.handshakes["resumed"] == 1
        assert adapter.manager.handshakes["full"] == 1
        assert statuses[1].metrics.snapshot(None)["pool_hit_total"] == 1
    finally:
        for stop in stops:
            stop.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert not gateway.manager._sessions and not adapter.manager._sessions


@pytest.mark.asyncio
async def test_live_revoke_api_closes_related_connection(uis):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    runtime = load_runtime(uis.directory / "client.json", PeerRole.CLIENT)
    try:
        session = await runtime.manager.open(runtime.target)
        code, before = await uis.api("gateway", "session-keys")
        assert code == 200 and before["count"] == 1
        code, after = await uis.api("gateway", "session-keys/revoke", "POST", {"peer_id": "client"})
        assert code == 200 and after["count"] == 0
        assert "secret" not in json.dumps(before) and "ticket" not in json.dumps(before)
        async with asyncio.timeout(3):
            with pytest.raises(GatewayError):
                await session.recv()
    finally:
        await runtime.manager.close()
