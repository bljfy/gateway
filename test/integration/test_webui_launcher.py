"""一键启动真实三控制台、离线推理及正常／失败停机。"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from aiohttp import ClientSession

from gateway.contracts import PeerRole
from gateway.runtime import load_backend, prepare_demo
from gateway.webui.launcher import api, launch
from test.integration.test_topology import ports


@pytest.mark.skipif(os.name != "nt", reason="Windows native loader PATH")
def test_repeated_native_load_keeps_windows_path_bounded(tmp_path, backend, monkeypatch):
    directory = str(Path(os.environ["GMSSL_LIBRARY"]).parent)
    monkeypatch.setenv(
        "PATH", directory.upper() + os.pathsep + directory + os.pathsep + os.environ["PATH"]
    )
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
    load_backend(manifest)
    selected = os.environ["PATH"]
    for _ in range(5):
        load_backend(manifest)
        assert os.environ["PATH"] == selected
    normalized = os.path.normcase(os.path.normpath(directory))
    assert (
        sum(
            os.path.normcase(os.path.normpath(item.strip('"'))) == normalized
            for item in selected.split(os.pathsep)
        )
        == 1
    )
    # The loader must leave enough environment space to create child processes.
    subprocess.run([sys.executable, "-c", "pass"], check=True)


@pytest.fixture
def launcher_setup(tmp_path, backend):
    manifest = tmp_path / "native.json"
    manifest.write_text(
        json.dumps({"library": os.environ["GMSSL_LIBRARY"], "sha256": os.environ["GMSSL_SHA256"]}),
        encoding="utf-8",
    )
    directory = tmp_path / "demo"
    business = ports()
    prepare_demo(
        directory,
        manifest,
        gateway_port=business[0],
        adapter_port=business[1],
        metrics_port=business[2],
    )
    ui_ports = dict(zip(PeerRole, ports(), strict=True))
    return directory, manifest, business, ui_ports


def free_ports(values):
    for port in values:
        with socket.create_server(("127.0.0.1", port)):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [False, True])
async def test_launcher_keeps_setup_available_without_model_key(launcher_setup, tmp_path, remote):
    directory, manifest, business, ui_ports = launcher_setup
    stop_file = tmp_path / "setup.stop"
    environment = os.environ.copy()
    environment["DEEPSEEK_API_KEY"] = ""
    environment["WEBUI_PASSWORD"] = "synthetic-remote-password"
    origins = {}
    for role in PeerRole:
        origins[role] = f"https://{role.value}.example.com" if remote else ""
        environment[f"WEBUI_{role.value.upper()}_ORIGIN"] = origins[role]
    command = [
        sys.executable,
        "-m",
        "gateway.cli",
        "webui-all",
        "--directory",
        str(directory),
        "--manifest",
        str(manifest),
        "--no-browser",
        "--stop-file",
        str(stop_file),
    ]
    for role, port in ui_ports.items():
        command.extend([f"--{role.value}-port", str(port)])
    with (tmp_path / "setup.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, env=environment)
        try:
            deadline = time.monotonic() + 25
            while True:
                assert process.poll() is None, (tmp_path / "setup.log").read_text()
                try:
                    from aiohttp import encode_basic_auth

                    async with ClientSession() as session:
                        states = {}
                        for role, port in ui_ports.items():
                            headers = {"X-WebUI-Request": "1"}
                            if remote:
                                headers.update(
                                    Host=f"{role.value}.example.com",
                                    Origin=origins[role],
                                    Authorization=encode_basic_auth(
                                        "admin", environment["WEBUI_PASSWORD"]
                                    ),
                                )
                            async with session.get(
                                f"http://127.0.0.1:{port}/api/state", headers=headers
                            ) as response:
                                states[role] = await response.json()
                        if all(s.get("config_loaded") for s in states.values()):
                            assert states[PeerRole.ADAPTER]["phase"] == "stopped"
                            assert states[PeerRole.GATEWAY]["phase"] == "running"
                            break
                except OSError:
                    pass
                assert time.monotonic() < deadline
                await asyncio.sleep(0.1)
        finally:
            stop_file.touch()
            try:
                await asyncio.to_thread(process.wait, 25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
    assert process.returncode == 0
    free_ports([*business, *ui_ports.values()])


def test_launcher_rejects_occupied_ui_without_initializing(tmp_path):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        occupied = listener.getsockname()[1]
        choices = dict(zip(PeerRole, ports(), strict=True))
        choices[PeerRole.CLIENT] = occupied
        directory = tmp_path / "not-created"
        with pytest.raises(OSError):
            launch(directory, tmp_path / "missing.json", choices, open_browser=False)
        assert not directory.exists()


def test_launcher_rolls_back_when_gateway_business_port_is_occupied(launcher_setup):
    directory, manifest, business, ui_ports = launcher_setup
    with socket.create_server(("127.0.0.1", business[0])):
        with pytest.raises(Exception, match="WebUI operation failed"):
            launch(directory, manifest, ui_ports, open_browser=False)
    free_ports([*business, *ui_ports.values()])
    assert not list(directory.glob("launcher-*"))


def test_launcher_keyboard_interrupt_cleans_all_owned_processes(launcher_setup, monkeypatch):
    directory, manifest, business, ui_ports = launcher_setup
    original_sleep = time.sleep

    def interrupt_when_ready(duration):
        if duration == 0.2:
            raise KeyboardInterrupt
        original_sleep(duration)

    monkeypatch.setattr("gateway.webui.launcher.time.sleep", interrupt_when_ready)
    with pytest.raises(KeyboardInterrupt):
        launch(directory, manifest, ui_ports, open_browser=False)
    free_ports([*business, *ui_ports.values()])
    assert not list(directory.glob("launcher-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_launcher_three_process_inference_and_graceful_stop(launcher_setup, tmp_path, stream):
    directory, manifest, business, ui_ports = launcher_setup
    stop_file = tmp_path / "all.stop"
    command = [
        sys.executable,
        "-m",
        "gateway.cli",
        "webui-all",
        "--directory",
        str(directory),
        "--manifest",
        str(manifest),
        "--no-browser",
        "--stop-file",
        str(stop_file),
    ]
    for role, port in ui_ports.items():
        command.extend([f"--{role.value}-port", str(port)])
    with (tmp_path / "launcher.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            while True:
                assert process.poll() is None, (tmp_path / "launcher.log").read_text()
                try:
                    states = {
                        role: await asyncio.to_thread(api, port, "state")
                        for role, port in ui_ports.items()
                    }
                    if all(state["config_loaded"] for state in states.values()):
                        assert states[PeerRole.ADAPTER]["phase"] == "running"
                        assert states[PeerRole.GATEWAY]["phase"] == "running"
                        break
                except OSError:
                    pass
                assert time.monotonic() < deadline, "launcher readiness timed out"
                await asyncio.sleep(0.1)
            origin = f"http://127.0.0.1:{ui_ports[PeerRole.CLIENT]}"
            headers = {"Origin": origin, "X-WebUI-Request": "1"}
            async with ClientSession() as session:
                async with session.ws_connect(origin + "/api/ws", origin=origin) as ws:
                    ready = await ws.receive_json(timeout=5)
                    response = await session.post(
                        origin + "/api/infer",
                        headers=headers,
                        json={
                            "owner": ready["owner"],
                            "prompt": "launcher synthetic prompt",
                            "model": "deepseek-flash",
                            "stream": stream,
                            "max_output_tokens": 32,
                            "retrieval_context": [],
                        },
                    )
                    assert response.status == 202
                    output = ""
                    while True:
                        item = await ws.receive_json(timeout=10)
                        if item["type"] == "output":
                            output += item["output"]
                        if item["type"] == "finished":
                            assert item["result"] == "completed"
                            assert output
                            break
            stop_file.touch()
            assert await asyncio.to_thread(process.wait, 25) == 0
        finally:
            stop_file.touch()
            try:
                await asyncio.to_thread(process.wait, 25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
    free_ports([*business, *ui_ports.values()])
    assert not list(directory.glob("launcher-*"))
