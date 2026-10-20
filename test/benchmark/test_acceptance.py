"""Corrupted or incomplete measurement evidence must fail acceptance."""

from types import SimpleNamespace

import pytest

from test.benchmark.run import acceptance_problems, stop_services


def evidence():
    return {
        "environment": {
            "command": {
                "rounds": 1,
                "modes": ["gm"],
                "audit": [1],
                "sizes": [1024],
                "concurrency": [1],
                "stream": [0],
                "handshakes": 1,
                "requests": 1,
                "seconds": 1,
            }
        },
        "complete": True,
        "cases": [
            {
                "failures": [],
                "successes": 1,
                "wall_seconds": 1,
                "audit": 1,
                "mode": "gm",
                "round": 0,
                "concurrency": 1,
            }
        ],
        "handshakes": [{"samples": [{"hop": "A", "error": None}, {"hop": "B", "error": None}]}],
        "shutdown": [{"audit_healthy": True, "audit_rejected": 0, "active_sessions": 0}] * 2,
        "audit_evidence": [
            {"mode": "gm", "round": 0, "started": 2, "finished_ok": 2, "synthetic_marker_leaks": 0}
        ],
    }


@pytest.mark.parametrize(
    "fault",
    [
        "incomplete",
        "short",
        "audit_loss",
        "leak",
        "session",
        "audit_unhealthy",
        "handshake",
        "shutdown",
        "matrix",
        "sampling_gap",
    ],
)
def test_reject_damaged_evidence(fault):
    data = evidence()
    assert not acceptance_problems(data)
    assert acceptance_problems(data, formal=True)
    if fault == "incomplete":
        data["complete"] = False
    elif fault == "short":
        data["cases"][0]["wall_seconds"] = 0.5
    elif fault == "audit_loss":
        data["audit_evidence"][0]["finished_ok"] = 1
    elif fault == "leak":
        data["audit_evidence"][0]["synthetic_marker_leaks"] = 1
    elif fault == "session":
        data["shutdown"][0]["active_sessions"] = 1
    elif fault == "audit_unhealthy":
        data["shutdown"][0]["audit_healthy"] = False
    elif fault == "handshake":
        data["handshakes"][0]["samples"][0]["error"] = "AuthenticationError"
    elif fault == "shutdown":
        data["shutdown"].pop()
    elif fault == "matrix":
        data["cases"].clear()
    elif fault == "sampling_gap":
        data["cases"][0]["max_resource_gap_seconds"] = 2
    assert acceptance_problems(data)


@pytest.mark.asyncio
async def test_failed_child_does_not_skip_other_cleanup():
    stopped, joined, closed = [], [], []
    services = []
    for name, status in (("adapter", 0), ("gateway", 1)):
        process = SimpleNamespace(
            name=name,
            exitcode=status,
            join=lambda timeout, name=name: joined.append(name),
            is_alive=lambda: False,
        )
        pipe = SimpleNamespace(
            poll=lambda timeout: True,
            recv=lambda: {},
            close=lambda name=name: closed.append(name),
        )
        stop = SimpleNamespace(set=lambda name=name: stopped.append(name))
        services.append((process, pipe, stop, 0))
    with pytest.raises(RuntimeError, match="gateway"):
        await stop_services(services)
    assert stopped == ["adapter", "gateway"]
    assert joined == closed == ["gateway", "adapter"]


def test_resumption_and_pool_must_be_proven():
    data = evidence()
    data["environment"]["command"]["modes"] = ["tls-combined"]
    data["handshakes"][0]["mode"] = "tls-combined"
    assert "resumption not demonstrated" in acceptance_problems(data)
    assert "connection reuse not demonstrated" in acceptance_problems(data)
    for row in data["handshakes"][0]["samples"]:
        row["resumed"] = True
    data["shutdown"] = [dict(row) for row in data["shutdown"]]
    data["shutdown"][0].update(
        mode="tls-combined", round=0, audit=1, pool_counts={"pool_hit_total": 1}
    )
    assert not acceptance_problems(data)
