"""真实 GmSSL 手动密钥／握手／记录攻击及实验隔离。"""

import asyncio
import json

import pytest

from gateway.webui.server import CONTROLLER
from test.integration.test_webui import demo_directory as demo_directory
from test.integration.test_webui import idle
from test.integration.test_webui import uis as uis
from test.integration.test_webui_connections import wait_connections


async def manual_state(uis, phase):
    async with asyncio.timeout(10):
        while True:
            _, state = await uis.api("client", "state")
            if state["manual"] and state["manual"]["phase"] == phase:
                return state["manual"]
            await asyncio.sleep(0.01)


async def command(uis, owner, action, **fields):
    return await uis.api(
        "client",
        "manual/action",
        "POST",
        {
            "owner": owner,
            "command": {"action": action, **fields},
        },
    )


async def open_lab(uis, owner, **options):
    return await uis.api("client", "manual/open", "POST", {"owner": owner, "options": options})


@pytest.mark.asyncio
async def test_manual_stepwise_key_restore_privacy_and_identity_files(uis):
    originals = {path.name: path.read_bytes() for path in uis.directory.iterdir() if path.is_file()}
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        assert (await open_lab(uis, owner))[0] == 202
        await manual_state(uis, "active")
        assert (await command(uis, owner, "send"))[0] == 200
        gateway = uis.clients["gateway"].app[CONTROLLER]
        async with asyncio.timeout(3):
            while next(iter(gateway.runtime.manager._sessions))._receive_sequence != 1:
                await asyncio.sleep(0.01)
        key = "ab" * 16
        assert (await command(uis, owner, "key", key=key))[0] == 200
        assert (await uis.api("client", "state"))[1]["manual"]["key_modified"]
        assert (await command(uis, owner, "restore_key"))[0] == 200
        assert (await command(uis, owner, "send"))[0] == 200
        async with asyncio.timeout(3):
            while next(iter(gateway.runtime.manager._sessions))._receive_sequence != 2:
                await asyncio.sleep(0.01)
        assert key not in json.dumps((await uis.api("client", "state"))[1])
        assert (await command(uis, owner, "close"))[0] == 200
        await idle(uis)
        await wait_connections(uis, 0)
        for name, content in originals.items():
            assert (uis.directory / name).read_bytes() == content
    finally:
        await ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [0, 2, 18, 19, 28, 44, 48, 52, 80])
async def test_manual_header_ciphertext_and_tag_mutations_rejected(uis, offset):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        await open_lab(uis, owner)
        await manual_state(uis, "active")
        assert (
            await command(
                uis,
                owner,
                "tamper",
                record_type=1,
                payload="synthetic-marker",
                offset=offset,
                xor=1,
            )
        )[0] == 200
        await idle(uis)
        monitor = await wait_connections(uis, 0)
        assert monitor["recent"][0]["result"] in {"authentication_failed", "protocol_rejected"}
        _, gateway = await uis.api("gateway", "state")
        assert gateway["metrics"]["requests_completed_total"] == 0
        assert "synthetic-marker" not in json.dumps(gateway)
    finally:
        await ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,fields",
    [
        ("send", {"sequence": "10"}),
        ("send", {"session_id": "00" * 16}),
        ("send", {"direction": 1}),
        ("send", {"nonce": "00" * 12}),
        ("send", {"chunk_index": 1}),
        ("send", {"record_type": 5, "payload": "invalid-control"}),
        ("send", {"record_type": 1, "chunk_index": 1, "payload": "bad-chunk"}),
        ("reflect", {}),
    ],
)
async def test_manual_custom_record_parameters_and_reflection(uis, action, fields):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        await open_lab(uis, owner)
        await manual_state(uis, "active")
        assert (await command(uis, owner, action, **fields))[0] == 200
        await idle(uis)
        monitor = await wait_connections(uis, 0)
        assert monitor["recent"][0]["result"] in {"authentication_failed", "protocol_rejected"}
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_manual_session_key_replacement_rejected(uis):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        await open_lab(uis, owner)
        await manual_state(uis, "active")
        assert (await command(uis, owner, "key", key="11" * 16))[0] == 200
        assert (await command(uis, owner, "send"))[0] == 200
        await idle(uis)
        assert (await wait_connections(uis, 0))["recent"][0]["result"] == "authentication_failed"
        assert not uis.clients["client"].app[CONTROLLER].manual.key_override
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_manual_replay_same_connection_and_cross_session(uis):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        await open_lab(uis, owner)
        await manual_state(uis, "active")
        assert (await command(uis, owner, "send"))[0] == 200
        assert (await command(uis, owner, "replay"))[0] == 200
        await idle(uis)
        monitor = await wait_connections(uis, 0)
        assert monitor["recent"][0]["result"] == "authentication_failed"
        await open_lab(uis, owner)
        await manual_state(uis, "active")
        assert (await command(uis, owner, "cross_session"))[0] == 200
        await idle(uis)
        assert (await wait_connections(uis, 0))["recent"][0]["result"] == "authentication_failed"
    finally:
        await ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"fault": "signing_key"},
        {"fault": "gateway_signing_key"},
        {"fault": "gateway_encryption_key"},
        {"fault": "hello"},
        {"fault": "client_key"},
        {"fault": "client_finished"},
        {"fault": "server_hello"},
        {"fault": "server_finished"},
        {"identity": "untrusted-lab-client"},
        {"key_version": 2},
        {"expired_trust": True},
        {"signing_private": "00" * 31 + "01"},
    ],
)
async def test_manual_handshake_attacks_and_recovery(uis, options):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        assert (await open_lab(uis, owner, **options))[0] == 202
        await manual_state(uis, "closed")
        controller = uis.clients["client"].app[CONTROLLER]
        assert controller.manual.result in {"authentication_failed", "connection_failed", "timeout"}
        assert not controller.manual.proxy_tasks
        assert not controller.manual.options.keys
        await idle(uis)
        await wait_connections(uis, 0)
        assert (await uis.api("client", "test", "POST"))[0] == 202
        assert (await idle(uis))["authenticated"]
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_manual_raw_frame_disconnect_and_early_cancel(uis):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        await open_lab(uis, owner, mode="raw")
        await manual_state(uis, "raw_connected")
        assert (await command(uis, owner, "raw", raw="ff"))[0] == 200
        await idle(uis)
        assert (await wait_connections(uis, 0))["recent"][0]["result"] == "protocol_rejected"
        controller = uis.clients["client"].app[CONTROLLER]
        await controller.open_manual({"owner": owner, "options": {}})
        runtime = controller.operation_runtime
        await controller.cancel()
        assert not controller.busy and controller.manual.phase == "closed"
        assert runtime.manager.session_counts()["active_sessions"] == 0
        await open_lab(uis, owner)
        await manual_state(uis, "active")
        runtime = controller.operation_runtime
        await ws.close()
        await idle(uis)
        await wait_connections(uis, 0)
        assert not runtime.manager._sessions and not runtime.manager._native_tasks
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_manual_client_key_replay_has_real_previous_transcript(uis):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        assert (await open_lab(uis, owner, fault="client_key_replay"))[0] == 400
        await open_lab(uis, owner)
        await manual_state(uis, "active")
        controller = uis.clients["client"].app[CONTROLLER]
        assert controller.manual.key_frame
        await command(uis, owner, "close")
        await idle(uis)
        await open_lab(uis, owner, fault="client_key_replay")
        await manual_state(uis, "closed")
        assert any(step["action"] == "handshake_replayed" for step in controller.manual.steps)
        await idle(uis)
        assert (await wait_connections(uis, 0))["recent"][0]["result"] == "authentication_failed"
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_manual_validation_owner_and_failed_input_preserves_sequence(uis):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    observer, other = await uis.ws()
    try:
        for options in (
            {"duration": 0},
            {"duration": 61},
            {"key_version": True},
            {"signing_private": "zz"},
            {"gateway_signing_public": "00"},
            {"signing_private": False},
            {"fault": "bad"},
            {"host": "example.com"},
        ):
            assert (await open_lab(uis, owner, **options))[0] == 400
        assert (await uis.api("gateway", "manual/open", "POST", {"options": {}}))[0] == 404
        await open_lab(uis, owner)
        await manual_state(uis, "active")
        assert (await command(uis, other, "send"))[0] == 409
        assert (await open_lab(uis, owner))[0] == 409
        for action, fields in [
            ("key", {"key": "00"}),
            ("tamper", {"offset": 9999}),
            ("send", {"sequence": "-1"}),
            ("send", {"sequence": "9" * 21}),
            ("send", {"nonce": "bad"}),
            ("raw", {"raw": "gg"}),
        ]:
            assert (await command(uis, owner, action, **fields))[0] == 400
        assert (await uis.api("client", "state"))[1]["manual"]["next_sequence"] == "0"
        assert (await command(uis, owner, "send"))[0] == 200
        assert (await command(uis, owner, "close"))[0] == 200
        await idle(uis)
        assert (await command(uis, owner, "close"))[0] == 409
    finally:
        await ws.close()
        await observer.close()


@pytest.mark.asyncio
async def test_manual_experiment_deadline_and_no_model_calls(uis):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        await open_lab(uis, owner, duration=1)
        await manual_state(uis, "active")
        await command(uis, owner, "send", record_type=1, payload="incomplete-lab-request")
        state = await manual_state(uis, "closed")
        assert state["result"] == "timeout"
        await wait_connections(uis, 0)
        gateway = (await uis.api("gateway", "state"))[1]
        assert gateway["metrics"]["requests_completed_total"] == 0
        assert not uis.clients["adapter"].app[CONTROLLER].runtime
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_manual_incomplete_request_timeout_is_observed_at_gateway(uis):
    with (uis.directory / "gateway.toml").open("a", encoding="utf-8") as output:
        output.write("request_timeout_seconds=1\n")
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        await open_lab(uis, owner, duration=5)
        await manual_state(uis, "active")
        await command(uis, owner, "send", record_type=1, payload="timeout-lab")
        await idle(uis)
        assert (await wait_connections(uis, 0))["recent"][0]["result"] == "timeout"
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_manual_action_budget_and_old_close_cannot_cancel_new_operation(uis):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        await open_lab(uis, owner)
        await manual_state(uis, "active")
        for _ in range(64):
            assert (await command(uis, owner, "restore_key"))[0] == 200
        assert (await command(uis, owner, "send"))[0] == 400
        assert (await command(uis, owner, "close"))[0] == 200
        assert (
            await uis.api(
                "client",
                "simulation",
                "POST",
                {
                    "owner": owner,
                    "count": 1,
                    "duration": 30,
                    "mode": "normal",
                },
            )
        )[0] == 202
        assert (await command(uis, owner, "close"))[0] == 409
        assert (await uis.api("client", "state"))[1]["busy"]
        assert (await uis.api("client", "simulation/stop", "POST"))[0] == 200
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_manual_repeated_cancel_during_cleanup_drains_proxy_and_erases_keys(uis, monkeypatch):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    release = asyncio.Event()
    cancelling = None
    try:
        await open_lab(uis, owner, duration=1)
        await manual_state(uis, "active")
        controller = uis.clients["client"].app[CONTROLLER]
        runtime = controller.operation_runtime
        original = runtime.manager.close
        entered = asyncio.Event()

        async def gated_close():
            entered.set()
            await release.wait()
            await original()

        monkeypatch.setattr(runtime.manager, "close", gated_close)
        await command(uis, owner, "key", key="ab" * 16)
        controller.manual.options.keys["signing_private"] = b"synthetic-key-copy"
        await asyncio.wait_for(entered.wait(), 3)
        assert controller.manual.phase == "closing"
        cancelling = asyncio.create_task(controller.cancel())
        await asyncio.sleep(0)
        controller.operation.cancel()
        await asyncio.sleep(0)
        assert not cancelling.done()
        release.set()
        await asyncio.wait_for(cancelling, 5)
        lab = controller.manual
        assert lab.phase == "closed" and not lab.options.keys and not lab.key_override
        assert not lab.proxy_tasks and not lab.proxy.sockets
        assert not runtime.manager._sessions and not runtime.manager._native_tasks
    finally:
        release.set()
        if cancelling:
            await asyncio.gather(cancelling, return_exceptions=True)
        await ws.close()
