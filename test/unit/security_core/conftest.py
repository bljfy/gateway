"""Real native backend is mandatory; no crypto substitutes or silent skips."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from gateway.crypto import GmSSLBackend

RESULTS: list[dict[str, str | float | list[str]]] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if "/security_core/" not in report.nodeid.replace("\\", "/"):
        return
    if report.when == "call" or (report.when == "setup" and report.failed):
        case = report.nodeid.rsplit("::", 1)[-1]
        mutation = "none"
        steps = ["加载校验后的真实 GmSSL 后端", "执行用例中的合成输入和断言"]
        expected = "所有内容、错误类别、状态和资源断言通过"
        if "tampering" in case:
            mutation = "wire_byte_offset=" + case.rsplit("[", 1)[-1].rstrip("]")
            steps = [
                "真实握手建立单链路",
                "在指定偏移翻转记录的一位",
                "调用接收并检查状态、计数和密钥清理",
            ]
            expected = "拒绝返回明文，接收计数不增长，会话关闭且密钥释放"
        elif "handshake_modification" in case:
            mutation = "handshake_type=" + case.rsplit("[", 1)[-1].rstrip("]")
            steps = ["修改指定握手消息的最后一位", "尝试建立会话", "检查双端待建立状态与标识释放"]
            expected = "握手失败，双端待建立计数、会话和标识清空"
        elif "replay" in case or "reflection" in case:
            mutation = "captured_frame_reinjected"
            steps = [
                "捕获真实握手或认证记录",
                "按用例重放到原链路、新链路或反方向",
                "检查拒绝及现存会话状态",
            ]
            expected = "重放记录不交付，失败链路关闭，独立有效会话不受影响"
        RESULTS.append(
            {
                "case_id": report.nodeid,
                "steps": steps,
                "input_summary": case,
                "mutation_position": mutation,
                "expected": expected,
                "actual": report.outcome,
                "duration_seconds": report.duration,
                "verdict": report.outcome,
            }
        )


def pytest_sessionfinish(session: pytest.Session) -> None:
    target = session.config.rootpath / "artifacts/security-core.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture(scope="session")
def backend() -> GmSSLBackend:
    configured = os.environ.get("GMSSL_LIBRARY")
    if not configured:
        pytest.fail("GMSSL_LIBRARY and GMSSL_SHA256 must select the verified native backend")
    path = Path(configured)
    digest = os.environ.get("GMSSL_SHA256")
    if not digest:
        pytest.fail("GMSSL_SHA256 is required")
    with path.open("rb") as source:
        assert hashlib.file_digest(source, "sha256").hexdigest() == digest
    return GmSSLBackend(path, sha256=digest)
