# 性能测量

结果摘要见[测试报告](integration_report.md#性能对比)，全部档位见[性能明细](tls_baseline_results.md)。工具位于 `test/benchmark/`，不调用外部模型。

## 拓扑与对比模式

客户端、网关、adapter 在三个独立进程中，通过 IPv4 回环 TCP 通信。两方案共享业务编码、转发、分片、审计和即时回显 provider；请求与回显分别为 1024 或 16384 个 ASCII 字节，流式按 256 字符分片。

| 连接策略 | 国密模式 | TLS 模式 |
| --- | --- | --- |
| 完整握手，上游每次新建连接 | `gm` | `tls` |
| 新连接允许会话恢复 | `gm-resume` | `tls-resume` |
| 完整握手加上游连接池 | `gm-pool` | `tls-pool` |
| 会话恢复加上游连接池 | `gm-combined` | `tls-combined` |

只比较相同策略的配对模式。池默认最多 8 条连接、100 个等待者、空闲 60 秒回收，每条连接独占一个请求，业务成功且终结审计接纳后归还，异常时淘汰，不自动重试业务。

国密使用真实 GmSSL、预置 SM2 双公钥和 SM4-GCM。TLS 固定为 1.3，使用每角色独立 P-256 证书、双向身份检查和实际套件 `TLS_AES_256_GCM_SHA384`，保留相同应用头，不发送 0-RTT，不生成密钥日志。

TLS 完整握手模式禁用票据；恢复模式在客户端读取服务端认证完成后的 READY、处理票据后缓存 `SSLSession`，通过上下文公开的 `wrap_bio(..., session=...)` 提供给 OpenSSL。缓存按上下文和对端隔离，自完整握手起最多使用 1800 秒，恢复不续期；服务器票据寿命由 OpenSSL 管理。缓存失效允许完整握手，报告按实际 `session_reused` 统计，不能把“启用恢复”当作“实际恢复”。

TLS 池沿用同一租用机制，并补充 TCP 健康、会话寿命、方向记录及字节预算检查。TLS 恢复只使用进程内缓存，国密使用加密 SQLite 和操作系统密钥保护；这项差异及双方未完全相同的生命周期策略应保留在比较结论中，测试适配器不作为生产替代。

## 运行

按[测试规范](testing.md#本地端到端验证)加载经校验的原生库，使用锁定依赖。Windows 恢复存储需使用实际账户的 DPAPI。不要同时运行其他基准或回归。

```powershell
uv run --locked python -m test.benchmark.run --manifest .tools/gmssl/manifest.json --modes gm-combined tls-combined --rounds 1 --handshakes 100 --requests 1000 --seconds 30 --output artifacts/tls-baseline-matched.json
uv run --locked python -m test.benchmark.report artifacts/tls-baseline-matched.json --markdown docs/tls_baseline_results.md --csv artifacts/tls-baseline-matched.csv --date 2026-10-21
```

新测量应使用新的输出文件名和实际日期，避免覆盖已有证据。该主对照覆盖两方案 × 两种载荷 × 四档并发 × 普通/流式 × 审计开关，共 64 档，每档同时满足 1000 次成功请求和 30 秒；两段分别预热 5 次，再各测 100 次独立连接握手。至少需要约 32 分钟负载时间，低吞吐档可能更长。

工具默认执行三轮，并轮换模式顺序；上面的单轮配置只满足本机单轮对照预算。短预算仅用于接线验证：

```powershell
uv run --locked python -m test.benchmark.run --manifest .tools/gmssl/manifest.json --modes gm-combined tls-combined --rounds 1 --handshakes 3 --requests 4 --seconds 0.2 --concurrency 1 10 --audit 1 --output artifacts/performance-smoke.json
```

## 统计与证据

| 指标 | 口径 |
| --- | --- |
| 握手 | 同一连接的 TCP 建连、安全 READY、总初始化；A/B 两段分别记录，包含调度和身份验证 |
| 业务时延 | 入站连接预热后记录请求响应、流完成及首块；首次业务初始化另记，使用普通请求预热 |
| 有效吞吐 | 成功交付业务字节 ÷ 实际墙钟；双向为请求加回显，不包含安全头、标签和 TCP/IP 开销 |
| CPU | 三进程 CPU 时间增量 ÷ 墙钟；100% 表示一个逻辑核，整机占比再除以逻辑核数 |
| RSS | 每 100 ms 依次采样三进程，取同次采样总和的峰值；含负载生成和累计原始样本开销 |
| 审计 | 开启时每 10 ms 排空本地文件并 flush；两方案相同，但不同于生产默认批处理频率 |

P50/P95/P99 使用最近秩。B 段独立握手由负载进程以网关身份发起，不是网关线程的独立剖析。RSS 不是不同时间单进程峰值之和，也不能直接用于跨方案内存优劣结论。

JSON 保存命令、平台与依赖版本、原生清单、源码和锁文件哈希、逐次时延、资源采样、失败、审计和停机证据。临时身份与私钥不进入报告。`complete` 只表示遍历结束；`acceptance_problems(..., formal=True)` 还检查矩阵与预算、业务失败、实际恢复、池命中、采样间隔、审计完整性和停机残留，不能只看退出码或成功数。

本机回显测量不包含真实模型计算、跨主机网络或生产容量；单轮、未固定 CPU 频率且未完全隔离后台负载的结果，不能解释为普遍算法性能优势。国密协议的前向保密、互操作及密钥内存边界见[安全协议](protocol.md)。
