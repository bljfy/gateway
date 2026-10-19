"""每个 UI 进程拥有一个角色；所有控制操作在同一事件循环串行化。"""

import asyncio
import logging
from collections import deque
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from gateway.adapter import DEFAULT_MODEL
from gateway.client import InferenceClient
from gateway.codec import encode_request
from gateway.contracts import InferenceRequest, PeerRole, SecureSession
from gateway.logging import EventLogger, PrivacyFormatter, set_log_level
from gateway.runtime import Runtime, RuntimeStatus, run_server
from gateway.webui.sessions import SessionHistory
from gateway.webui.settings import Settings, inspect_settings, load_settings, table, text

LOGGER = EventLogger(__name__)
EVENT_CAPACITY = 128
SOCKET_CAPACITY = 8
QUEUE_CAPACITY = 32


class ControlError(Exception):
    """固定错误码，禁止向页面传播原始异常。"""

    def __init__(self, code: str, status: int = 400) -> None:
        self.code, self.status = code, status
        super().__init__(code)


class EventHandler(logging.Handler):
    def __init__(self, controller: "Controller") -> None:
        super().__init__()
        self.controller = controller
        self.loop = asyncio.get_running_loop()
        self.setFormatter(PrivacyFormatter("%(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.controller.event, message)


class Controller:
    def __init__(self, role: PeerRole, path: Path) -> None:
        self.role = role
        self.path = path.resolve()
        self.settings: Settings | None = None
        self.peers: list[dict[str, object]] = []
        self.phase = "stopped"
        self.error: str | None = None
        self.request_id: UUID | None = None
        self.request_phase = "idle"
        self.authenticated = False
        self.runtime: Runtime | None = None
        self.status = RuntimeStatus()
        self.service: asyncio.Task[None] | None = None
        self.operation: asyncio.Task[None] | None = None
        self.operation_runtime: Runtime | None = None
        self.stop_event = asyncio.Event()
        self.lock = asyncio.Lock()
        self.owner: str | None = None
        self.sockets: dict[str, asyncio.Queue[dict[str, object]]] = {}
        self.events: deque[dict[str, object]] = deque(maxlen=EVENT_CAPACITY)
        self.handler: EventHandler | None = None
        self.history = SessionHistory()

    async def initialize(self) -> None:
        self.handler = EventHandler(self)
        logging.getLogger("gateway").addHandler(self.handler)
        try:
            await self.load(self.path)
        except ControlError:
            # 缺失或错误配置不阻止页面启动，可在页面选择另一份本地配置。
            self.error = "config_invalid"

    def snapshot(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "phase": self.phase,
            "error": self.error,
            "config_loaded": self.settings is not None,
            "busy": self.busy,
            "request_id": str(self.request_id) if self.request_id else None,
            "request_phase": self.request_phase,
            "authenticated": self.authenticated,
            "model_verified": self.status.adapter.completed > 0,
            "metrics": self.status.snapshot(self.runtime) if self.runtime else {},
            "default_model": DEFAULT_MODEL,
        }

    @property
    def busy(self) -> bool:
        return self.operation is not None and not self.operation.done()

    def configuration(self) -> dict[str, object]:
        return {
            "settings": self.settings.public(self.role)
            if self.settings
            else {"config_path": str(self.path)},
            "peers": self.peers,
        }

    def event(self, message: str) -> None:
        item: dict[str, object] = {
            "type": "event",
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "message": message,
        }
        self.events.append(item)
        self.broadcast(item)

    def broadcast(self, item: dict[str, object]) -> None:
        for queue in tuple(self.sockets.values()):
            # 控制快照和事件允许丢弃；推理输出走独立的阻塞入队路径。
            if not queue.full():
                queue.put_nowait(item)

    def changed(self) -> None:
        self.broadcast({"type": "state", "state": self.snapshot()})

    def editable(self) -> None:
        if self.phase in {"starting", "running", "stopping"} or self.busy:
            raise ControlError("busy", 409)

    async def load(self, path: Path) -> None:
        async with self.lock:
            self.editable()
            try:
                settings, peers = await load_settings(path, self.role)
            except Exception:
                raise ControlError("config_invalid") from None
            self.path, self.settings, self.peers = path.resolve(), settings, peers
            self.error = None
            self.authenticated = False
            self.changed()

    async def save(self, data: object) -> None:
        async with self.lock:
            self.editable()
            try:
                settings = Settings.parse(data, self.role)
                if Path(settings.config_path) != self.path:
                    raise ValueError("load identity before saving")
                peers = await inspect_settings(settings, self.role)
                settings.save(self.role)
            except Exception:
                raise ControlError("config_invalid") from None
            self.settings, self.peers, self.error = settings, peers, None
            self.authenticated = False
            self.changed()

    def new_runtime(self) -> Runtime:
        if self.settings is None:
            raise ControlError("config_required")
        try:
            runtime = self.settings.runtime(self.role)
            set_log_level(runtime.gateway.log_level)
            return runtime
        except Exception:
            raise ControlError("config_invalid") from None

    async def start(self) -> None:
        async with self.lock:
            if self.role is PeerRole.CLIENT:
                raise ControlError("wrong_role", 404)
            if self.phase == "running":
                return
            self.editable()
            self.runtime = self.new_runtime()
            self.status = RuntimeStatus()
            self.phase, self.error = "starting", None
            self.stop_event = asyncio.Event()
            self.changed()
            ready = asyncio.Event()
            self.service = asyncio.create_task(self._serve(ready))
            waiter = asyncio.create_task(ready.wait())
            try:
                async with asyncio.timeout(10):
                    await asyncio.wait((self.service, waiter), return_when=asyncio.FIRST_COMPLETED)
                if self.service.done():
                    raise ControlError("service_failed", 503)
                self.phase = "running"
            except BaseException:
                self.service.cancel()
                await asyncio.gather(self.service, return_exceptions=True)
                await self.runtime.manager.close()
                self.phase, self.error = "failed", "service_failed"
                raise
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                self.changed()

    async def _serve(self, ready: asyncio.Event) -> None:
        assert self.runtime is not None
        try:
            await run_server(
                self.runtime,
                stop=self.stop_event,
                ready=ready.set,
                status=self.status,
                observer=self.history.observe if self.role is PeerRole.GATEWAY else None,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.phase, self.error = "failed", "service_failed"
            self.changed()

    async def stop(self) -> None:
        async with self.lock:
            if self.role is PeerRole.CLIENT:
                raise ControlError("wrong_role", 404)
            if self.service is None or self.service.done():
                self.phase = "stopped"
                self.changed()
                return
            self.phase = "stopping"
            self.changed()
            self.stop_event.set()
            await self.service
            self.phase = "stopped"
            self.changed()

    async def test_connection(self) -> None:
        async with self.lock:
            if self.role is not PeerRole.CLIENT:
                raise ControlError("wrong_role", 404)
            self.editable()
            runtime = self.new_runtime()
            self.operation_runtime = runtime
            self.authenticated, self.error = False, None
            self.operation = asyncio.create_task(self._test(runtime))
            self.changed()

    async def _test(self, runtime: Runtime) -> None:
        session: SecureSession | None = None
        self.authenticated, self.error = False, None
        try:
            assert runtime.target is not None
            session = await runtime.manager.open(runtime.target)
            self.authenticated = True
            LOGGER.info("client_authenticated")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error = "connection_failed"
            LOGGER.error("client_failed")
        finally:
            if session:
                await session.close()
            await runtime.manager.close()
            asyncio.get_running_loop().call_soon(self.changed)

    async def infer(self, data: object) -> UUID:
        async with self.lock:
            if self.role is not PeerRole.CLIENT:
                raise ControlError("wrong_role", 404)
            self.editable()
            try:
                values = table(
                    data,
                    {
                        "owner",
                        "model",
                        "prompt",
                        "retrieval_context",
                        "max_output_tokens",
                        "stream",
                    },
                )
                owner = text(values.get("owner"))
                if owner not in self.sockets:
                    raise ControlError("socket_required", 409)
                prompt = values.get("prompt")
                context = values.get("retrieval_context", [])
                tokens, stream = values.get("max_output_tokens", 512), values.get("stream", True)
                if type(prompt) is not str or not prompt.strip():
                    raise ValueError("invalid prompt")
                if not isinstance(context, list) or len(context) > 64:
                    raise ValueError("invalid context")
                if any(type(item) is not str for item in context):
                    raise ValueError("invalid context")
                if type(tokens) is not int or not 1 <= tokens <= 2**32 - 1:
                    raise ValueError("invalid budget")
                if type(stream) is not bool:
                    raise ValueError("invalid stream flag")
                request = InferenceRequest(
                    uuid4(),
                    text(values.get("model", DEFAULT_MODEL), maximum=256),
                    prompt,
                    tuple(context),
                    tokens,
                )
                runtime = self.new_runtime()
                try:
                    if len(encode_request(request, stream=stream)) > (
                        runtime.gateway.limits.max_request_body_bytes
                    ):
                        raise ValueError("request exceeds limit")
                except BaseException:
                    await runtime.manager.close()
                    raise
            except ControlError:
                raise
            except Exception:
                raise ControlError("request_invalid") from None
            self.owner, self.request_id = owner, request.request_id
            self.operation_runtime = runtime
            self.request_phase, self.error, self.authenticated = "running", None, False
            self.operation = asyncio.create_task(self._infer(runtime, request, stream, owner))
            self.changed()
            return request.request_id

    async def deliver(self, owner: str, item: dict[str, object]) -> None:
        queue = self.sockets.get(owner)
        if queue is None:
            raise ControlError("socket_required", 409)
        async with asyncio.timeout(5):
            await queue.put(item)

    async def _infer(
        self, runtime: Runtime, request: InferenceRequest, stream: bool, owner: str
    ) -> None:
        session: SecureSession | None = None
        identity = str(request.request_id)
        try:
            assert runtime.target is not None
            LOGGER.info("client_connecting", request_id=request.request_id, stream=stream)
            async with asyncio.timeout(runtime.gateway.limits.request_timeout_seconds):
                session = await runtime.manager.open(runtime.target)
                self.authenticated = True
                LOGGER.info("client_authenticated", request_id=request.request_id)
                self.changed()
                client = InferenceClient(session, runtime.gateway.limits)
                if stream:
                    async with aclosing(client.stream(request)) as iterator:
                        async for chunk in iterator:
                            await self.deliver(
                                owner,
                                {"type": "output", "request_id": identity, "output": chunk.output},
                            )
                else:
                    response = await client.complete(request)
                    # 普通响应也分块入队，限制每个 WebSocket 消息占用。
                    for offset in range(0, len(response.output), 16384):
                        await self.deliver(
                            owner,
                            {
                                "type": "output",
                                "request_id": identity,
                                "output": response.output[offset : offset + 16384],
                            },
                        )
            self.request_phase = "completed"
            LOGGER.info("client_completed", request_id=request.request_id)
        except asyncio.CancelledError:
            self.request_phase = "cancelled"
        except Exception:
            self.request_phase, self.error = "failed", "inference_failed"
            LOGGER.error("client_failed", request_id=request.request_id)
        finally:
            if session:
                await session.close()
            await runtime.manager.close()
            self.owner = None
            try:
                await self.deliver(
                    owner,
                    {
                        "type": "finished",
                        "request_id": identity,
                        "result": self.request_phase,
                        "error": self.error,
                    },
                )
            except (ControlError, TimeoutError):
                pass
            asyncio.get_running_loop().call_soon(self.changed)

    async def cancel(self, request_id: str | None = None) -> None:
        async with self.lock:
            if self.role is not PeerRole.CLIENT:
                raise ControlError("wrong_role", 404)
            if request_id is not None and request_id != str(self.request_id):
                raise ControlError("request_mismatch", 409)
            if self.busy and self.operation:
                self.operation.cancel()
                await asyncio.gather(self.operation, return_exceptions=True)
                if self.operation_runtime:
                    # 任务可能尚未进入协程体，不能仅依赖其 finally 回收运行对象。
                    await self.operation_runtime.manager.close()
                if self.request_phase == "running":
                    self.request_phase = "cancelled"
                self.owner = None
            self.changed()

    async def disconnect(self, owner: str) -> None:
        self.sockets.pop(owner, None)
        if self.owner == owner:
            await self.cancel(str(self.request_id))

    async def close(self) -> None:
        if self.role is PeerRole.CLIENT:
            await self.cancel()
        else:
            await self.stop()
        if self.handler:
            logging.getLogger("gateway").removeHandler(self.handler)
            self.handler.close()
