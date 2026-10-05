"""Mandatory native backend and redacted integrated test evidence."""

import json
import os
from pathlib import Path

import pytest

from gateway.crypto import GmSSLBackend

_results: list[dict[str, object]] = []


@pytest.fixture(scope="session")
def backend() -> GmSSLBackend:
    library, digest = os.environ.get("GMSSL_LIBRARY"), os.environ.get("GMSSL_SHA256")
    if not library or not digest:
        pytest.fail("Verified GmSSL native library and checksum are required")
    return GmSSLBackend(Path(library), sha256=digest)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if "/integration/" not in report.nodeid.replace("\\", "/"):
        return
    if report.when == "call" or (report.when == "setup" and report.failed):
        _results.append(
            {
                "case_id": report.nodeid,
                "steps": "Run real SM2/SM4-GCM two-link or three-process test",
                "input_summary": "Fixed synthetic payloads and runtime-generated native keys",
                "mutation_position": report.nodeid.rsplit("::", 1)[-1],
                "expected": "Reject invalid traffic; protect plaintext; release resources",
                "actual": report.outcome,
                "duration_seconds": report.duration,
                "verdict": report.outcome,
            }
        )


def pytest_sessionfinish(session: pytest.Session) -> None:
    path = session.config.rootpath / "artifacts/integration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _results.clear()
