# 性能与 TLS 1.3 对照

当前工具支持四组对应模式：`gm/tls` 完整握手、`gm-resume/tls-resume` 会话恢复、`gm-pool/tls-pool` 完整握手加连接池，以及 `gm-combined/tls-combined` 恢复加池。恢复模式预热后的独立握手样本必须全部标记 `resumed`，池模式必须提供实际池命中，否则证据检查失败。历史实验见[恢复验证结果](resumption_results.md)，更新基线的重测见[TLS 基线重测](tls_baseline_results.md)。

## 更新后的 TLS 基线

TLS 四种模式均使用同一 TLS 1.3 双向证书认证、P-256 配置及 `TLS_AES_256_GCM_SHA384` 套件，不发送 0-RTT。`tls` 与 `tls-pool` 禁用票据并拒绝恢复。`tls-resume` 与 `tls-combined` 允许服务器发出 NewSessionTicket；客户端读取认证 READY、处理票据后缓存 `SSLSession`，后续通过上下文的公开 `wrap_bio(..., session=...)` 接口向 OpenSSL 提供会话。没有修改 asyncio 私有实现或全局 SSL 方法。

缓存只属于单个对端和 SSLContext，进程退出后丢失；客户端自完整握手起最多使用 1800 秒，恢复不延长本地窗口。服务器票据寿命仍由 OpenSSL 管理，不将此测试适配器称为完整的生产撤销或持久化方案。国密恢复包含加密 SQLite 与 DPAPI 存储开销，TLS 恢复仅使用进程内缓存；本次对齐的是连接优化策略，存储与协议安全性质仍有差异。每次新连接均检查预期对端身份，客户端接收服务端认证完成后发送的 READY，记录实际 `session_reused`；票据缺失或失效时允许 OpenSSL 执行完整握手。

TLS 池使用相同 `UpstreamPool` 容量、独占租用、等待截止、空闲回收及成功归还逻辑。测量专用子类补充对 TCP 健康、会话寿命、方向记录与字节预算的租用检查，采用相同默认策略与保守响应预留；连接复用不断号，异常后不重试业务。TLS 测量适配器仍不实现国密协议的全部主动定时清理、动态信任、撤销及认证关闭策略。

主对照重测使用以下命令，保留既有完整握手及历史报告：

```powershell
uv run --locked python -m test.benchmark.run --manifest .tools/gmssl/manifest.json --modes gm-combined tls-combined --rounds 1 --handshakes 100 --requests 1000 --seconds 30 --output artifacts/tls-baseline-matched.json
uv run --locked python -m test.benchmark.report artifacts/tls-baseline-matched.json --markdown docs/tls_baseline_results.md --csv artifacts/tls-baseline-matched.csv --date 2026-10-21
```

该配置覆盖两方案、两种载荷、1/10/50/100 并发、普通/流式及审计开关，共 64 档。一次完整运行至少需要约 32 分钟负载时间，另加初始化和握手。判定同时检查预算、实际恢复、池命中、资源采样、审计及停机；单轮通过仍不等于多轮稳定性或生产验收。下文三方案拓扑与旧命令描述对应完整握手模式，池模式仅在需要新建上游连接时握手。

基准入口为 `python -m test.benchmark.run`，代码只位于 `test/benchmark/`。生产入口使用真实国密安全会话和 DeepSeek provider；本地性能工具注入测试 provider，不访问外部模型。实测汇总见 [性能结果](performance_results.md)，原始数据写入忽略目录 `artifacts/`。

## 拓扑与身份

客户端、网关、adapter 分别位于三个独立进程，通过 IPv4 回环 TCP 连接。三个方案复用 `InferenceClient`、`GatewayServer`、业务编解码、分片与 `AdapterServer.serve`。测试目录中的 Echo provider 立即回显原提示词，不调用 DeepSeek。提示词与输出分别为精确 1024 或 16384 个 ASCII 字节。普通响应另外包含业务编码，流式输出使用现有的 256 字符分片。

每个客户端连接只有一个在途业务；同一档使用指定数量的独立客户端连接。完整握手模式网关每次请求重新建立并关闭上游会话；恢复模式新建连接时允许恢复，池模式复用健康上游连接。仅比较相同连接策略的配对模式，结果不能当作单纯密码算法速度。

| 方案 | 身份与保护 | 连接设置 |
| --- | --- | --- |
| 国密 | 真实 GmSSL 3.1.1；SM2 独立签名/加密密钥与固定公钥信任；SM4-GCM | 原有安全会话、严格序列、密钥确认 |
| TLS | 每角色临时生成独立 P-256 自签名证书；双方只信任相邻指定角色证书，验证有效期、用途及身份；TLS 1.3 | P-256 临时密钥协商；要求实际套件为 `TLS_AES_256_GCM_SHA384`；禁用票据，拒绝恢复会话 |
| 裸 TCP | 同一业务头与分帧，没有网络保密/认证 | 仅作为通信开销参考 |

TLS 最低与最高版本均固定为 1.3；客户端验证服务端 DNS 身份，服务端验证预期客户端证书与角色。收到服务端应用 READY 后才返回可用会话，避免只测量客户端完成而漏掉服务端身份验证。Python 接口没有启用早期数据，基线不发送 0-RTT。适配器直接构造 SSLContext，不读取 `SSLKEYLOGFILE`。缺少证书、错误证书和错误服务端身份都有真实 TLS 拒绝回归。

TLS 基线保留与国密相同字段的应用头（版本、类型、会话、方向、序号、请求 UUID、分片和长度），由 TLS 记录提供密码保护；不额外附加 SM4 的 16 字节标签。裸 TCP 同样保留该头。该适配器用于测量，并不实现原国密协议全部密钥寿命、轮换、认证关闭和容量策略，不能作为生产替代。功能/安全性质对照依据 [RFC 8446](https://www.rfc-editor.org/rfc/rfc8446.html) 与 [Python 3.12 ssl](https://docs.python.org/3.12/library/ssl.html)。

## 运行

先按 [测试规范](testing.md#本地端到端验证) 配置经校验的原生库与加载路径。开发依赖锁定 `cryptography==46.0.3`（仅生成临时证书）与 `psutil==7.1.0`（跨平台进程采样），不加入生产运行依赖。

```powershell
uv run --locked python -m test.benchmark.run --manifest .tools/gmssl/manifest.json
```

默认执行三轮，每轮每方案/审计配置分别预热 5 次，再测量两段各 100 次完整握手；业务覆盖 1024/16384 字节、1/10/50/100 并发、普通/流式与审计开关。每个业务档持续至少 30 秒且累计至少 1000 次成功请求。矩阵顺序在多轮间轮换；禁止同时执行其他基准或回归测试。完整默认矩阵需要较长时间。

短预算只用于验证工具接线：

```powershell
uv run --locked python -m test.benchmark.run --manifest .tools/gmssl/manifest.json --rounds 1 --handshakes 2 --requests 4 --seconds 0.2 --concurrency 1 10 --output artifacts/performance-smoke.json
```

可使用 `--modes gm tls` 选择正式对照，`--rounds 1` 执行满足最小握手要求的一轮。预算不足的数据不能视为完整性能验收。adapter 使用回显，当前没有延迟模型模式；实际模型计算、跨主机网络和生产限额需在目标环境另外验证。

## 统计与证据

- 握手测量同一连接的 TCP 建连、TCP 完成至安全 READY、总初始化三个区间。独立记录 A/B 两段，各次失败保存异常类别和总耗时；独立握手测量均由负载进程发起，B 段使用网关身份连接真实 adapter 进程，不是网关进程的 CPU 分段剖析。实际网关出站建连计时另外保存在停机证据中。安全 READY 区间包含调度、证书/固定公钥校验及应用确认，不能称为纯密码运算时间。
- 首次业务初始化从入站连接开始到第一次完整业务响应，包含两段握手和第一条业务；所有档位均使用普通请求完成该预热，包括后续测量流式的档位。正式往返/首块统计在入站预热后开始。首块、普通往返与流完成时延分别保存原始样本。P50/P95/P99 使用最近秩，平均值按样本数计算。
- 单向有效吞吐为成功数 × 提示词字节 / 实际总墙钟；双向有效吞吐为其两倍。包含慢请求、失败与调度消耗的墙钟，不平均逐条吞吐。编码长度单独保存；TLS 记录及 TCP/IP 线字节未抓包统计，不能由应用头推算真实线速。
- CPU 使用三个进程 user+system 时间增量 / 同一测量墙钟，100% 为一个逻辑核心；整机归一化比例再除逻辑核数。角色顺序始终为客户端、网关、adapter。
- 每 100 ms 从父进程依次读取三个进程 RSS，记录单角色峰值和每次采样 RSS 总和的最大值。读取存在短时偏移且可能遗漏瞬时峰值；总峰值不是不同时间单角色峰值的相加。客户端进程包含负载生成、统计与采样，累计原始数据也占用内存；结果包含测量工具开销。
- 隐私审计复用字段白名单队列，开启时每 10 ms 同步排空到临时本地文件并 flush，关闭时测量替身仅接纳事件。两方案设置相同；此频率/批量有别于 CLI 默认的 50 ms/128 条，结果不能直接作为生产默认审计开销。停机时检查健康、队列拒绝、审计开始与成功结束条数及合成明文泄漏标记。
- 原始 JSON 保存命令、Python/OpenSSL、CPU/逻辑核、系统、锁文件 SHA256、原生清单、来源基准 SHA、逐次时延、资源采样、失败类别、审计和停机结果。临时身份与私钥在运行后删除，不进入报告。

每档完成后保存报告；异常中断时 `complete` 保持 false。失败样本、未达到预算、剩余会话、审计丢失或测量范围不足应阻止验收，不能只凭进程零退出判定。

`complete` 仅表示遍历完成；`acceptance_problems` 检查本次命令的预算、失败、审计与清理，非空时返回非零退出。`performance_acceptance_problems` 额外核对完整国密/TLS 矩阵和最小 100 次握手/1000 次请求/30 秒预算。保存的旧报告也可由同一判定函数重新检查。

从 JSON 自动生成汇总（`--date` 为操作者时区的报告日期）：

```powershell
uv run --locked python -m test.benchmark.report artifacts/performance.json --markdown docs/performance_results.md --csv artifacts/performance-summary.csv --date 2026-10-05
```

本轮执行 `--modes gm tls --rounds 1`，完整覆盖 64 档和两段各配置 100 次握手。共享开发主机未隔离后台桌面负载，期间存在少量文档、静态检查与统计活动，尚需受控环境多轮重复。原矩阵负载进程累计保留原始数据，跨方案 RSS 比较另用四次全新三进程运行：分别设置 `--modes gm` / `tls`、`--sizes 1024` / `16384`，共同使用 `--rounds 1 --concurrency 10 --stream 0 --audit 1`，每次写入不同的 `artifacts/rss-<方案>-<字节>.json`。补充结果和裸 TCP 长负载失败证据可附到同一报告：

```powershell
uv run --locked python -m test.benchmark.report artifacts/performance.json --markdown docs/performance_results.md --csv artifacts/performance-summary.csv --date 2026-10-05 --rss artifacts/rss-gm-1024.json artifacts/rss-tls-1024.json artifacts/rss-gm-16384.json artifacts/rss-tls-16384.json --reference artifacts/performance-reference-diagnostic.json
```

首轮正式矩阵在后续验收判定和诊断增强前启动，原始文件没有新增判定字段；已用最终 `acceptance_problems(data, formal=True)` 重新验证通过。生产源码未改变。新复测保存测量代码哈希；归档索引另记录原始数据、候选包和本轮来源，供复核。

本地证据归档位于 `artifacts/performance-tls-20261005/`，`evidence-index.json` 保存原始报告、回归 XML、候选 wheel/sdist 的 SHA256。报告引用的性能 JSON/CSV 及 `wheel-smoke.json` 同时保留在 `artifacts/` 根目录；证据为本地产物，不随 Git 提交。

## 解释边界

国密固定公钥传送与 TLS 双向证书的信任模型、握手往返和实现路径不同。TLS 由 OpenSSL/asyncio 处理，国密通过 ctypes 与异步工作线程调用 GmSSL；差异不能全部归因于 SM4 与 AES。国密首版没有前向保密，TLS 此基线使用临时 P-256 密钥协商；会话内记录序列不等于跨连接业务去重。

本机测量用于发现当前实现瓶颈，不能外推到真实模型、WAN、多进程生产服务或不同 CPU。正式发布还需 Linux 最终版本验证、多轮稳定性、目标环境压力与生产审批，见 [部署验收](deployment.md#生产验收记录)。

## Adapter 迁移工具冒烟

2026-10-14 在 Windows / Python 3.12.11 / GmSSL 3.1.1 使用本地 Echo provider 验证角色迁移后的性能工具。执行 gm/tls、1024 字节、1 并发、审计开启、普通与流式四档，rounds=1、handshakes=2、requests=4、seconds=0.2。累计 109 次业务成功、0 失败；短预算 acceptance_problems 为空，停机及审计检查通过。报告为本地 artifacts/adapter-performance-smoke.json。

该运行只验证工具接线，不满足正式性能矩阵的预算与覆盖条件，不改写历史性能结果，也不包含 DeepSeek 计算或公网延迟。
