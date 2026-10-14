"""Adapter config migration and credential failure before readiness."""

import asyncio
import json
import os
import socket

import pytest

from gateway.contracts import GatewayError, PeerRole
from gateway.runtime import load_runtime, prepare_demo, run_server
from test.integration.test_topology import ports


def provision(tmp_path):
    manifest = tmp_path / "native.json"
    manifest.write_text(
        json.dumps(
            {
                "library": os.environ["GMSSL_LIBRARY"],
                "sha256": os.environ["GMSSL_SHA256"],
            }
        )
    )
    directory = tmp_path / "demo"
    gp, ap, mp = ports()
    prepare_demo(directory, manifest, gateway_port=gp, adapter_port=ap, metrics_port=mp)
    return directory


@pytest.mark.asyncio
async def test_missing_credential_does_not_start_adapter(tmp_path, backend, monkeypatch):
    directory = provision(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    runtime = load_runtime(directory / "adapter.json", PeerRole.ADAPTER)
    ready = asyncio.Event()
    with pytest.raises(GatewayError, match="credential missing"):
        await run_server(runtime, ready=ready.set)
    assert not ready.is_set() and runtime.manager._closed
    with socket.create_server(runtime.listen):
        pass


@pytest.mark.asyncio
async def test_gateway_and_client_do_not_require_model_credentials(tmp_path, backend, monkeypatch):
    directory = provision(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    for role in (PeerRole.CLIENT, PeerRole.GATEWAY):
        runtime = load_runtime(directory / f"{role.value}.json", role)
        assert runtime.adapter_config is None
        await runtime.manager.close()


@pytest.mark.parametrize("field,value", [("provider", "unknown"), ("api_key_env", True)])
def test_bad_adapter_config_rejected(tmp_path, backend, field, value):
    directory = provision(tmp_path)
    path = directory / "adapter.json"
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(GatewayError, match="^invalid runtime configuration$"):
        load_runtime(path, PeerRole.ADAPTER)


def test_provider_options_not_accepted_for_gateway(tmp_path, backend):
    directory = provision(tmp_path)
    path = directory / "gateway.json"
    data = json.loads(path.read_text())
    data["provider"] = "deepseek"
    path.write_text(json.dumps(data))
    with pytest.raises(GatewayError, match="^invalid runtime configuration$"):
        load_runtime(path, PeerRole.GATEWAY)
