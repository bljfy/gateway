"""Independent client, gateway and adapter commands using real secure sessions."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from collections.abc import Callable
from contextlib import aclosing
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import load_dotenv

from gateway.adapter import DEFAULT_MODEL, AdapterConfig, create_provider
from gateway.config import Limits
from gateway.contracts import GatewayError, InferenceRequest, PeerRole
from gateway.logging import console_logging, set_log_level
from gateway.runtime import (
    Runtime,
    load_runtime,
    prepare_demo,
    request_from_file,
    run_client,
    run_server,
)
from gateway.webui.launcher import launch
from gateway.webui.server import run_webui

DEMO_DIRECTORY = Path(".tools/demo")
NATIVE_MANIFEST = Path(".tools/gmssl/manifest.json")


def _write_stdout(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _infer(args: argparse.Namespace) -> int:
    request = InferenceRequest(
        request_id=args.request_id if args.request_id else uuid4(),
        model=args.model,
        prompt=args.prompt,
        retrieval_context=tuple(args.context) if args.context else (),
        max_output_tokens=args.max_output_tokens,
    )

    async def run() -> None:
        provider = create_provider(AdapterConfig.from_environment(), Limits())
        try:
            if args.stream:
                async with aclosing(provider.stream(request)) as iterator:
                    async for chunk in iterator:
                        _write_stdout(chunk.output)
                _write_stdout("\n")
            else:
                response = await provider.complete(request)
                _write_stdout(response.output + "\n")
        finally:
            await provider.close()

    asyncio.run(run())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guomi-gateway",
        description="Application workstream CLI for the national-crypto inference gateway.",
    )
    parser.add_argument("--quiet", action="store_true", help="hide console progress logs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    infer = subparsers.add_parser("infer", help="call the model provider directly")
    infer.add_argument("--prompt", required=True, help="prompt text")
    infer.add_argument("--model", default=DEFAULT_MODEL, help="model identifier")
    infer.add_argument(
        "--context",
        action="append",
        default=None,
        metavar="TEXT",
        help="retrieval context item; may be repeated",
    )
    infer.add_argument(
        "--request-id", default=None, type=UUID, help="request UUID; generated when absent"
    )
    infer.add_argument("--max-output-tokens", type=_positive_int, default=512, help="output budget")
    infer.add_argument("--stream", action="store_true", help="emit streaming chunks")
    infer.set_defaults(func=_infer)

    demo = subparsers.add_parser("init-demo", help="create fresh local demo identities and configs")
    demo.add_argument("--directory", default=DEMO_DIRECTORY, type=Path)
    demo.add_argument("--manifest", default=NATIVE_MANIFEST, type=Path)
    demo.add_argument("--gateway-port", type=_positive_int, default=18443)
    demo.add_argument("--adapter-port", type=_positive_int, default=19443)
    demo.add_argument("--metrics-port", type=_positive_int, default=19100)
    demo.set_defaults(func=_init_demo)
    for command in ("gateway", "adapter"):
        server = subparsers.add_parser(command, help=f"run the secure {command} listener")
        server.add_argument("--config", default=DEMO_DIRECTORY / f"{command}.json", type=Path)
        server.add_argument("--stop-file", type=Path)
        server.set_defaults(func=_server)
    client = subparsers.add_parser("client", help="send an inference request through the gateway")
    client.add_argument("--config", default=DEMO_DIRECTORY / "client.json", type=Path)
    _request_options(client, required=True)
    client.set_defaults(func=_client)
    quick = subparsers.add_parser("demo", help="initialize, run a secure request and stop services")
    quick.add_argument("--directory", default=DEMO_DIRECTORY, type=Path)
    quick.add_argument("--manifest", default=NATIVE_MANIFEST, type=Path)
    quick.add_argument("--gateway-port", type=_positive_int, default=18443)
    quick.add_argument("--adapter-port", type=_positive_int, default=19443)
    quick.add_argument("--metrics-port", type=_positive_int, default=19100)
    _request_options(quick, required=False)
    quick.set_defaults(func=_demo)
    ui = subparsers.add_parser("webui", help="run a local browser UI for one role")
    ui.add_argument("--role", choices=[role.value for role in PeerRole], required=True)
    ui.add_argument("--config", type=Path, help="local identity configuration")
    ui.add_argument("--port", type=_positive_int, help="loopback WebUI port")
    ui.add_argument("--stop-file", type=Path, help="exit gracefully when this file appears")
    ui.set_defaults(func=_webui)
    all_ui = subparsers.add_parser("webui-all", help="start all three local WebUIs and services")
    all_ui.add_argument("--directory", type=Path, default=DEMO_DIRECTORY)
    all_ui.add_argument("--manifest", type=Path, default=NATIVE_MANIFEST)
    all_ui.add_argument("--client-port", type=_positive_int, default=18080)
    all_ui.add_argument("--gateway-port", type=_positive_int, default=18081)
    all_ui.add_argument("--adapter-port", type=_positive_int, default=18082)
    all_ui.add_argument("--no-browser", action="store_true")
    all_ui.add_argument("--stop-file", type=Path)
    all_ui.set_defaults(func=_webui_all)
    return parser


def _webui(args: argparse.Namespace) -> int:
    role = PeerRole(args.role)
    run_webui(
        role,
        args.config or DEMO_DIRECTORY / f"{role.value}.json",
        args.port,
        stop_file=args.stop_file,
    )
    return 0


def _webui_all(args: argparse.Namespace) -> int:
    return launch(
        args.directory,
        args.manifest,
        {
            PeerRole.CLIENT: args.client_port,
            PeerRole.GATEWAY: args.gateway_port,
            PeerRole.ADAPTER: args.adapter_port,
        },
        open_browser=not args.no_browser,
        stop_file=args.stop_file,
    )


def _request_options(parser: argparse.ArgumentParser, *, required: bool) -> None:
    prompt = parser.add_mutually_exclusive_group(required=required)
    prompt.add_argument("--prompt", help="prompt text")
    prompt.add_argument("--prompt-file", type=Path, help="UTF-8 prompt file")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-output-tokens", type=_positive_int, default=512)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--request-id", type=UUID, help="request UUID; generated when absent")


def _request(args: argparse.Namespace) -> InferenceRequest:
    if args.prompt_file is not None:
        request = request_from_file(args.prompt_file, args.model, args.max_output_tokens)
        return InferenceRequest(
            args.request_id or request.request_id,
            request.model,
            request.prompt,
            max_output_tokens=request.max_output_tokens,
        )
    return InferenceRequest(
        args.request_id or uuid4(),
        args.model,
        args.prompt if args.prompt is not None else "你好",
        max_output_tokens=args.max_output_tokens,
    )


def _demo(args: argparse.Namespace) -> int:
    request = _request(args)
    if not args.directory.exists():
        prepare_demo(
            args.directory,
            args.manifest,
            gateway_port=args.gateway_port,
            adapter_port=args.adapter_port,
            metrics_port=args.metrics_port,
        )

    async def run() -> None:
        runtimes: list[Runtime] = []
        tasks: list[asyncio.Task[None]] = []
        try:
            for role in (PeerRole.ADAPTER, PeerRole.GATEWAY, PeerRole.CLIENT):
                runtimes.append(load_runtime(args.directory / f"{role.value}.json", role))
            set_log_level(runtimes[2].gateway.log_level)
            for runtime in runtimes[:2]:
                ready = asyncio.Event()
                server = asyncio.create_task(run_server(runtime, ready=ready.set))
                tasks.append(server)
                waiter = asyncio.create_task(ready.wait())
                try:
                    async with asyncio.timeout(10):
                        await asyncio.wait((server, waiter), return_when=asyncio.FIRST_COMPLETED)
                    if server.done():
                        server.result()
                        raise GatewayError("demo service stopped before readiness")
                finally:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
            await run_client(runtimes[2], request, stream=args.stream, write=_write_stdout)
        finally:
            for task in reversed(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for runtime in reversed(runtimes):
                await runtime.manager.close()

    asyncio.run(run())
    return 0


def _init_demo(args: argparse.Namespace) -> int:
    prepare_demo(
        args.directory,
        args.manifest,
        gateway_port=args.gateway_port,
        adapter_port=args.adapter_port,
        metrics_port=args.metrics_port,
    )
    print("demo configuration created")
    return 0


def _server(args: argparse.Namespace) -> int:
    runtime = load_runtime(args.config, PeerRole(args.command))
    set_log_level(runtime.gateway.log_level)

    async def run() -> None:
        stop = asyncio.Event()

        async def watch() -> None:
            while not stop.is_set():
                if args.stop_file is not None and args.stop_file.exists():
                    stop.set()
                    return
                await asyncio.sleep(0.1)

        watcher = asyncio.create_task(watch())
        try:
            await run_server(
                runtime, stop=stop, ready=lambda: print(f"{args.command} ready", flush=True)
            )
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(run())
    return 0


def _client(args: argparse.Namespace) -> int:
    runtime = load_runtime(args.config, PeerRole.CLIENT)
    set_log_level(runtime.gateway.log_level)
    request = _request(args)
    asyncio.run(run_client(runtime, request, stream=args.stream, write=_write_stdout))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    with console_logging(quiet=args.quiet):
        try:
            if args.command in {"adapter", "demo", "infer", "init-demo", "webui", "webui-all"}:
                load_dotenv(Path(".env"), override=False, interpolate=False)
            return func(args)
        except KeyboardInterrupt:
            return 130
        except (GatewayError, ValueError, OSError, TimeoutError, subprocess.CalledProcessError):
            print("command failed", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
