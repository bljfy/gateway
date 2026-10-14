"""回环 HTTP 控制面与同源 WebSocket；国密业务通道由运行层建立。"""

import asyncio
from collections.abc import AsyncIterator
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from aiohttp.typedefs import Handler

from gateway.contracts import PeerRole
from gateway.webui.controller import (
    QUEUE_CAPACITY,
    SOCKET_CAPACITY,
    ControlError,
    Controller,
)
from gateway.webui.settings import table, text

DEFAULT_PORTS = {PeerRole.CLIENT: 18080, PeerRole.GATEWAY: 18081, PeerRole.ADAPTER: 18082}
CONTROLLER = web.AppKey("controller", Controller)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; "
    "script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'",
}


@web.middleware
async def boundary(request: web.Request, handler: Handler) -> web.StreamResponse:
    transport = request.transport
    address = transport.get_extra_info("sockname") if transport else None
    expected_host = f"127.0.0.1:{address[1]}" if address else ""
    origins = request.headers.getall("Origin", [])
    hosts = request.headers.getall("Host", [])
    valid = hosts == [expected_host] and len(origins) <= 1
    if origins and origins != [f"http://{expected_host}"]:
        valid = False
    if request.path == "/api/ws" or request.method not in {"GET", "HEAD"}:
        valid = valid and origins == [f"http://{expected_host}"]
    if request.path.startswith("/api/") and request.path != "/api/ws":
        valid = valid and request.headers.get("X-WebUI-Request") == "1"
    if not valid:
        return web.json_response({"error": "origin_rejected"}, status=403, headers=SECURITY_HEADERS)
    try:
        response = await handler(request)
    except ControlError as exc:
        response = web.json_response({"error": exc.code}, status=exc.status)
    except web.HTTPException as exc:
        response = web.json_response({"error": "http_rejected"}, status=exc.status)
    except Exception:
        response = web.json_response({"error": "operation_failed"}, status=400)
    if not response.prepared:
        response.headers.update(SECURITY_HEADERS)
    return response


async def body(request: web.Request) -> object:
    if request.content_type != "application/json":
        raise ControlError("json_required", 415)
    try:
        return await request.json()
    except web.HTTPRequestEntityTooLarge:
        raise ControlError("request_too_large", 413) from None
    except (ValueError, UnicodeDecodeError):
        raise ControlError("request_invalid") from None


def create_app(role: PeerRole, config: Path) -> web.Application:
    controller = Controller(role, config)
    sockets: set[web.WebSocketResponse] = set()
    cleanups: set[asyncio.Task[None]] = set()
    app = web.Application(middlewares=[boundary], client_max_size=2 * 1024 * 1024)
    app[CONTROLLER] = controller

    async def resource(request: web.Request) -> web.Response:
        name = request.match_info.get("name", "index.html")
        content_types = {
            "index.html": "text/html",
            "app.js": "application/javascript",
            "output.js": "application/javascript",
            "markdown-it.min.js": "application/javascript",
            "style.css": "text/css",
        }
        if name not in content_types:
            raise web.HTTPNotFound()
        return web.Response(
            body=files("gateway.webui").joinpath("static", name).read_bytes(),
            content_type=content_types[name],
            charset="utf-8",
        )

    async def status(_request: web.Request) -> web.Response:
        return web.json_response(controller.snapshot())

    async def configuration(request: web.Request) -> web.Response:
        if request.method == "PUT":
            await controller.save(await body(request))
        return web.json_response(controller.configuration())

    async def load(request: web.Request) -> web.Response:
        values = table(await body(request), {"config_path"})
        await controller.load(Path(text(values.get("config_path"))))
        return web.json_response(controller.configuration())

    async def start(_request: web.Request) -> web.Response:
        await controller.start()
        return web.json_response(controller.snapshot())

    async def stop(_request: web.Request) -> web.Response:
        await controller.stop()
        return web.json_response(controller.snapshot())

    async def test(_request: web.Request) -> web.Response:
        await controller.test_connection()
        return web.json_response(controller.snapshot(), status=202)

    async def infer(request: web.Request) -> web.Response:
        identity = await controller.infer(await body(request))
        return web.json_response({"request_id": str(identity)}, status=202)

    async def cancel(request: web.Request) -> web.Response:
        values = table(await body(request), {"request_id"})
        await controller.cancel(text(values.get("request_id")))
        return web.json_response(controller.snapshot())

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        if len(sockets) >= SOCKET_CAPACITY:
            raise ControlError("capacity_rejected", 503)
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=1024)
        ws.headers.update(SECURITY_HEADERS)
        # 在 prepare 的 await 前预留容量，避免并发握手突破上限。
        sockets.add(ws)
        owner = str(uuid4())
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(QUEUE_CAPACITY)
        controller.sockets[owner] = queue
        sender: asyncio.Task[None] | None = None

        async def send() -> None:
            try:
                while True:
                    item = await queue.get()
                    async with asyncio.timeout(5):
                        await ws.send_json(item)
            except (TimeoutError, ConnectionError, RuntimeError):
                await ws.close()

        try:
            await ws.prepare(request)
            await ws.send_json(
                {
                    "type": "ready",
                    "owner": owner,
                    "state": controller.snapshot(),
                    "events": list(controller.events),
                }
            )
            sender = asyncio.create_task(send())
            async for _message in ws:
                # WebSocket 仅推送数据；控制操作统一走受保护的 HTTP API。
                await ws.close(code=1008, message=b"receive only")
                break
        finally:

            async def cleanup() -> None:
                try:
                    await controller.disconnect(owner)
                finally:
                    if sender:
                        sender.cancel()
                        await asyncio.gather(sender, return_exceptions=True)
                    sockets.discard(ws)

            # 对端关闭 TCP 时框架可能再次取消 handler；清理任务由应用拥有，
            # 不让该取消中断推理会话和发送任务的回收，停机时统一排空。
            task = asyncio.create_task(cleanup())
            cleanups.add(task)
            task.add_done_callback(cleanups.discard)
            await asyncio.shield(task)
        return ws

    async def lifecycle(_app: web.Application) -> AsyncIterator[None]:
        await controller.initialize()

        async def refresh() -> None:
            while True:
                await asyncio.sleep(1)
                controller.changed()

        updater = asyncio.create_task(refresh())
        try:
            yield
        finally:
            updater.cancel()
            await asyncio.gather(updater, return_exceptions=True)
            await controller.close()

    async def shutdown(_app: web.Application) -> None:
        await controller.close()
        await asyncio.gather(*(ws.close(code=1001) for ws in tuple(sockets)))
        await asyncio.gather(*tuple(cleanups), return_exceptions=True)

    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(shutdown)
    app.add_routes(
        [
            web.get("/", resource),
            web.get("/static/{name}", resource),
            web.get("/api/state", status),
            web.get("/api/config", configuration),
            web.put("/api/config", configuration),
            web.post("/api/config/load", load),
            web.post("/api/start", start),
            web.post("/api/stop", stop),
            web.post("/api/test", test),
            web.post("/api/infer", infer),
            web.post("/api/cancel", cancel),
            web.get("/api/ws", websocket),
        ]
    )
    return app


async def serve_until_stop(app: web.Application, port: int, stop_file: Path) -> None:
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        print(f"WebUI: http://127.0.0.1:{port}", flush=True)
        while not stop_file.exists():
            await asyncio.sleep(0.1)
    finally:
        await runner.cleanup()


def run_webui(
    role: PeerRole, config: Path, port: int | None = None, *, stop_file: Path | None = None
) -> None:
    chosen = DEFAULT_PORTS[role] if port is None else port
    if type(chosen) is not int or not 1 <= chosen <= 65535:
        raise ValueError("invalid UI port")
    if stop_file is not None:
        asyncio.run(serve_until_stop(create_app(role, config), chosen, stop_file))
        return
    web.run_app(
        create_app(role, config),
        host="127.0.0.1",
        port=chosen,
        access_log=None,
        print=lambda _message: print(f"WebUI: http://127.0.0.1:{chosen}", flush=True),
    )
