"""面向已配置网关的有界连接实验，不调用模型服务。"""

import asyncio
from collections.abc import Callable
from contextlib import suppress

from gateway.contracts import SecureSession, SessionClosedError
from gateway.runtime import Runtime
from gateway.session.core import SecuritySession

MODES = frozenset({"normal", "stalled_handshake", "malformed_frame", "abrupt_disconnect"})
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
                if self.mode in {"normal", "abrupt_disconnect"}:
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
                else:
                    try:
                        receiving = asyncio.create_task(session.recv())
                        done, _ = await asyncio.wait({receiving}, timeout=self.duration)
                        if done:
                            receiving.result()
                            row["result"] = "unexpected_record"
                        else:
                            row["result"] = "held"
                    except SessionClosedError:
                        row["result"] = "peer_closed"
            else:
                assert writer is not None
                if self.mode == "malformed_frame":
                    writer.write(b"\x00\x00\x00\x01\xff")
                    await writer.drain()
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
