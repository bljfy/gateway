"""Real model requests, including the three-role WebUI and native secure links."""

import asyncio
import json
import os
from contextlib import AsyncExitStack, aclosing
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from gateway.adapter import DEFAULT_MODEL, AdapterConfig, create_provider
from gateway.config import Limits
from gateway.contracts import InferenceRequest, PeerRole
from gateway.runtime import prepare_demo
from gateway.webui.server import create_app
from test.fixtures.adapter_output import response_body
from test.integration.test_topology import ports
from test.integration.test_webui import UIs

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
async def test_deepseek_complete_and_stream(stream):
    provider = create_provider(AdapterConfig.from_environment(), Limits())
    try:
        request = InferenceRequest(
            uuid4(),
            os.environ.get("E2E_MODEL", DEFAULT_MODEL),
            "请回复：连接成功",
            max_output_tokens=32,
        )
        if stream:
            async with aclosing(provider.stream(request)) as iterator:
                chunks = [chunk async for chunk in iterator]
            assert chunks and chunks[-1].is_final and sum(c.is_final for c in chunks) == 1
            assert response_body("".join(c.output for c in chunks), request.request_id).strip()
        else:
            response = await provider.complete(request)
            assert response_body(response.output, request.request_id).strip()
    finally:
        await provider.close()


@pytest_asyncio.fixture
async def real_uis(tmp_path, e2e_environment, monkeypatch, request):
    manifest = request.config.rootpath / ".tools/gmssl/manifest.json"
    if os.environ.get("GMSSL_LIBRARY") and os.environ.get("GMSSL_SHA256"):
        manifest = tmp_path / "native.json"
        manifest.write_text(
            json.dumps(
                {
                    "library": os.environ["GMSSL_LIBRARY"],
                    "sha256": os.environ["GMSSL_SHA256"],
                }
            ),
            encoding="utf-8",
        )
    if not manifest.is_file():
        pytest.fail(
            "WebUI E2E requires a verified GmSSL manifest; run bootstrap first", pytrace=False
        )
    native = json.loads(manifest.read_text(encoding="utf-8"))
    directory = str(Path(native["library"]).resolve().parent)
    if os.name == "nt":
        monkeypatch.setenv("PATH", directory + os.pathsep + os.environ.get("PATH", ""))
    else:
        search = os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if not search[0] or Path(search[0]).resolve() != Path(directory):
            pytest.fail(
                "Start pytest with the native loader configured: python scripts/run_e2e.py",
                pytrace=False,
            )
    identities = tmp_path / "identities"
    gateway_port, adapter_port, metrics_port = ports()
    prepare_demo(
        identities,
        manifest,
        gateway_port=gateway_port,
        adapter_port=adapter_port,
        metrics_port=metrics_port,
    )
    async with AsyncExitStack() as stack:
        clients = {}
        for role in PeerRole:
            ui = TestClient(TestServer(create_app(role, identities / f"{role.value}.json")))
            clients[role.value] = await stack.enter_async_context(ui)
        uis = UIs(identities, clients)
        await uis.start()
        yield uis


@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
async def test_real_model_through_three_role_webui(real_uis, stream):
    ws, owner = await real_uis.ws()
    try:
        code, result = await real_uis.api(
            "client",
            "infer",
            "POST",
            {
                "owner": owner,
                "prompt": "请回复：连接成功",
                "model": os.environ.get("E2E_MODEL", DEFAULT_MODEL),
                "stream": stream,
                "max_output_tokens": 32,
                "retrieval_context": [],
            },
        )
        assert code == 202
        request_id = UUID(result["request_id"])
        output = ""
        async with asyncio.timeout(150):
            while True:
                item = await ws.receive_json()
                if item["type"] in {"output", "finished"}:
                    assert item["request_id"] == str(request_id)
                if item["type"] == "output":
                    output += item["output"]
                if item["type"] == "finished":
                    assert item["result"] == "completed", item.get("error")
                    break
        assert response_body(output, request_id).strip()
        async with asyncio.timeout(5):
            while (await real_uis.api("gateway", "state"))[1]["metrics"][
                "requests_completed_total"
            ] < 1:
                await asyncio.sleep(0.05)
        adapter = (await real_uis.api("adapter", "state"))[1]
        assert adapter["model_verified"]
        assert adapter["metrics"]["requests_completed_total"] == 1
    finally:
        await ws.close()
