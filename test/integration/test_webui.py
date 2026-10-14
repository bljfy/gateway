"""三个独立控制面的真实 SM2/SM4-GCM 链路与清理行为。"""

import asyncio
import json
import os
import socket
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from gateway.contracts import InferenceChunk, PeerRole
from gateway.crypto import GmSSLBackend
from gateway.runtime import prepare_demo, run_server
from gateway.webui.server import CONTROLLER, create_app
from test.fixtures.adapter_output import response_body
from test.fixtures.mock_provider import MockProvider
from test.integration.test_topology import ports


@dataclass
class UIs:
    directory: Path
    clients: dict[str, TestClient]

    async def api(self, role, path, method="GET", data=None):
        ui = self.clients[role]
        headers = {"Origin": str(ui.make_url("/")).rstrip("/"), "X-WebUI-Request": "1"}
        response = await ui.request(method, "/api/" + path, headers=headers, json=data)
        return response.status, await response.json()

    async def ws(self, role="client"):
        ui = self.clients[role]
        ws = await ui.ws_connect("/api/ws", origin=str(ui.make_url("/")).rstrip("/"))
        ready = await ws.receive_json(timeout=5)
        assert ready["type"] == "ready"
        return ws, ready["owner"]

    async def start(self):
        for role in ("adapter", "gateway"):
            code, data = await self.api(role, "start", "POST")
            assert code == 200 and data["phase"] == "running"


@pytest.fixture
def demo_directory(tmp_path, backend: GmSSLBackend):
    manifest = tmp_path / "native.json"
    manifest.write_text(
        json.dumps({"library": os.environ["GMSSL_LIBRARY"], "sha256": os.environ["GMSSL_SHA256"]}),
        encoding="utf-8",
    )
    directory = tmp_path / "demo"
    gp, ap, mp = ports()
    prepare_demo(directory, manifest, gateway_port=gp, adapter_port=ap, metrics_port=mp)
    return directory


@pytest_asyncio.fixture
async def uis(demo_directory):
    async with AsyncExitStack() as stack:
        clients = {}
        for role in PeerRole:
            ui = TestClient(TestServer(create_app(role, demo_directory / f"{role.value}.json")))
            clients[role.value] = await stack.enter_async_context(ui)
        yield UIs(demo_directory, clients)


async def finished(ws):
    output, messages = "", []
    async with asyncio.timeout(10):
        while True:
            item = await ws.receive_json()
            messages.append(item)
            if item["type"] == "output":
                output += item["output"]
            if item["type"] == "finished":
                return output, item, messages


async def idle(uis):
    async with asyncio.timeout(5):
        while True:
            _, state = await uis.api("client", "state")
            if not state["busy"]:
                return state
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_real_three_ui_roundtrip_context_uuid_privacy_and_restart(uis, stream):
    await uis.start()
    ws, owner = await uis.ws()
    observer, _ = await uis.ws()
    try:
        assert (await uis.api("client", "test", "POST"))[0] == 202
        assert (await idle(uis))["authenticated"]
        marker = "synthetic-webui-private-marker"
        prompt = marker + " 中文🙂 <script>alert(1)</script>"
        code, result = await uis.api(
            "client",
            "infer",
            "POST",
            {
                "owner": owner,
                "model": "m",
                "prompt": prompt,
                "retrieval_context": ["片段甲", "片段乙"],
                "max_output_tokens": 1024,
                "stream": stream,
            },
        )
        assert code == 202
        output, last, messages = await finished(ws)
        assert last["result"] == "completed" and last["request_id"] == result["request_id"]
        assert response_body(output) == f"[mock:m] {prompt} (context: 片段甲 | 片段乙)"
        assert sum(item["type"] == "finished" for item in messages) == 1
        assert (await idle(uis))["request_phase"] == "completed"
        for role in ("client", "gateway", "adapter"):
            code, config = await uis.api(role, "config")
            assert code == 200
            assert (
                not {"trust", "private_key", "password_file", "signing_key", "encryption_key"}
                & config.keys()
            )
            controller = uis.clients[role].app[CONTROLLER]
            safe = json.dumps(
                [controller.snapshot(), controller.configuration(), list(controller.events)]
            )
            assert marker not in safe and "片段甲" not in safe
        _, adapter = await uis.api("adapter", "state")
        assert adapter["metrics"]["requests_completed_total"] == 1
        assert adapter["model_verified"] is True
        _, gateway = await uis.api("gateway", "state")
        assert gateway["metrics"]["requests_completed_total"] == 1
        # 第二个页面只收到状态和脱敏事件，业务输出不能广播。
        observed = []
        try:
            while True:
                observed.append(await observer.receive_json(timeout=0.1))
        except TimeoutError:
            pass
        assert all(item["type"] not in {"output", "finished"} for item in observed)
        assert marker not in json.dumps(observed)
        for role in ("gateway", "adapter"):
            assert (await uis.api(role, "start", "POST"))[0] == 200
            _, data = await uis.api(role, "config")
            assert (await uis.api(role, "config", "PUT", data["settings"]))[0] == 409
            assert (await uis.api(role, "stop", "POST"))[0] == 200
            assert (await uis.api(role, "stop", "POST"))[0] == 200
            controller = uis.clients[role].app[CONTROLLER]
            assert controller.runtime.manager.session_counts()["active_sessions"] == 0
            with socket.create_server(controller.runtime.listen):
                pass
            assert (await uis.api(role, "start", "POST"))[0] == 200
    finally:
        await ws.close()
        await observer.close()


@pytest.mark.asyncio
async def test_config_sidecar_restore_invalid_role_untrusted_target_and_ports(uis):
    original = (uis.directory / "client.json").read_bytes()
    _, data = await uis.api("client", "config")
    settings = data["settings"] | {"target_host": "192.168.1.25", "target_port": 18444}
    assert (await uis.api("client", "config", "PUT", settings))[0] == 200
    assert (uis.directory / "client.json").read_bytes() == original
    sidecar = json.loads((uis.directory / "client.webui.json").read_text(encoding="utf-8"))
    assert sidecar == settings
    assert (
        await uis.api(
            "client", "config/load", "POST", {"config_path": str(uis.directory / "client.json")}
        )
    )[1]["settings"] == settings
    for change in (
        {"target_port": 65536},
        {"target_id": "unknown"},
        {"base_url": "https://example.com"},
    ):
        assert (await uis.api("client", "config", "PUT", settings | change))[0] == 400
    assert (
        await uis.api(
            "client", "config/load", "POST", {"config_path": str(uis.directory / "adapter.json")}
        )
    )[0] == 400
    assert (await uis.api("client", "config"))[1]["settings"] == settings
    async with TestClient(
        TestServer(create_app(PeerRole.CLIENT, uis.directory / "client.json"))
    ) as reload:
        assert reload.app[CONTROLLER].settings.target_port == 18444


@pytest.mark.asyncio
async def test_failed_start_port_occupied_recovery_and_socket_does_not_stop_server(uis):
    _, config = await uis.api("adapter", "config")
    settings = config["settings"]
    with socket.create_server((settings["listen_host"], settings["listen_port"])):
        code, state = await uis.api("adapter", "start", "POST")
        assert code == 503 and state["error"] == "service_failed"
    assert (await uis.api("adapter", "start", "POST"))[0] == 200
    ws, _ = await uis.ws("adapter")
    await ws.close()
    assert (await uis.api("adapter", "state"))[1]["phase"] == "running"
    assert (await uis.api("adapter", "stop", "POST"))[0] == 200
    with socket.create_server((settings["listen_host"], settings["listen_port"])):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreachable", "wrong_key", "expired"])
async def test_connection_test_requires_real_authentication(uis, failure):
    if failure == "unreachable":
        _, config = await uis.api("client", "config")
        assert (
            await uis.api(
                "client", "config", "PUT", config["settings"] | {"target_port": ports()[0]}
            )
        )[0] == 200
    else:
        path = uis.directory / "client.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if failure == "expired":
            data["trust"][0]["expires_at"] = 1
        else:
            adapter = json.loads((uis.directory / "gateway.json").read_text(encoding="utf-8"))
            data["trust"][0]["signing_public_key"] = adapter["trust"][1]["signing_public_key"]
        path.write_text(json.dumps(data), encoding="utf-8")
        await uis.start()
    assert (await uis.api("client", "test", "POST"))[0] == 202
    state = await idle(uis)
    assert not state["authenticated"] and state["error"] == "connection_failed"


class SlowProvider(MockProvider):
    async def _stream(self, request):
        yield InferenceChunk(request.request_id, 0, "partial-🙂")
        await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "disconnect", "timeout"])
async def test_partial_stream_cancellation_timeout_and_cleanup(uis, ending, monkeypatch):
    provider = SlowProvider()

    async def serve(runtime, **kwargs):
        await run_server(
            runtime,
            provider=provider if runtime.manager.local.peer.role is PeerRole.ADAPTER else None,
            **kwargs,
        )

    monkeypatch.setattr("gateway.webui.controller.run_server", serve)
    if ending == "timeout":
        with (uis.directory / "gateway.toml").open("a", encoding="utf-8") as output:
            output.write("request_timeout_seconds=1\n")
    await uis.start()
    ws, owner = await uis.ws()
    code, result = await uis.api(
        "client", "infer", "POST", {"owner": owner, "prompt": "synthetic-slow", "stream": True}
    )
    assert code == 202
    async with asyncio.timeout(5):
        while (await ws.receive_json())["type"] != "output":
            pass
    assert (await uis.api("client", "infer", "POST", {"owner": owner, "prompt": "other"}))[0] == 409
    _, config = await uis.api("client", "config")
    assert (await uis.api("client", "config", "PUT", config["settings"]))[0] == 409
    if ending == "disconnect":
        await ws.close()
    elif ending == "cancel":
        assert (await uis.api("client", "cancel", "POST", {"request_id": "wrong"}))[0] == 409
        assert (await uis.api("client", "cancel", "POST", result))[0] == 200
    if ending != "disconnect":
        _, last, _ = await finished(ws)
        assert last["result"] == ("cancelled" if ending == "cancel" else "failed")
        await ws.close()
    state = await idle(uis)
    assert state["request_phase"] != "completed"
    async with asyncio.timeout(5):
        while provider.metrics.inflight:
            await asyncio.sleep(0.02)
        while uis.clients["adapter"].app[CONTROLLER].status.adapter.inflight:
            await asyncio.sleep(0.02)
    assert (await uis.api("gateway", "state"))[1]["phase"] == "running"


@pytest.mark.asyncio
async def test_cancel_before_task_runs_closes_runtime(uis):
    controller = uis.clients["client"].app[CONTROLLER]
    controller.sockets["synthetic-owner"] = asyncio.Queue(32)
    identity = await controller.infer({"owner": "synthetic-owner", "prompt": "early cancel"})
    runtime = controller.operation_runtime
    await controller.cancel(str(identity))
    assert not controller.busy and controller.request_phase == "cancelled"
    assert controller.owner is None
    with pytest.raises(Exception, match="manager closed"):
        await runtime.manager.open(runtime.target)
    controller.sockets.pop("synthetic-owner")


@pytest.mark.asyncio
async def test_missing_credential_failure_and_valid_role_settings(uis, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert (await uis.api("adapter", "start", "POST"))[0] == 503
    _, state = await uis.api("adapter", "state")
    assert state["phase"] == "failed" and not state["model_verified"]
    _, config = await uis.api("adapter", "config")
    assert (
        await uis.api("adapter", "config", "PUT", config["settings"] | {"target_id": "gateway"})
    )[0] == 400
    assert (await uis.api("adapter", "infer", "POST", {"prompt": "x"}))[0] == 404
    assert (await uis.api("client", "start", "POST"))[0] == 404


@pytest.mark.asyncio
async def test_socket_capacity_request_validation_and_body_limit(uis):
    sockets = []
    try:
        for _ in range(8):
            sockets.append((await uis.ws())[0])
        with pytest.raises(WSServerHandshakeError) as exc:
            await uis.ws()
        assert exc.value.status == 503
        controller = uis.clients["client"].app[CONTROLLER]
        owner = next(iter(controller.sockets))
        for change in (
            {"prompt": ""},
            {"max_output_tokens": True},
            {"max_output_tokens": 0},
            {"stream": 1},
            {"retrieval_context": [1]},
            {"retrieval_context": "x"},
            {"owner": "absent"},
            {"model": ""},
            {"private_key": "synthetic-secret"},
        ):
            code, _ = await uis.api(
                "client", "infer", "POST", {"owner": owner, "prompt": "x"} | change
            )
            assert code in {400, 409}
        code, _ = await uis.api(
            "client", "infer", "POST", {"owner": owner, "prompt": "x" * (2 * 1024 * 1024)}
        )
        assert code == 413
    finally:
        await asyncio.gather(*(ws.close() for ws in sockets))
