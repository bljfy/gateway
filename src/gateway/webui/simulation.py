"""面向已配置网关的有界连接实验，不调用模型服务。"""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from uuid import uuid4

from gateway.contracts import RecordType, SecureSession, SessionClosedError
from gateway.protocol import MAX_FRAME, ZERO_REQUEST
from gateway.runtime import Runtime
from gateway.session.core import SecuritySession

SECURE_MODES = frozenset(
    {
        "normal",
        "abrupt_disconnect",
        "heartbeat",
        "graceful_close",
        "unexpected_record",
        "out_of_order_fragment",
    }
)
MODES = SECURE_MODES | frozenset(
    {
        "stalled_handshake",
        "malformed_frame",
        "tcp_disconnect",
        "empty_frame",
        "oversized_frame",
        "truncated_frame",
    }
)
MAX_CLIENTS = 32
MAX_DURATION = 60


class Simulation:
    def __init__(self, count: int, duration: int, mode: str, changed: Callable[[], None]) -> None:
        self.duration, self.mode, self.changed = duration, mode, changed
        self.phase = "running"
        self.rows: list[dict[str, object]] = [
            {"client": index + 1, "phase": "connecting", "result": None} for index in range(count)
        ]

    def snapshot(self) -> dict[str, object]:
        return {"phase": self.phase, "mode": self.mode, "clients": [dict(row) for row in self.rows]}

    async def run(self, runtime: Runtime) -> None:
        # 子任务随批次取消并排空；即使创建后尚未执行就被取消，控制器仍关闭 manager。
        try:
            async with asyncio.TaskGroup() as group:
                for row in self.rows:
                    group.create_task(self.connect(runtime, row))
            self.phase = "completed"
        except asyncio.CancelledError:
            self.phase = "cancelled"
            raise
        finally:
            await runtime.manager.close()
            self.changed()

    async def connect(self, runtime: Runtime, row: dict[str, object]) -> None:
        writer: asyncio.StreamWriter | None = None
        session: SecureSession | None = None
        receiving: asyncio.Task[object] | None = None
        try:
            assert runtime.target is not None
            address = runtime.manager.trust[runtime.target.peer_id].address
            assert address is not None
            async with asyncio.timeout(runtime.gateway.session.handshake_timeout_seconds + 1):
                if self.mode in SECURE_MODES:
                    session = await runtime.manager.open(runtime.target)
                else:
                    reader, writer = await asyncio.open_connection(*address)
            row["phase"] = "authenticated" if session else "tcp_connected"
            self.changed()
            if session:
                if self.mode == "abrupt_disconnect":
                    # 直接中断传输，刻意省略国密 CLOSE/CLOSE_ACK。
                    assert isinstance(session, SecuritySession)
                    session.writer.transport.abort()
                    row["result"] = "disconnected"
                elif self.mode == "graceful_close":
                    await session.close()
                    row["result"] = "gracefully_closed"
                else:
                    if self.mode == "unexpected_record":
                        await session.send(RecordType.RESPONSE, b"simulation", request_id=uuid4())
                    elif self.mode == "out_of_order_fragment":
                        await session.send(
                            RecordType.REQUEST,
                            b"simulation",
                            request_id=uuid4(),
                            chunk_index=1,
                            end_of_message=False,
                        )
                    try:
                        receiving = asyncio.create_task(session.recv())
                        if self.mode == "heartbeat":
                            row["phase"] = "heartbeating"
                            self.changed()
                            deadline = asyncio.get_running_loop().time() + self.duration
                            while not receiving.done():
                                remaining = deadline - asyncio.get_running_loop().time()
                                if remaining <= 0:
                                    break
                                await session.send(
                                    RecordType.HEARTBEAT, b"", request_id=ZERO_REQUEST
                                )
                                await asyncio.wait({receiving}, timeout=min(1, remaining))
                            done = {receiving} if receiving.done() else set()
                        else:
                            done, _ = await asyncio.wait({receiving}, timeout=self.duration)
                        if done:
                            receiving.result()
                            row["result"] = "unexpected_record"
                        else:
                            row["result"] = (
                                "heartbeat_completed" if self.mode == "heartbeat" else "held"
                            )
                    except SessionClosedError:
                        row["result"] = "peer_closed"
            else:
                assert writer is not None
                if self.mode == "tcp_disconnect":
                    row["result"] = "tcp_disconnected"
                    return
                frames = {
                    "malformed_frame": b"\x00\x00\x00\x01\xff",
                    "empty_frame": b"\x00\x00\x00\x00",
                    "oversized_frame": (MAX_FRAME + 1).to_bytes(4, "big"),
                    "truncated_frame": b"\x00\x00\x00\x10\xff",
                }
                if self.mode in frames:
                    writer.write(frames[self.mode])
                    await writer.drain()
                    if self.mode == "truncated_frame":
                        writer.write_eof()
                try:
                    async with asyncio.timeout(self.duration):
                        await reader.readexactly(1)
                    row["result"] = "unexpected_data"
                except asyncio.IncompleteReadError:
                    row["result"] = "peer_closed"
                except TimeoutError:
                    row["result"] = "hold_elapsed"
        except asyncio.CancelledError:
            row["result"] = "cancelled"
            raise
        except Exception:
            row["result"] = "connection_failed"
        finally:
            row["phase"] = "closed"
            if writer:
                writer.close()
                with suppress(OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), 1)
            if session:
                await session.close()
            if receiving:
                receiving.cancel()
                await asyncio.gather(receiving, return_exceptions=True)
            self.changed()
