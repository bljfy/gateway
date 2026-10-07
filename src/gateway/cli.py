"""Independent client, gateway and simulator commands using real secure sessions."""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from gateway.contracts import GatewayError, InferenceRequest, PeerRole
from gateway.runtime import (
    Runtime,
    load_runtime,
    prepare_demo,
    request_from_file,
    run_client,
    run_server,
)
from gateway.simulator import InferenceSimulator

DEMO_DIRECTORY = Path(".tools/demo")
NATIVE_MANIFEST = Path(".tools/gmssl/manifest.json")


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _simulate(args: argparse.Namespace) -> int:
    request = InferenceRequest(
        request_id=args.request_id if args.request_id else uuid4(),
        model=args.model,
        prompt=args.prompt,
        retrieval_context=tuple(args.context) if args.context else (),
        max_output_tokens=args.max_output_tokens,
    )
    simulator = InferenceSimulator()

    async def run() -> None:
        if args.stream:
            async for chunk in simulator.stream(request):
                sys.stdout.write(chunk.output)
            sys.stdout.write("\n")
        else:
            response = await simulator.complete(request)
            sys.stdout.write(response.output + "\n")

    asyncio.run(run())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guomi-gateway",
        description="Application workstream CLI for the national-crypto inference gateway.",
    )
    parser.add_argument("--quiet", action="store_true", help="hide console progress logs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    simulate = subparsers.add_parser("simulate", help="run the deterministic mock inference")
    simulate.add_argument("--prompt", required=True, help="prompt text for the mock model")
    simulate.add_argument("--model", default="mock-model", help="model identifier")
    simulate.add_argument(
        "--context",
        action="append",
        default=None,
        metavar="TEXT",
        help="retrieval context item; may be repeated",
    )
    simulate.add_argument(
        "--request-id", default=None, type=UUID, help="request UUID; generated when absent"
    )
    simulate.add_argument(
        "--max-output-tokens", type=_positive_int, default=512, help="output budget"
    )
    simulate.add_argument("--stream", action="store_true", help="emit streaming chunks")
    simulate.set_defaults(func=_simulate)

    demo = subparsers.add_parser("init-demo", help="create fresh local demo identities and configs")
    demo.add_argument("--directory", default=DEMO_DIRECTORY, type=Path)
    demo.add_argument("--manifest", default=NATIVE_MANIFEST, type=Path)
    demo.add_argument("--gateway-port", type=_positive_int, default=18443)
    demo.add_argument("--simulator-port", type=_positive_int, default=19443)
    demo.add_argument("--metrics-port", type=_positive_int, default=19100)
    demo.set_defaults(func=_init_demo)
    for command in ("gateway", "simulator"):
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
    quick.add_argument("--simulator-port", type=_positive_int, default=19443)
    quick.add_argument("--metrics-port", type=_positive_int, default=19100)
    _request_options(quick, required=False)
    quick.set_defaults(func=_demo)
    return parser


def _request_options(parser: argparse.ArgumentParser, *, required: bool) -> None:
    prompt = parser.add_mutually_exclusive_group(required=required)
    prompt.add_argument("--prompt", help="prompt text")
    prompt.add_argument("--prompt-file", type=Path, help="UTF-8 prompt file")
    parser.add_argument("--model", default="mock-model")
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
            simulator_port=args.simulator_port,
            metrics_port=args.metrics_port,
        )

    async def run() -> None:
        runtimes: list[Runtime] = []
        tasks: list[asyncio.Task[None]] = []
        try:
            for role in (PeerRole.SIMULATOR, PeerRole.GATEWAY, PeerRole.CLIENT):
                runtimes.append(load_runtime(args.directory / f"{role.value}.json", role))
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
            await run_client(runtimes[2], request, stream=args.stream, write=sys.stdout.write)
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
        simulator_port=args.simulator_port,
        metrics_port=args.metrics_port,
    )
    print("demo configuration created")
    return 0


def _server(args: argparse.Namespace) -> int:
    runtime = load_runtime(args.config, PeerRole(args.command))

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
    request = _request(args)
    asyncio.run(run_client(runtime, request, stream=args.stream, write=sys.stdout.write))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    logger = logging.getLogger("gateway")
    previous_level = logger.level
    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    handler.setLevel(logging.ERROR if args.quiet else logging.INFO)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        return func(args)
    except KeyboardInterrupt:
        return 130
    except (GatewayError, ValueError, OSError, TimeoutError, subprocess.CalledProcessError):
        print("command failed", file=sys.stderr)
        return 1
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        handler.close()


if __name__ == "__main__":
    sys.exit(main())
