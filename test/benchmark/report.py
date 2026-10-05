"""Generate a Markdown summary and CSV from saved benchmark evidence."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

from gateway.protocol import HEADER
from test.benchmark.run import acceptance_problems, distribution


def render(data, digest, report_date=None):
    env = data["environment"]
    cases = data["cases"]
    problems = acceptance_problems(data, formal=True)
    failures = sum(len(c["failures"]) for c in cases)
    successes = sum(c["successes"] for c in cases)
    lines = [
        "# 性能实测结果",
        "",
        f"报告日期：{report_date or '未指定'}。方法与限制见 [性能与 TLS 对照](performance.md)。",
        "",
        f"本次记录 {len(cases)} 个负载档、{successes} 次成功业务、{failures} 次业务失败。"
        + (
            "完整本机对照的预算与证据检查通过。"
            if not problems
            else "本次记录未达到完整本机对照验收：" + "；".join(problems) + "。"
        ),
        "",
        f"环境：{env['platform']}；{env['cpu']}；{env['logical_cpus']} 逻辑核；"
        f"Python {env['python']}；{env['openssl']}；GmSSL 3.1.1/绑定 2.2.2。",
        "",
        f"来源基准：`{env['source_sha']}`，测量工具为本次新增实现；"
        f"锁文件 SHA256：`{env['lock_sha256']}`。",
        "",
        f"原始数据：`{env['command']['output']}`，SHA256：`{digest}`。"
        "各次时延、资源序列、握手与清理证据保存在原始 JSON；"
        "每档汇总可重新生成至 `artifacts/performance-summary.csv`。",
        "",
        "## 握手",
        "",
        "TCP、安全 READY 和总初始化分别按同一连接测量。以下时延单位为 ms；"
        "安全区间包含身份验证、调度与应用确认。失败另计。",
        "",
        "| 方案 | 审计 | 段 | 成功/失败 | TCP 均值 | 安全均值 | 总均值 | 总 P95 | 总 P99 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for group in data["handshakes"]:
        for hop in ("A", "B"):
            rows = [r for r in group["samples"] if r["hop"] == hop and not r["error"]]
            failed = sum(r["hop"] == hop and bool(r["error"]) for r in group["samples"])
            if not rows:
                continue
            tcp = distribution([r["tcp_ms"] for r in rows])
            secure = distribution([r["security_ready_ms"] for r in rows])
            total = distribution([r["secure_open_ms"] for r in rows])
            lines.append(
                f"| {group['mode']} | {group['audit']} | {hop} | {len(rows)}/{failed} | "
                f"{tcp['mean_ms']:.3f} | {secure['mean_ms']:.3f} | {total['mean_ms']:.3f} | "
                f"{total['p95_ms']:.3f} | {total['p99_ms']:.3f} |"
            )
    lines += [
        "",
        f"应用记录头 {HEADER.size} 字节、长度前缀 4 字节；"
        "国密另有 16 字节 GCM 标签。普通请求编码比提示词多 34 字节，"
        "普通响应编码比输出多 22 字节（本次 echo 模型与无检索配置）。"
        "流式输出按 256 字节分片。TLS 记录与 TCP/IP 开销未做线字节测量。",
    ]
    lines += [
        "",
        "## 首次业务与审计开关",
        "",
        "以下抽取 10 并发档。首次业务 P95 为普通预热请求，"
        "从入站连接开始计时；吞吐变化为开启/关闭审计的比值减一。"
        f"命令指定审计顺序 {env['command']['audit']}（0 关闭、1 开启），"
        "变化可能包含时间和机器负载差异，"
        "不能全部归因为审计。完整档位见原始数据。",
        "",
        "| 方案 | 字节 | 流式测量 | 首次业务 P95 ms（审计开） | 吞吐变化 % |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in cases:
        if c["audit"] != 1 or c["concurrency"] != 10:
            continue
        off = next(
            (
                row
                for row in cases
                if row["mode"] == c["mode"]
                and row["audit"] == 0
                and row["payload_bytes"] == c["payload_bytes"]
                and row["concurrency"] == c["concurrency"]
                and row["stream"] == c["stream"]
                and row["round"] == c["round"]
            ),
            None,
        )
        initial = c["first_business_initialization"]["p95_ms"]
        if off and off["bidirectional_mib_s"] > 0 and initial is not None:
            delta = (c["bidirectional_mib_s"] / off["bidirectional_mib_s"] - 1) * 100
            lines.append(
                f"| {c['mode']} | {c['payload_bytes']} | {c['stream']} | "
                f"{initial:.3f} | {delta:+.1f} |"
            )
    lines += [
        "",
        "## 业务与资源",
        "",
        "每行是一档独立墙钟区间；吞吐为成功交付的双向业务 MiB/s，"
        "CPU 为三个进程合计、100% 代表一个逻辑核，RSS 为同步采样总峰值 MiB。"
        "时延为完整业务 P95；流式档另列首块 P95。",
        "",
        "| 方案 | 审计 | 字节 | 并发 | 流式 | 成功 | 秒 | P95 ms | 首块 P95 ms | "
        "MiB/s | CPU % | RSS MiB |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in cases:
        p95 = c["latency"]["p95_ms"]
        first = c["first_chunk"]["p95_ms"]
        p95_text = f"{p95:.3f}" if p95 is not None else "—"
        lines.append(
            f"| {c['mode']} | {c['audit']} | {c['payload_bytes']} | {c['concurrency']} | "
            f"{c['stream']} | {c['successes']} | {c['wall_seconds']:.1f} | "
            f"{p95_text}"
        )
        lines[-1] += f" | {first:.3f}" if first is not None else " | —"
        lines[-1] += (
            f" | {c['bidirectional_mib_s']:.3f} | {sum(c['cpu_percent_by_role']):.1f}"
            f" | {c['rss_total_peak'] / 2**20:.1f} |"
        )
    lines += [
        "",
        "## 审计与清理",
        "",
        "审计计数含每连接一条预热业务；应与原始成功数加预热连接数一致。",
        "",
        "| 方案 | 开始 | 成功结束 | 合成明文泄漏标记 |",
        "| --- | --- | --- | --- |",
    ]
    for audit in data["audit_evidence"]:
        lines.append(
            f"| {audit['mode']} | {audit['started']} | {audit['finished_ok']} | "
            f"{audit['synthetic_marker_leaks']} |"
        )
    remaining = sum(r["active_sessions"] for r in data["shutdown"])
    rejected = sum(r["audit_rejected"] for r in data["shutdown"])
    unhealthy = sum(not r["audit_healthy"] for r in data["shutdown"])
    suites = sorted({s for r in data["shutdown"] for s in r["cipher_suites"]})
    lines += [
        "",
        f"停机证据：{len(data['shutdown'])} 条；剩余会话 {remaining}；"
        f"审计拒绝 {rejected}；不健康审计 {unhealthy}。实际 TLS 套件：`{', '.join(suites)}`。",
        "",
        "## 结论范围",
        "",
        "结果代表当前三进程回环实现，包括每次业务的上游握手。"
        "TLS 与国密身份、协议往返和运行路径不同，不能将速度差异归因于单一算法。"
        "高并发超过吞吐峰值时应按资源和时延设置容量，而非仅提高连接上限。",
        "",
        "测量未记录后台负载隔离或固定 CPU 频率的证据。"
        "结果包含时间变化与测量工具开销，需在受控目标环境重复验证。",
        "",
        f"本次运行命令指定 {env['command']['rounds']} 轮；"
        "本机结果不能证明 Linux、真实模型/跨主机及生产容量。"
        "裸 TCP 短预算结果只证明测量接线；长负载参考状态见补充记录。"
        "生产发布与回滚状态见 [部署验收](deployment.md#生产验收记录)。",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--date", help="Report date in the operator's timezone (YYYY-MM-DD)")
    parser.add_argument("--rss", nargs="*", type=Path, default=[])
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    data = json.loads(raw)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    report = render(data, hashlib.sha256(raw).hexdigest(), args.date)
    if args.rss:
        report += "\n## 独立进程 RSS 复测\n\n"
        report += (
            "原矩阵负载进程保留前序档位原始数据，其总 RSS 不能直接用于跨方案内存比较。"
            "以下每行使用全新负载进程和两个全新服务进程，10 并发、普通请求、审计开启，"
            "各至少 1000 请求和 30 秒。仍包含当前档计时/采样开销，仅覆盖这些代表档位。\n\n"
            "| 方案 | 字节 | 客户端峰值 MiB | 网关峰值 MiB | 模拟器峰值 MiB | "
            "同步总峰值 MiB | 验收 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
        )
        rss_sources = []
        for path in args.rss:
            source = path.read_bytes()
            profile = json.loads(source)
            if len(profile["cases"]) != 1:
                parser.error("RSS supplements must contain one case per fresh process run")
            c = profile["cases"][0]
            verdict = "通过" if not acceptance_problems(profile) else "未通过"
            peaks = c["rss_peak_by_role"]
            report += (
                f"| {c['mode']} | {c['payload_bytes']} | {peaks[0] / 2**20:.1f} | "
                f"{peaks[1] / 2**20:.1f} | {peaks[2] / 2**20:.1f} | "
                f"{c['rss_total_peak'] / 2**20:.1f} | {verdict} |\n"
            )
            rss_sources.append(
                f"原始数据 `{path}`，SHA256 `{hashlib.sha256(source).hexdigest()}`。"
            )
        report += "\n" + "\n\n".join(rss_sources) + "\n"
    if args.reference:
        source = args.reference.read_bytes()
        reference = json.loads(source)
        report += "\n## 裸 TCP 参考验收\n\n"
        report += (
            f"原始数据 `{args.reference}`，SHA256 `{hashlib.sha256(source).hexdigest()}`。\n\n"
        )
        errors = [item for row in reference["shutdown"] for item in row.get("open_errors", [])]
        if acceptance_problems(reference):
            report += (
                "本次裸 TCP 长负载参考未通过验收，失败和未完成档位不纳入正式吞吐结论。"
                "网络建连诊断仅保存异常类型和系统错误编号，未确定系统错误的根因。\n\n"
            )
        else:
            report += "本次命令指定的裸 TCP 参考负载验收通过。\n\n"
        report += "| 字节 | 并发 | 成功 | 失败 | 实际秒 |\n| --- | --- | --- | --- | --- |\n"
        for c in reference["cases"]:
            report += (
                f"| {c['payload_bytes']} | {c['concurrency']} | {c['successes']} | "
                f"{len(c['failures'])} | {c['wall_seconds']:.2f} |\n"
            )
        for error in errors:
            report += (
                f"\n出站建连：`{error['type']}`、winerror `{error['winerror']}`、"
                f"errno `{error['errno']}`，{error['count']} 次。\n"
            )
    args.markdown.write_text(report, encoding="utf-8")
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "mode",
                "audit",
                "payload_bytes",
                "concurrency",
                "stream",
                "successes",
                "failures",
                "wall_seconds",
                "mean_ms",
                "p50_ms",
                "p95_ms",
                "p99_ms",
                "first_chunk_p95_ms",
                "first_business_p95_ms",
                "bidirectional_mib_s",
                "cpu_client",
                "cpu_gateway",
                "cpu_simulator",
                "rss_total_peak_bytes",
            ]
        )
        for c in data["cases"]:
            writer.writerow(
                [
                    c[k]
                    for k in (
                        "mode",
                        "audit",
                        "payload_bytes",
                        "concurrency",
                        "stream",
                        "successes",
                    )
                ]
                + [len(c["failures"]), c["wall_seconds"]]
                + [c["latency"][k] for k in ("mean_ms", "p50_ms", "p95_ms", "p99_ms")]
                + [
                    c["first_chunk"]["p95_ms"],
                    c["first_business_initialization"]["p95_ms"],
                    c["bidirectional_mib_s"],
                ]
                + c["cpu_percent_by_role"]
                + [c["rss_total_peak"]]
            )


if __name__ == "__main__":
    main()
