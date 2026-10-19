"""一键启动已有三角色控制台，退出时仅清理本次创建的进程。"""

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from gateway.contracts import GatewayError, PeerRole
from gateway.runtime import prepare_demo


def api(port: int, path: str, *, start: bool = False) -> dict[str, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request(
            "POST" if start else "GET",
            f"/api/{path}",
            headers={"Origin": f"http://127.0.0.1:{port}", "X-WebUI-Request": "1"},
        )
        response = connection.getresponse()
        data = json.loads(response.read(65536))
        if response.status != 200 or not isinstance(data, dict):
            raise GatewayError("WebUI operation failed")
        return cast(dict[str, object], data)
    finally:
        connection.close()


def launch(
    directory: Path,
    manifest: Path,
    ports: dict[PeerRole, int],
    *,
    open_browser: bool = True,
    stop_file: Path | None = None,
) -> int:
    if set(ports) != set(PeerRole) or len(set(ports.values())) != 3:
        raise ValueError("three distinct UI ports required")
    for port in ports.values():
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid UI port")
    # 在生成身份或创建子进程前检查 UI 端口，避免重复启动接管已有控制台。
    reserved: list[socket.socket] = []
    try:
        for port in ports.values():
            listener = socket.socket()
            reserved.append(listener)
            listener.bind(("127.0.0.1", port))
    finally:
        for listener in reserved:
            listener.close()
    directory = directory.resolve()
    if not directory.exists():
        prepare_demo(directory, manifest)
    processes: list[tuple[subprocess.Popen[bytes], Path]] = []
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW
    with TemporaryDirectory(prefix="launcher-", dir=directory) as temporary:
        try:
            for role in (PeerRole.ADAPTER, PeerRole.GATEWAY, PeerRole.CLIENT):
                child_stop = Path(temporary) / f"{role.value}.stop"
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "gateway.cli",
                        "webui",
                        "--role",
                        role.value,
                        "--config",
                        str(directory / f"{role.value}.json"),
                        "--port",
                        str(ports[role]),
                        "--stop-file",
                        str(child_stop),
                    ],
                    creationflags=creation_flags,
                    start_new_session=os.name != "nt",
                )
                processes.append((process, child_stop))
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise GatewayError(f"{role.value} WebUI failed to start")
                    try:
                        state = api(ports[role], "state")
                    except (OSError, http.client.HTTPException):
                        time.sleep(0.1)
                        continue
                    if state.get("role") != role.value or not state.get("config_loaded"):
                        raise GatewayError(f"{role.value} configuration failed to load")
                    break
                if role is not PeerRole.CLIENT:
                    if api(ports[role], "start", start=True).get("phase") != "running":
                        raise GatewayError(f"{role.value} service failed to start")
            for role in (PeerRole.CLIENT, PeerRole.GATEWAY, PeerRole.ADAPTER):
                url = f"http://127.0.0.1:{ports[role]}/"
                print(f"{role.value}: {url}", flush=True)
                if open_browser:
                    webbrowser.open(url, new=2)
            print("Ready. Press Ctrl+C to stop all three WebUIs and services.", flush=True)
            while stop_file is None or not stop_file.exists():
                if any(process.poll() is not None for process, _ in processes):
                    raise GatewayError("a WebUI process exited")
                time.sleep(0.2)
        finally:
            # 先请求客户端退出，再停止网关、adapter，排空各自的安全会话和审计。
            for process, child_stop in reversed(processes):
                child_stop.touch()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    return 0
