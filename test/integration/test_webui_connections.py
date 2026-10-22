"""真实连接模拟、网关逐连接监控及取消清理。"""

import asyncio
import json

import pytest

from gateway.webui.server import CONTROLLER
from test.integration.test_webui import demo_directory as demo_directory
from test.integration.test_webui import idle
from test.integration.test_webui import uis as uis


async def wait_connections(uis, count, phase=None):
    async with asyncio.timeout(8):
        while True:
            _, state = await uis.api("gateway", "state")
            monitor = state["connections"]
            if monitor["total_active"] == count and (
                phase is None or all(row["phase"] == phase for row in monitor["active"])
            ):
                return monitor
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_parallel_authenticated_clients_live_websocket_and_cleanup(uis):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    observer, _ = await uis.ws("gateway")
    try:
        code, state = await uis.api(
            "client",
            "simulation",
            "POST",
            {"owner": owner, "count": 4, "duration": 30, "mode": "normal"},
        )
        assert code == 202 and state["busy"]
        monitor = await wait_connections(uis, 4, "active")
        assert len({row["id"] for row in monitor["active"]}) == 4
        assert len({row["source"] for row in monitor["active"]}) == 4
        assert all(row["peer"] == "client" for row in monitor["active"])
        async with asyncio.timeout(5):
            while True:
                event = await observer.receive_json()
                if event["type"] == "state" and event["state"]["connections"]["total_active"] == 4:
                    break
        controller = uis.clients["client"].app[CONTROLLER]
        runtime = controller.operation_runtime
        assert (await uis.api("client", "simulation/stop", "POST"))[0] == 200
        assert not (await idle(uis))["busy"]
        monitor = await wait_connections(uis, 0)
        assert len(monitor["recent"]) == 4
        assert all(row["result"] == "closed" for row in monitor["recent"])
        assert runtime.manager.session_counts()["active_sessions"] == 0
    finally:
        await ws.close()
        await observer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,result",
    [
        ("malformed_frame", "protocol_rejected"),
        ("abrupt_disconnect", "protocol_rejected"),
        ("stalled_handshake", "timeout"),
        ("tcp_disconnect", "protocol_rejected"),
        ("empty_frame", "protocol_rejected"),
        ("oversized_frame", "protocol_rejected"),
        ("truncated_frame", "protocol_rejected"),
        ("unexpected_record", "protocol_rejected"),
        ("out_of_order_fragment", "protocol_rejected"),
    ],
)
async def test_abnormal_clients_are_observed_and_gateway_recovers(uis, mode, result):
    with (uis.directory / "gateway.toml").open("a", encoding="utf-8") as output:
        output.write("\n[session]\nhandshake_timeout_seconds=1\n")
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        assert (
            await uis.api(
                "client",
                "simulation",
                "POST",
                {
                    "owner": owner,
                    "count": 3,
                    "duration": 5,
                    "mode": mode,
                },
            )
        )[0] == 202
        state = await idle(uis)
        assert state["simulation"]["phase"] == "completed"
        expected_client_result = (
            "disconnected"
            if mode == "abrupt_disconnect"
            else "tcp_disconnected"
            if mode == "tcp_disconnect"
            else "peer_closed"
        )
        assert all(
            row["result"] == expected_client_result for row in state["simulation"]["clients"]
        )
        monitor = await wait_connections(uis, 0)
        assert len(monitor["recent"]) == 3
        assert all(row["result"] == result for row in monitor["recent"])
        assert (
            all(row["peer"] is None for row in monitor["recent"])
            if mode not in {"abrupt_disconnect", "unexpected_record", "out_of_order_fragment"}
            else all(row["peer"] == "client" for row in monitor["recent"])
        )
        assert (await uis.api("client", "test", "POST"))[0] == 202
        assert (await idle(uis))["authenticated"]
        assert (await uis.api("adapter", "state"))[1]["model_verified"] is False
    finally:
        await ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "heartbeat", "stalled_handshake"])
async def test_simulation_disconnect_and_early_cancel_reclaim_all_sessions(uis, mode):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    controller = uis.clients["client"].app[CONTROLLER]
    await controller.simulate({"owner": owner, "count": 3, "duration": 60, "mode": mode})
    runtime = controller.operation_runtime
    await controller.cancel()
    assert controller.simulation.phase == "cancelled" and not controller.busy
    assert runtime.manager.session_counts()["active_sessions"] == 0
    await controller.simulate({"owner": owner, "count": 3, "duration": 60, "mode": mode})
    await wait_connections(uis, 3, "handshaking" if mode == "stalled_handshake" else "active")
    runtime = controller.operation_runtime
    await ws.close()
    await idle(uis)
    await wait_connections(uis, 0)
    assert runtime.manager.session_counts()["active_sessions"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,result", [("heartbeat", "heartbeat_completed"), ("graceful_close", "gracefully_closed")]
)
async def test_additional_normal_cases_close_cleanly_without_model(uis, mode, result):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        code, _ = await uis.api(
            "client",
            "simulation",
            "POST",
            {
                "owner": owner,
                "count": 3,
                "duration": 1,
                "mode": mode,
            },
        )
        assert code == 202
        state = await idle(uis)
        assert all(row["result"] == result for row in state["simulation"]["clients"])
        monitor = await wait_connections(uis, 0)
        assert len(monitor["recent"]) == 3
        assert all(row["result"] == "closed" for row in monitor["recent"])
        assert (await uis.api("gateway", "state"))[1]["metrics"]["requests_completed_total"] == 0
        assert (await uis.api("client", "test", "POST"))[0] == 202
        assert (await idle(uis))["authenticated"]
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_simulation_validation_role_and_busy(uis):
    ws, owner = await uis.ws()
    valid = {"owner": owner, "count": 1, "duration": 2, "mode": "normal"}
    try:
        for change in (
            {"count": 0},
            {"count": 33},
            {"count": True},
            {"duration": 61},
            {"duration": 0},
            {"mode": "unknown"},
            {"target_host": "example.com"},
        ):
            assert (await uis.api("client", "simulation", "POST", valid | change))[0] == 400
        assert (await uis.api("gateway", "simulation", "POST", valid))[0] == 404
        assert (await uis.api("gateway", "simulation/stop", "POST"))[0] == 404
        assert (await uis.api("client", "simulation", "POST", valid | {"owner": "absent"}))[
            0
        ] == 409
        assert (await uis.api("gateway", "start", "POST"))[0] == 200
        assert (await uis.api("client", "simulation", "POST", valid | {"duration": 30}))[0] == 202
        assert (await uis.api("client", "simulation", "POST", valid))[0] == 409
        assert (await uis.api("client", "infer", "POST", {"owner": owner, "prompt": "x"}))[0] == 409
        assert (await uis.api("client", "simulation/stop", "POST"))[0] == 200
        assert (await uis.api("client", "simulation/stop", "POST"))[0] == 200
        assert "private_key" not in json.dumps((await uis.api("gateway", "state"))[1])
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_normal_simulation_completes_without_model_and_gateway_stop_is_detected(uis):
    assert (await uis.api("gateway", "start", "POST"))[0] == 200
    ws, owner = await uis.ws()
    try:
        assert (
            await uis.api(
                "client",
                "simulation",
                "POST",
                {
                    "owner": owner,
                    "count": 2,
                    "duration": 1,
                    "mode": "normal",
                },
            )
        )[0] == 202
        state = await idle(uis)
        assert all(row["result"] == "held" for row in state["simulation"]["clients"])
        await wait_connections(uis, 0)
        assert (
            await uis.api(
                "client",
                "simulation",
                "POST",
                {
                    "owner": owner,
                    "count": 2,
                    "duration": 60,
                    "mode": "normal",
                },
            )
        )[0] == 202
        await wait_connections(uis, 2, "active")
        assert (await uis.api("gateway", "stop", "POST"))[0] == 200
        state = await idle(uis)
        assert all(
            row["result"] in {"peer_closed", "connection_failed"}
            for row in state["simulation"]["clients"]
        )
        assert (await uis.api("gateway", "state"))[1]["connections"]["total_active"] == 0
    finally:
        await ws.close()
