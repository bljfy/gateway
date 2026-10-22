"""远程控制面认证和首次配置的真实后端验证。"""

import json

import pytest
from aiohttp import WSServerHandshakeError, encode_basic_auth
from aiohttp.test_utils import TestClient, TestServer

from gateway.contracts import PeerRole
from gateway.webui.server import CONTROLLER, create_app
from test.integration.test_webui import demo_directory as _demo_directory

demo_directory = _demo_directory


@pytest.mark.asyncio
async def test_first_run_creates_shared_identities(tmp_path, demo_directory, monkeypatch):
    # Reuse the verified native manifest while starting from an absent directory.
    monkeypatch.chdir(demo_directory.parent)
    manifest = tmp_path / ".tools/gmssl/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text((tmp_path / "native.json").read_text(), encoding="utf-8")
    directory = tmp_path / "fresh"
    async with TestClient(TestServer(create_app(PeerRole.CLIENT, directory / "client.json"))) as ui:
        headers = {"Origin": str(ui.make_url("/")).rstrip("/"), "X-WebUI-Request": "1"}
        response = await ui.get("/api/setup", headers=headers)
        assert (await response.json())["can_initialize"]
        response = await ui.post("/api/setup", headers=headers, json={"initialize": True})
        assert response.status == 200
        assert (await response.json())["identity_ready"]
        assert all((directory / f"{role.value}.json").exists() for role in PeerRole)
        assert ui.app[CONTROLLER].snapshot()["config_loaded"]


@pytest.mark.asyncio
async def test_remote_auth_origin_and_websocket(demo_directory):
    origin = "https://chat.example.com"
    password = "synthetic-password-1234"
    app = create_app(
        PeerRole.CLIENT, demo_directory / "client.json", public_origin=origin, password=password
    )
    async with TestClient(TestServer(app)) as ui:
        headers = {"Host": "chat.example.com"}
        response = await ui.get("/", headers=headers)
        assert response.status == 401
        assert "Basic" in response.headers["WWW-Authenticate"]
        authenticated = {**headers, "Authorization": encode_basic_auth("admin", password)}
        response = await ui.get("/", headers=authenticated)
        assert response.status == 200
        api_headers = {**authenticated, "Origin": origin, "X-WebUI-Request": "1"}
        response = await ui.get("/api/state", headers=api_headers)
        assert response.status == 200
        response = await ui.post(
            "/api/test", headers={**api_headers, "Origin": "https://evil.example"}
        )
        assert response.status == 403
        response = await ui.get(
            "/api/state",
            headers={**api_headers, "Authorization": encode_basic_auth("admin", "wrong")},
        )
        assert response.status == 401
        with pytest.raises(WSServerHandshakeError):
            await ui.ws_connect("/api/ws", headers=headers, origin=origin)
        async with ui.ws_connect("/api/ws", headers=authenticated, origin=origin) as ws:
            assert (await ws.receive_json())["type"] == "ready"


@pytest.mark.asyncio
async def test_setup_credentials_persist_and_reload(demo_directory, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    path = demo_directory / "adapter.json"
    app = create_app(PeerRole.ADAPTER, path)
    async with TestClient(TestServer(app)) as ui:
        headers = {"Origin": str(ui.make_url("/")).rstrip("/"), "X-WebUI-Request": "1"}
        response = await ui.get("/api/setup", headers=headers)
        assert (await response.json())["missing_credentials"] == ["DEEPSEEK_API_KEY"]
        response = await ui.post(
            "/api/setup",
            headers=headers,
            json={
                "api_key": "synthetic-new-key",
                "api_key_env": "DEEPSEEK_API_KEY",
                "persist": True,
            },
        )
        assert response.status == 200
        assert "synthetic-new-key" not in await response.text()
        assert (await response.json())["ready"]
        response = await ui.post("/api/setup", headers=headers, json={"initialize": True})
        assert response.status == 409
    stored = path.with_suffix(".webui.credentials.json")
    assert json.loads(stored.read_text())["DEEPSEEK_API_KEY"] == "synthetic-new-key"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    async with TestClient(TestServer(create_app(PeerRole.ADAPTER, path))) as ui:
        headers = {"X-WebUI-Request": "1"}
        response = await ui.get("/api/setup", headers=headers)
        assert (await response.json())["ready"]


def test_remote_rejects_unsafe_configuration(tmp_path):
    for origin, password in [
        ("http://chat.example.com", "long-enough-password"),
        ("https://chat.example.com", "short"),
        ("https://chat.example.com/path", "long-enough-password"),
    ]:
        with pytest.raises(ValueError):
            create_app(
                PeerRole.CLIENT, tmp_path / "client.json", public_origin=origin, password=password
            )
