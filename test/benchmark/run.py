"""Run three-process, two-hop benchmarks: python -m test.benchmark.run --help."""

import argparse
import asyncio
import hashlib
import json
import math
import multiprocessing as mp
import os
import platform
import ssl
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import psutil

from gateway.audit import PrivacyAudit
from gateway.client import InferenceClient
from gateway.codec import encode_request, encode_response
from gateway.config import GatewayConfig
from gateway.contracts import InferenceRequest, InferenceResponse, PeerIdentity, PeerRole
from gateway.crypto import GmSSLBackend
from gateway.server.relay import GatewayServer
from gateway.session import LocalIdentity, SecuritySession, SecuritySessionManager, TrustRecord
from gateway.simulator import InferenceSimulator
from test.benchmark.transport import TransportManager, certificates


class Echo(InferenceSimulator):
    @staticmethod
    def _output(request):
        return request.prompt


class DisabledAudit:
    def publish(self, event):
        return True


class MeasuredNative(SecuritySessionManager):
    """Keep real admission/handshake; time TCP and security on the same connection."""

    async def _open(self, peer):
        started = time.perf_counter()
        connected = None
        session = None
        trust = self._trust(peer, outbound=True)
        try:
            async with asyncio.timeout(self.policy.handshake_timeout_seconds):
                reader, writer = await asyncio.open_connection(*trust.address)
                connected = time.perf_counter()
                session = SecuritySession(self, reader, writer, peer)
                self._sessions.add(session)
                await session.handshake()
            return session
        except BaseException:
            if session:
                session._abort()
            raise
        finally:
            finished = time.perf_counter()
            self.timings.append(
                {
                    "tcp_ms": (connected - started) * 1000 if connected else None,
                    "security_ready_ms": (finished - connected) * 1000 if connected else None,
                    "secure_open_ms": (finished - started) * 1000,
                }
            )


def manager(mode, role, identities, directory, port, manifest):
    local, _ = identities[role]
    incoming = {"gateway": "client", "simulator": "gateway"}.get(role)
    outgoing = {"client": "gateway", "gateway": "simulator"}.get(role)
    if mode != "gm":
        return TransportManager(
            local.peer,
            identities[incoming][0].peer if incoming else None,
            identities[outgoing][0].peer if outgoing else None,
            port,
            directory,
            mode,
        )
    backend = GmSSLBackend(Path(manifest["library"]), sha256=manifest["sha256"])
    trusted = {}
    for remote in (incoming, outgoing):
        if remote:
            peer, keys = identities[remote]
            trusted[peer.peer.peer_id] = TrustRecord(
                peer.peer, *keys, address=("127.0.0.1", port) if remote == outgoing else None
            )
    mgr = MeasuredNative(backend, local, trusted)
    mgr.timings = []
    return mgr


async def server(mode, role, identities, directory, upstream_port, manifest, audit_on, pipe, stop):
    mgr = manager(mode, role, identities, directory, upstream_port, manifest)
    owned = set()
    errors = Counter()
    audit = PrivacyAudit(capacity=8192) if audit_on else DisabledAudit()
    gateway = GatewayServer(mgr, GatewayConfig(identities["simulator"][0].peer), audit)
    simulator = Echo()
    audit_path = directory / f"{mode}-{audit_on}-{role}-audit.jsonl"
    log = audit_path.open("w", encoding="utf-8")

    async def handle(reader, writer):
        task = asyncio.current_task()
        owned.add(task)
        try:
            if role == "gateway":
                await gateway.handle(reader, writer)
            else:
                session = await mgr.accept(reader, writer)
                try:
                    await simulator.serve(session)
                finally:
                    await session.close()
        except Exception as exc:
            # Peer shutdown can be a transport EOF, never retain its message.
            errors[type(exc).__name__] += 1
        finally:
            writer.close()
            owned.discard(task)

    kwargs = {}
    if mode == "tls":
        kwargs = {"ssl": mgr.server_context, "ssl_handshake_timeout": 5}
    listener = await asyncio.start_server(handle, "127.0.0.1", 0, **kwargs)
    pipe.send(listener.sockets[0].getsockname()[1])
    try:
        while not stop.is_set():
            if role == "gateway" and audit_on:
                audit.drain(log.write, limit=8192)
                log.flush()
            await asyncio.sleep(0.01)
    finally:
        listener.close()
        await listener.wait_closed()
        if role == "gateway":
            await gateway.close()
        else:
            await mgr.close()
        for task in tuple(owned):
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)
        if role == "gateway" and audit_on:
            audit.drain(log.write, limit=8192)
        log.close()
        # Persist bulk samples to disk: a large pipe reply can block process exit.
        timing_path = directory / f"{mode}-{audit_on}-{role}-timings.json"
        timing_path.write_text(json.dumps(mgr.timings), encoding="utf-8")
        pipe.send(
            {
                "errors": errors,
                "audit_healthy": audit.healthy if audit_on else True,
                "audit_rejected": audit.rejected if audit_on else 0,
                "active_sessions": len(mgr._sessions if mode == "gm" else mgr.sessions),
                "cipher_suites": sorted(mgr.negotiated) if mode == "tls" else [],
                "open_errors": [
                    {"type": key[0], "winerror": key[1], "errno": key[2], "count": value}
                    for key, value in mgr.open_errors.items()
                ]
                if mode != "gm"
                else [],
            }
        )


def worker(*args):
    asyncio.run(server(*args))


def start(mode, role, identities, directory, port, manifest, audit_on):
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    stop = ctx.Event()
    process = ctx.Process(
        target=worker,
        name=role,
        args=(mode, role, identities, directory, port, manifest, audit_on, child, stop),
    )
    process.start()
    child.close()
    if not parent.poll(30):
        stop.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        raise RuntimeError("benchmark server did not become ready")
    return process, parent, stop, parent.recv()


async def stop_services(services):
    """Stop every owned child before reporting a failure of any one child."""
    for _, _, stop, _ in services:
        stop.set()
    results, problems = [], []
    for process, pipe, _, _ in reversed(services):
        try:
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
                problems.append(f"{process.name}: forced termination")
            if process.exitcode != 0 or not pipe.poll(1):
                problems.append(f"{process.name}: missing clean shutdown")
            else:
                results.append((process.name, pipe.recv()))
        except Exception as exc:
            problems.append(f"{process.name}: {type(exc).__name__}")
        finally:
            pipe.close()
    if problems:
        raise RuntimeError("; ".join(problems))
    return results


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def distribution(values):
    return {
        "count": len(values),
        "mean_ms": sum(values) / len(values) if values else None,
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
    }


async def measure(mgr, peer, size, concurrency, stream, count, seconds, processes):
    request = InferenceRequest(uuid4(), "echo", "x" * size, max_output_tokens=size)
    samples, latencies, first_chunks, failures = [], [], [], []
    completed = attempts = 0
    clients = []
    initialization = []
    # Warm up and establish persistent inbound connections outside the interval.
    for _ in range(concurrency):
        initial_started = time.perf_counter()
        client = InferenceClient(await mgr.open(peer))
        assert (await client.complete(request)).output == request.prompt
        initialization.append((time.perf_counter() - initial_started) * 1000)
        clients.append(client)
    cpu_before = [p.cpu_times() for p in processes]
    started = time.perf_counter()
    deadline = started + seconds
    sampling = True

    async def sample():
        while sampling:
            samples.append(
                {
                    "elapsed": time.perf_counter() - started,
                    "rss": [p.memory_info().rss for p in processes],
                }
            )
            await asyncio.sleep(0.1)

    async def load(client):
        nonlocal attempts, completed
        while attempts < count or time.perf_counter() < deadline:
            attempts += 1
            rid = uuid4()
            item = InferenceRequest(rid, "echo", request.prompt, max_output_tokens=size)
            begin = time.perf_counter()
            try:
                async with asyncio.timeout(120):
                    if stream:
                        chunks = []
                        async for chunk in client.stream(item):
                            if not chunks:
                                first_chunks.append((time.perf_counter() - begin) * 1000)
                            chunks.append(chunk)
                        assert chunks and sum(c.is_final for c in chunks) == 1
                        assert "".join(c.output for c in chunks) == request.prompt
                    else:
                        assert (await client.complete(item)).output == request.prompt
                completed += 1
                latencies.append((time.perf_counter() - begin) * 1000)
            except Exception as exc:
                failures.append(
                    {
                        "type": type(exc).__name__,
                        "duration_ms": (time.perf_counter() - begin) * 1000,
                    }
                )
                # Stop this connection; never count rapid retries on a dead transport.
                return

    sampler = asyncio.create_task(sample())
    try:
        await asyncio.gather(*(load(client) for client in clients))
    finally:
        wall = time.perf_counter() - started
        cpu_after = [p.cpu_times() for p in processes]
        sampling = False
        await sampler
        for client in clients:
            await client._session.close()
    cpu = [
        (b.user + b.system - a.user - a.system) / wall * 100
        for a, b in zip(cpu_before, cpu_after, strict=True)
    ]
    return {
        "payload_bytes": size,
        "concurrency": concurrency,
        "stream": stream,
        "wall_seconds": wall,
        "attempts": attempts,
        "successes": completed,
        "failures": failures,
        "failure_rate": len(failures) / attempts if attempts else 1,
        "qualified_duration_and_count": wall >= seconds and completed >= count,
        "latency": distribution(latencies),
        "first_business_initialization": distribution(initialization),
        "first_business_initialization_samples_ms": initialization,
        "first_chunk": distribution(first_chunks),
        "request_mib_s": completed * size / wall / 2**20,
        "bidirectional_mib_s": completed * size * 2 / wall / 2**20,
        "cpu_percent_by_role": cpu,
        "cpu_machine_percent": sum(cpu) / (os.cpu_count() or 1),
        "rss_peak_by_role": [max(s["rss"][i] for s in samples) for i in range(3)],
        "rss_total_peak": max(sum(s["rss"]) for s in samples),
        "latency_samples_ms": latencies,
        "first_chunk_samples_ms": first_chunks,
        "resource_samples": samples,
        "request_encoded_bytes": len(encode_request(request, stream=stream)),
        "response_encoded_bytes": None
        if stream
        else len(encode_response(InferenceResponse(request.request_id, request.prompt))),
    }


async def run(args, output):
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    backend = GmSSLBackend(Path(manifest["library"]), sha256=manifest["sha256"])
    identities = {}
    for role in PeerRole:
        signing, signing_pub = backend.generate_keypair()
        encryption, encryption_pub = backend.generate_keypair()
        identities[role.value] = (
            LocalIdentity(PeerIdentity(role.value, role), signing, encryption),
            (signing_pub, encryption_pub),
        )
    with tempfile.TemporaryDirectory(prefix="gateway-benchmark-") as temp:
        directory = Path(temp)
        certificates(directory)
        for round_number in range(args.rounds):
            # Rotate ordering to expose run-order effects over multiple rounds.
            modes = (
                args.modes[round_number % len(args.modes) :]
                + args.modes[: round_number % len(args.modes)]
            )
            for mode in modes:
                for audit_on in args.audit:
                    services = []
                    mgr = None
                    try:
                        sim = start(mode, "simulator", identities, directory, 0, manifest, audit_on)
                        services.append(sim)
                        gw = start(
                            mode, "gateway", identities, directory, sim[3], manifest, audit_on
                        )
                        services.append(gw)
                        mgr = manager(mode, "client", identities, directory, gw[3], manifest)
                        peer = identities["gateway"][0].peer
                        handshake_rows = []
                        for hop, role, port, target in (
                            ("A", "client", gw[3], "gateway"),
                            ("B", "gateway", sim[3], "simulator"),
                        ):
                            hm = manager(mode, role, identities, directory, port, manifest)
                            try:
                                for index in range(args.handshakes + 5):
                                    begin = time.perf_counter()
                                    error = None
                                    try:
                                        session = await hm.open(identities[target][0].peer)
                                        elapsed = (time.perf_counter() - begin) * 1000
                                        await session.close()
                                    except Exception as exc:
                                        elapsed = (time.perf_counter() - begin) * 1000
                                        error = type(exc).__name__
                                    if index >= 5:
                                        handshake_rows.append(
                                            {
                                                "hop": hop,
                                                **hm.timings[-1],
                                                "secure_open_ms": elapsed,
                                                "error": error,
                                            }
                                        )
                            finally:
                                await hm.close()
                        output["handshakes"].append(
                            {
                                "mode": mode,
                                "audit": audit_on,
                                "round": round_number,
                                "samples": handshake_rows,
                            }
                        )
                        processes = [
                            psutil.Process(),
                            psutil.Process(gw[0].pid),
                            psutil.Process(sim[0].pid),
                        ]
                        for size in args.sizes:
                            for concurrency in args.concurrency:
                                for stream in args.stream:
                                    result = await measure(
                                        mgr,
                                        peer,
                                        size,
                                        concurrency,
                                        stream,
                                        args.requests,
                                        args.seconds,
                                        processes,
                                    )
                                    result.update(mode=mode, audit=audit_on, round=round_number)
                                    output["cases"].append(result)
                                    print(
                                        f"{mode} audit={audit_on} bytes={size} "
                                        f"concurrent={concurrency} "
                                        f"stream={stream}: {result['successes']} ok, "
                                        f"{len(result['failures'])} failed",
                                        flush=True,
                                    )
                                    save(args.output, output)
                    finally:
                        try:
                            if mgr:
                                await mgr.close()
                        finally:
                            shutdown_results = await stop_services(services)
                        for role, evidence in shutdown_results:
                            timing_path = directory / f"{mode}-{audit_on}-{role}-timings.json"
                            output["shutdown"].append(
                                {
                                    "mode": mode,
                                    "audit": audit_on,
                                    "round": round_number,
                                    **evidence,
                                    "open_timings": json.loads(timing_path.read_text()),
                                }
                            )
                        if audit_on:
                            audit_file = directory / f"{mode}-{audit_on}-gateway-audit.jsonl"
                            rows = [
                                json.loads(line) for line in audit_file.read_text().splitlines()
                            ]
                            output["audit_evidence"].append(
                                {
                                    "mode": mode,
                                    "round": round_number,
                                    "started": sum(
                                        r["event_code"] == "request_started" for r in rows
                                    ),
                                    "finished_ok": sum(
                                        r["event_code"] == "request_finished"
                                        and r["result"] == "ok"
                                        for r in rows
                                    ),
                                    "synthetic_marker_leaks": audit_file.read_text().count(
                                        "x" * 32
                                    ),
                                }
                            )
                        save(args.output, output)


def save(path, output):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


def acceptance_problems(output, *, formal=False):
    """Judge saved evidence, including audits and cleanup, not just success latency."""
    problems = []
    command = output["environment"]["command"]
    expected_cases = (
        command["rounds"]
        * len(command["modes"])
        * len(command["audit"])
        * len(command["sizes"])
        * len(command["concurrency"])
        * len(command["stream"])
    )
    if not output["complete"] or len(output["cases"]) != expected_cases:
        problems.append("incomplete workload matrix")
    for case in output["cases"]:
        if (
            case["failures"]
            or case["successes"] < command["requests"]
            or case["wall_seconds"] < command["seconds"]
        ):
            problems.append("workload failure or insufficient budget")
    expected_groups = command["rounds"] * len(command["modes"]) * len(command["audit"])
    if len(output["handshakes"]) != expected_groups:
        problems.append("missing handshake groups")
    for group in output["handshakes"]:
        for hop in ("A", "B"):
            rows = [row for row in group["samples"] if row["hop"] == hop]
            if len(rows) != command["handshakes"] or any(row["error"] for row in rows):
                problems.append("handshake failure or insufficient count")
    if len(output["shutdown"]) != expected_groups * 2:
        problems.append("missing shutdown evidence")
    for row in output["shutdown"]:
        if not row["audit_healthy"] or row["audit_rejected"] or row["active_sessions"]:
            problems.append("unhealthy audit or remaining sessions")
    expected_audits = command["rounds"] * len(command["modes"]) * command["audit"].count(1)
    if len(output["audit_evidence"]) != expected_audits:
        problems.append("missing audit evidence")
    for audit in output["audit_evidence"]:
        expected = sum(
            case["successes"] + case["concurrency"]
            for case in output["cases"]
            if case["audit"] and case["mode"] == audit["mode"] and case["round"] == audit["round"]
        )
        if (
            audit["started"] != expected
            or audit["finished_ok"] != expected
            or audit["synthetic_marker_leaks"]
        ):
            problems.append("audit loss or plaintext leak")
    if formal and (
        command["handshakes"] < 100
        or command["requests"] < 1000
        or command["seconds"] < 30
        or not {"gm", "tls"}.issubset(command["modes"])
        or set(command["sizes"]) != {1024, 16384}
        or set(command["concurrency"]) != {1, 10, 50, 100}
        or set(command["audit"]) != {0, 1}
        or set(command["stream"]) != {0, 1}
    ):
        problems.append("formal comparison budget or coverage insufficient")
    return sorted(set(problems))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/performance.json"))
    parser.add_argument(
        "--modes", nargs="+", choices=["gm", "tls", "plain"], default=["gm", "tls", "plain"]
    )
    parser.add_argument("--sizes", nargs="+", type=int, default=[1024, 16384])
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 10, 50, 100])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--handshakes", type=int, default=100)
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--audit", nargs="+", type=int, choices=[0, 1], default=[0, 1])
    parser.add_argument("--stream", nargs="+", type=int, choices=[0, 1], default=[0, 1])
    args = parser.parse_args()
    if (
        min(
            *args.sizes,
            *args.concurrency,
            args.rounds,
            args.handshakes,
            args.requests,
            args.seconds,
        )
        <= 0
    ):
        parser.error("measurement budgets must be positive")
    output = {
        "environment": {
            "python": platform.python_version(),
            "openssl": ssl.OPENSSL_VERSION,
            "platform": platform.platform(),
            "cpu": platform.processor(),
            "logical_cpus": os.cpu_count(),
            "rss_sample_seconds": 0.1,
            "roles": ["client", "gateway", "simulator"],
            "network": "IPv4 loopback TCP",
            "gmssl": json.loads(args.manifest.read_text()),
            "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "lock_sha256": hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest(),
            "command": vars(args) | {"manifest": str(args.manifest), "output": str(args.output)},
        },
        "handshakes": [],
        "cases": [],
        "shutdown": [],
        "audit_evidence": [],
        "complete": False,
        "source_files_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [*Path("src").rglob("*.py"), *Path("test/benchmark").glob("*.py")]
        },
    }
    try:
        asyncio.run(run(args, output))
        output["complete"] = True
        output["acceptance_problems"] = acceptance_problems(output)
        output["performance_acceptance_problems"] = acceptance_problems(output, formal=True)
    finally:
        save(args.output, output)
    if output["acceptance_problems"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
