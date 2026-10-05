"""Report provenance must follow the supplied measurement, including partial runs."""

from test.benchmark.report import render
from test.benchmark.test_acceptance import evidence


def test_report_uses_real_rounds_source_and_operator_date():
    data = evidence()
    data["environment"].update(
        platform="test-platform",
        cpu="test-cpu",
        logical_cpus=2,
        python="3.12.11",
        openssl="test-openssl",
        source_sha="test-source",
        lock_sha256="test-lock",
    )
    data["environment"]["command"].update(rounds=3, output="artifacts/custom-run.json")
    data["cases"][0].update(
        payload_bytes=1024,
        stream=0,
        latency={"p95_ms": 3},
        first_chunk={"p95_ms": None},
        bidirectional_mib_s=2,
        cpu_percent_by_role=[1, 2, 3],
        rss_total_peak=2**20,
    )
    for shutdown in data["shutdown"]:
        shutdown["cipher_suites"] = []
    for group in data["handshakes"]:
        group.update(mode="gm", audit=1)
        for row in group["samples"]:
            row.update(tcp_ms=1, security_ready_ms=2, secure_open_ms=3)
    report = render(data, "test-digest", "2027-01-02")
    assert "报告日期：2027-01-02" in report
    assert "artifacts/custom-run.json" in report
    assert "命令指定 3 轮" in report
    assert "只执行一轮" not in report
    assert "未达到完整本机对照验收" in report
    assert "test-digest" in report
