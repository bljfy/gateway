"""Lease ownership, bounded waiting and cancellation without shared readers."""

import asyncio
from dataclasses import replace
from time import monotonic

import pytest

from gateway.config import GatewayConfig, load_config
from gateway.contracts import CapacityError, SessionState
from gateway.metrics import Metrics
from gateway.server.pool import UpstreamPool
from test.unit.gateway.test_relay import UPSTREAM, Manager


@pytest.mark.parametrize("name", ["resumption_enabled", "pool_enabled"])
@pytest.mark.parametrize("value", ['"false"', '"true"', "0", "1", "[]"])
def test_feature_switch_rejects_non_boolean(tmp_path, name, value):
    path = tmp_path / "config.toml"
    path.write_text(f'[upstream]\npeer_id="adapter"\n[gateway]\n{name}={value}\n')
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.asyncio
async def test_exclusive_lease_reuse_wait_and_discard():
    manager = Manager(None)
    metrics = Metrics()
    config = replace(
        GatewayConfig(UPSTREAM), pool_enabled=True, pool_max_connections=1, pool_max_waiters=1
    )
    pool = UpstreamPool(manager, config, metrics)
    try:
        first = await pool.acquire(monotonic() + 2)
        waiting = asyncio.create_task(pool.acquire(monotonic() + 2))
        await asyncio.sleep(0)
        assert not waiting.done()
        with pytest.raises(CapacityError):
            await pool.acquire(monotonic() + 1)
        await pool.release(first, reusable=True)
        second = await waiting
        assert second is first and len(manager.opened) == 1
        await pool.release(second, reusable=False)
        assert second.state is SessionState.CLOSED
        third = await pool.acquire(monotonic() + 2)
        assert third is not first
        await pool.release(third, reusable=True)
        assert metrics.snapshot(None)["pool_hit_total"] == 1
    finally:
        await pool.close()
    assert not pool._all and not pool._idle and not pool._waiters


@pytest.mark.asyncio
async def test_timeout_cancel_and_shutdown_wake_waiters():
    pool = UpstreamPool(
        Manager(None), replace(GatewayConfig(UPSTREAM), pool_max_connections=1), Metrics()
    )
    first = await pool.acquire(monotonic() + 2)
    try:
        with pytest.raises(TimeoutError):
            await pool.acquire(monotonic() + 0.01)
        waiting = asyncio.create_task(pool.acquire(monotonic() + 2))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not pool._waiters
        waiting = asyncio.create_task(pool.acquire(monotonic() + 2))
        await asyncio.sleep(0)
        await pool.close()
        with pytest.raises(CapacityError):
            await waiting
        assert first.state is SessionState.CLOSED
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_idle_reaper_closes_connection():
    manager = Manager(None)
    pool = UpstreamPool(manager, replace(GatewayConfig(UPSTREAM), pool_idle_seconds=1), Metrics())
    session = await pool.acquire(monotonic() + 2)
    try:
        await pool.release(session, reusable=True)
        await asyncio.wait_for(session.closed.wait(), 3)
        assert not pool._all
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_cancel_bulk_retirement_closes_every_stale_connection():
    manager = Manager(None)
    pool = UpstreamPool(manager, GatewayConfig(UPSTREAM), Metrics())
    first = await pool.acquire(monotonic() + 2)
    second = await pool.acquire(monotonic() + 2)
    first.close_gate = asyncio.Event()
    first.state = second.state = SessionState.DRAINING
    pool._idle = [(first, monotonic()), (second, monotonic())]
    acquiring = asyncio.create_task(pool.acquire(monotonic() + 5))
    try:
        await asyncio.wait_for(first.close_started.wait(), 2)
        acquiring.cancel()
        await asyncio.wait_for(second.closed.wait(), 2)
        acquiring.cancel()
        first.close_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await acquiring
        assert first.closed.is_set() and second.closed.is_set()
        assert not pool._retiring and not pool._all
    finally:
        first.close_gate.set()
        await pool.close()
