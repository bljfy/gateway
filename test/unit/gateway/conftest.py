"""Local B-line evidence report; never capture exception text or payload values."""

import hashlib
import json

import pytest

_results: list[dict[str, object]] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if "test/unit/gateway/" not in report.nodeid.replace("\\", "/"):
        return
    if report.when != "call" and report.outcome == "passed":
        return
    digest = hashlib.sha256(report.nodeid.encode()).hexdigest()
    _results.append(
        {
            "case_id": digest[:16],
            "test": report.nodeid.split("[")[0],
            "phase": report.when,
            "steps": "Run named contract-boundary test and its assertions",
            "synthetic_input_digest": digest,
            "mutation_location": "Named test fixture or configuration input",
            "expected": "All assertions pass without leaked payloads or orphaned resources",
            "actual": report.outcome,
            "duration_seconds": report.duration,
            "verdict": report.outcome,
        }
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    target = session.config.rootpath / ".tools" / "gateway-test-report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "scope": "B-line contract substitutes; not real cryptographic validation",
                "exitstatus": exitstatus,
                "cases": _results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _results.clear()
