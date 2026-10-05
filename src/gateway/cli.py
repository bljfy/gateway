"""Command-line entry for the application workstream deliverables.

The deterministic simulator can be exercised directly. The full three-program
topology (client -> gateway -> simulator) is wired during integration once the
security-core (session) and gateway (routing/audit) lines land; the client and
simulator business layers are already available programmatically.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from uuid import UUID, uuid4

from gateway.contracts import InferenceRequest
from gateway.simulator import InferenceSimulator


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
