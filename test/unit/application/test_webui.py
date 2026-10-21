"""WebUI 回环边界、非敏感设置与 CLI 接线。"""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from gateway.cli import main
from gateway.contracts import PeerRole
from gateway.webui.server import CONTROLLER, create_app
from gateway.webui.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
async def test_auth_test_result_lifecycle(outcome, tmp_path, monkeypatch):
    from gateway.webui.controller import Controller

    controller = Controller(PeerRole.CLIENT, tmp_path / "client.json")
    controller.settings = Settings(str(controller.path), target_id="gateway")
    session = SimpleNamespace(close=AsyncMock())
    entered, release = asyncio.Event(), asyncio.Event()

    async def open_session(target):
        entered.set()
        await release.wait()
        if outcome == "failed":
            raise RuntimeError("private-error-must-not-reach-page")
        return session

    manager = SimpleNamespace(open=AsyncMock(side_effect=open_session), close=AsyncMock())
    runtime = SimpleNamespace(target=object(), manager=manager)
    monkeypatch.setattr(controller, "new_runtime", lambda: runtime)
    await controller.test_connection()
    async with asyncio.timeout(3):
        await entered.wait()
    pending = controller.snapshot()
    assert pending["busy"] is True
    assert pending["auth_test"]["phase"] == "running"
    assert pending["auth_test"]["target_id"] == "gateway"
    if outcome == "cancelled":
        controller.operation.cancel()
    release.set()
    await asyncio.gather(controller.operation, return_exceptions=True)
    state = controller.snapshot()
    assert state["auth_test"]["phase"] == outcome
    assert state["auth_test"]["duration_ms"] >= 0
    assert state["auth_test"]["finished_at"]
    assert state["busy"] is False
    assert state["authenticated"] is (outcome == "success")
    assert state["auth_test_totals"] == {
        "success": int(outcome == "success"),
        "failed": int(outcome == "failed"),
    }
    assert "private-error" not in json.dumps(state)
    manager.close.assert_awaited_once()
    assert session.close.await_count == int(outcome == "success")
    # 快照与历史测试结果独立；推理认证状态变化不覆盖测试结果。
    controller.authenticated = not controller.authenticated
    assert controller.snapshot()["auth_test"] == state["auth_test"]
    monkeypatch.setattr(
        "gateway.webui.controller.load_settings",
        AsyncMock(return_value=(controller.settings, [])),
    )
    await controller.load(controller.path)
    assert controller.snapshot()["auth_test"] == {"phase": "idle"}
    assert controller.snapshot()["auth_test_totals"] == state["auth_test_totals"]


@pytest.mark.parametrize("role,port", [("client", 18080), ("gateway", 18081), ("adapter", 18082)])
def test_webui_cli_role_defaults(role, port, monkeypatch):
    calls = []
    monkeypatch.setattr("gateway.cli.run_webui", lambda *args, **kwargs: calls.append(args))
    assert main(["webui", "--role", role]) == 0
    assert calls[0][0] == PeerRole(role)
    assert calls[0][1].as_posix() == f".tools/demo/{role}.json"
    assert calls[0][2] is None
    from gateway.webui.server import DEFAULT_PORTS

    assert DEFAULT_PORTS[PeerRole(role)] == port


def test_webui_cli_explicit_config_port(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("gateway.cli.run_webui", lambda *args, **kwargs: calls.append(args))
    path = tmp_path / "identity.json"
    assert main(["webui", "--role", "client", "--config", str(path), "--port", "18090"]) == 0
    assert calls[0][1:] == (path, 18090)


@pytest.mark.parametrize(
    "change",
    [
        {"target_port": 0},
        {"target_port": 65536},
        {"target_port": True},
        {"target_host": "https://example.com"},
        {"target_host": "127.0.0.1\nsecret"},
        {"private_key": "synthetic-secret"},
        {"target_id": ""},
        {"config_path": ""},
    ],
)
def test_settings_reject_invalid_values(change, tmp_path):
    settings = Settings(str(tmp_path / "client.json"), target_id="gateway").public(PeerRole.CLIENT)
    with pytest.raises(ValueError):
        Settings.parse(settings | change, PeerRole.CLIENT)


@pytest.mark.parametrize(
    "role,settings",
    [
        (
            PeerRole.CLIENT,
            {"target_id": "gateway", "target_host": "127.0.0.1", "target_port": 18443},
        ),
        (
            PeerRole.GATEWAY,
            {
                "listen_host": "127.0.0.1",
                "listen_port": 18443,
                "target_id": "adapter",
                "target_host": "127.0.0.1",
                "target_port": 19443,
            },
        ),
    ],
)
def test_settings_load_legacy_sidecar_without_adapter_services(role, settings, tmp_path):
    legacy = {"config_path": str(tmp_path / f"{role.value}.json"), **settings}

    parsed = Settings.parse(legacy, role)
    parsed_with_previous_default = Settings.parse(legacy | {"services_json": "[]"}, role)

    assert parsed.config_path == str((tmp_path / f"{role.value}.json").resolve())
    assert parsed_with_previous_default == parsed
    assert "services_json" not in parsed.public(role)


@pytest.mark.parametrize("role", [PeerRole.CLIENT, PeerRole.GATEWAY])
@pytest.mark.parametrize("services_json", [None, "[{}]"])
def test_settings_reject_nonempty_legacy_adapter_services(role, services_json, tmp_path):
    settings = Settings(str(tmp_path / f"{role.value}.json"), target_id="peer").public(role)

    with pytest.raises(ValueError, match="invalid adapter configuration"):
        Settings.parse(settings | {"services_json": services_json}, role)


@pytest.mark.parametrize(
    "base_url",
    ["http://192.168.1.1", "https://u:p@example.com", "https://example.com?api_key=secret"],
)
def test_settings_reject_unsafe_model_urls(base_url, tmp_path):
    settings = Settings(str(tmp_path / "adapter.json")).public(PeerRole.ADAPTER)
    with pytest.raises(Exception, match="invalid adapter configuration"):
        Settings.parse(settings | {"base_url": base_url}, PeerRole.ADAPTER)


def test_atomic_sidecar_contains_only_settings(tmp_path):
    path = tmp_path / "client.json"
    path.write_text("original identity", encoding="utf-8")
    settings = Settings(str(path), target_id="gateway")
    settings.save(PeerRole.CLIENT)
    assert path.read_text(encoding="utf-8") == "original identity"
    assert (
        Settings.parse(json.loads(settings.sidecar.read_text(encoding="utf-8")), PeerRole.CLIENT)
        == settings
    )
    assert not list(tmp_path.glob(".webui-*"))


@pytest.mark.asyncio
async def test_loopback_boundary_missing_config_resources_and_privacy(tmp_path):
    async with TestClient(TestServer(create_app(PeerRole.CLIENT, tmp_path / "missing.json"))) as ui:
        origin = str(ui.make_url("/")).rstrip("/")
        headers = {"Origin": origin, "X-WebUI-Request": "1"}
        assert (await ui.get("/")).status == 200
        for name in ("style.css", "app.js", "output.js", "markdown-it.min.js"):
            response = await ui.get("/static/" + name)
            assert response.status == 200
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert (await ui.get("/static/missing.txt")).status == 404
        assert (await ui.get("/api/state")).status == 403
        assert (await ui.get("/api/state", headers={"X-WebUI-Request": "1"})).status == 200
        assert (await ui.get("/", headers={"Host": "attacker.example"})).status == 403
        for replacement in (
            {"Origin": "https://attacker.example"},
            {"Origin": "null"},
            {"X-WebUI-Request": "0"},
        ):
            assert (
                await ui.post(
                    "/api/config/load", headers=headers | replacement, json={"config_path": "x"}
                )
            ).status == 403
        assert (
            await ui.post("/api/config/load", headers={"X-WebUI-Request": "1"}, json={})
        ).status == 403
        assert (await ui.post("/api/config/load", headers=headers, data="not json")).status == 415
        assert (
            await ui.post(
                "/api/config/load", headers=headers | {"Content-Type": "application/json"}, data="{"
            )
        ).status == 400
        with pytest.raises(WSServerHandshakeError):
            await ui.ws_connect("/api/ws", origin="https://attacker.example")
        controller = ui.app[CONTROLLER]
        logger = logging.getLogger("gateway")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            logger.error("synthetic-business-secret", exc_info=ValueError("synthetic-secret"))
            await asyncio.sleep(0)
            assert "synthetic" not in json.dumps(list(controller.events))
            for i in range(140):
                controller.event(f"event {i}")
            assert len(controller.events) == 128
        finally:
            logger.setLevel(old_level)
        async with ClientSession() as session:
            response = await session.get(
                ui.make_url("/api/state"), headers=headers | {"Origin": "http://127.0.0.1:1"}
            )
            assert response.status == 403
