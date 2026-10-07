# B 线：网关配置、转发与隐私审计

公共契约为 1.0。实现 `GatewayServer`、`ForwardingService`、`load_config`、`PrivacyAudit` 与本机 `/metrics`，由 `gateway.runtime` 连接真实安全层及业务层。
`test/unit/gateway/` 的会话替身用于验证 B 的边界；真实安全与三程序结果见 [整合报告](integration_report.md)。
分工与合并流程见 [实现方案](implementation_plan.md#13-实施顺序)。

## 启动配置

使用 `gateway.config.load_config(Path(...))` 在启动时加载 TOML。
拒绝未知表/字段、错误类型（包括以布尔值冒充整数）、非法套件、短标签、非正限额、
冲突的时间/大小限制、非回环指标地址。错误不回显配置内容或路径。
以下为 B 线实际支持的配置模式；实现方案第 9 节原示例是设计建议，不能直接作为本加载器输入。

```toml
[upstream]
peer_id = "simulator"
key_version = 1

[gateway]
suite = "SM2-SM4-GCM-SM3-v1"
tag_bytes = 16
audit_capacity = 1024
security_audit_capacity = 128
metrics_host = "127.0.0.1"
metrics_port = 9100

[session]
handshake_timeout_seconds = 5
absolute_lifetime_seconds = 1800
idle_timeout_seconds = 300
rotate_before_seconds = 60
max_records_per_direction = 1048576

[limits]
max_record_plaintext_bytes = 65536
max_wire_frame_bytes = 131072
max_request_body_bytes = 1048576
max_response_body_bytes = 16777216
max_inflight_requests_per_session = 16
max_queued_bytes_per_session = 1048576
request_timeout_seconds = 120
max_active = 1000
max_pending = 100
max_plaintext_bytes_per_direction = 1073741824
```

除 `upstream.peer_id` 外均有上述默认值。角色固定为 `simulator`，地址、公钥、私钥、
信任有效期和身份授权由 A 的可信 `SessionManager` 工厂解析。业务记录不能改变目标。
配置只在重启时重建；没有热更新或跳过认证的运行选项。

## 三角色运行配置

`init-demo` 生成 `client.json`、`gateway.json`、`simulator.json`。每个 JSON 最多 64 KiB，拒绝未知字段、错误角色、重复信任身份和非法类型。路径相对 JSON 所在目录解析，也接受受运维控制的绝对路径。实际生成文件是可直接运行的配置示例。

`init-demo` 默认目录为 `.tools/demo`，原生清单为 `.tools/gmssl/manifest.json`。`demo` 在目录不存在时调用同一初始化流程，存在时校验并复用配置；不会覆盖身份。独立角色命令默认读取 `.tools/demo/<角色>.json`，可用 `--config` 覆盖。所有默认路径相对命令执行目录；示例从仓库根目录运行。

| 字段 | 内容 |
| --- | --- |
| `peer_id`、`role` | 本地身份和 `client` / `gateway` / `simulator` 角色，密钥版本固定为 1 |
| `native_manifest` | 已校验原生库清单；`library` 和 `sha256` 必填，启动核对库哈希及版本 |
| `signing_key`、`encryption_key` | 各自独立的加密 DER 私钥文件，每个最多 512 字节 |
| `password_file` | 私钥导入口令文件，最多 1024 字节，按原始字节读取 |
| `gateway_config` | 共享的上述 TOML，三端应用相同会话策略和业务限额 |
| `host`、`port` | 本地监听地址；演示固定回环，端口范围 1–65535 |
| `target` | 客户端指向信任表中的网关身份 |
| `audit_file` | 网关本地审计 JSONL 文件，默认 `audit.jsonl` |
| `trust` | 最多 64 个预置公钥身份，见下文 |

每条信任记录包含 `peer_id`、`role`、`signing_public_key`、`encryption_public_key`（十六进制公钥）；出站目标另需 `host`、`port`。`enabled` 默认为 true，`expires_at` 为 Unix 秒时间戳，演示默认 4102444800；部署时设置实际有效期。客户端仅信任网关，模拟器仅信任网关，网关信任客户端与固定模拟器。业务请求不能修改信任表。口令文件与加密私钥同目录仅用于本地原型，生产密钥托管另行配置。

原生线帧上限固定为 131072，运行加载器拒绝其他值；记录明文配置可降低至 1 字节，由客户端、网关、模拟器共同执行。演示将队列预算设为 2 MiB，以容纳最多 1 MiB 载荷及每片 128 字节的计费开销；大量小分片仍可能先触发队列预算。启动命令与原生加载环境见 [使用说明](usage.md)。

## 接线与职责

由 C 的运行入口构造以下对象，将 `relay.handle` 注册到 `asyncio.start_server`：

```python
audit = PrivacyAudit(config.audit_capacity, config.security_audit_capacity)
metrics = Metrics()
relay = GatewayServer(manager, config, audit, metrics)
metrics_server = await start_metrics_server(
    metrics, audit, host=config.metrics_host, port=config.metrics_port
)
```

`manager` 必须是 A 的真实实现，且由应用独占生命周期；`relay.close()` 会关闭它。
创建 manager 时必须同时传入 `config.session` 及线帧、方向字节预算等限制。
契约 1.0 尚未定义 manager 工厂参数或剩余密钥寿命查询，B 不会自行模拟密码校验或延长期限。
原生密码、线帧长度、序列/nonce、方向累计字节预算、密钥寿命与轮换硬截止必须在 A 内部执行。
CLI `gateway --config ...` 构造真实 manager、转发、审计输出和指标，关闭时统一清理；完整命令见使用说明。

网关不解析 C 的业务编码，只转发可信 `recv()` 交付的请求/响应载荷；
另提供 B 的 `ForwardingService`，实现公共 `InferenceService.complete()`：
构造时注入 `service_factory: Callable[[SecureSession], InferenceService]`，由 C 的可信适配器
负责 DTO 编解码，B 负责固定上游、认证状态检查、容量、超时、取消、请求标识与审计。
它是已认证业务调用的入口，不接受原始网络输入；与 `GatewayServer` 是两种接入方式，
不要在同一个请求上重复套用。`stream()` 明确抛出 `NotImplementedError`。
DTO 入口在编码前检查文本 UTF-8 总字节数、返回时检查输出大小；适配器仍必须在构建 DTO 前
执行编码后载荷总长和记录限额，避免先接收无界输出再检查。manager 的关闭由运行入口负责。
`ForwardingService.healthy` 在终结审计失败或上游关闭失败后置为 False 并阻止后续调用。
记录头的请求标识、方向、类型、分片连续性和结束标志由 B 额外验证。
`GatewayServer` 同时转发完整响应与 C 的流式响应；`ForwardingService` 的 DTO 接口仍仅支持普通响应。

每个请求使用新建的独立上游会话，仅向配置的身份发送，成功或失败后关闭。
这是首版有界转发策略，尚未实现上游连接池复用。它避免跨请求单读者争用，代价是每次请求握手。
入站最多 `max_active` 个活跃/待认证连接，最多 `max_pending` 个待认证连接；
出站分别受相同两个限额约束。A manager 还应执行整体资源上限。

请求分片完整接收后才向上游发送。每个入站在途请求数、请求载荷总长、队列总字节都受限；
队列预算为载荷加每条记录 128 字节的计费开销，不是 Python RSS 的精确测量。
因此队列与请求上限同为 1 MiB 时，可用载荷会略低于 1 MiB。响应逐条读取并等待入站发送完成，
不累积完整响应；每个活跃请求额外最多持有一条响应记录（最大 64 KiB）。
同一入站所有发送通过一个锁串行化；慢客户端产生背压，超时后终止。

请求截止从第一片开始，覆盖组装、上游握手、发送和响应发送；握手另受握手期限限制。
`CANCEL` 取消关联任务并关闭其独立上游会话。已经开始向共享入站发送的单条响应记录，
在原请求截止前发送完毕或失败后再传播取消，发送锁和容量在此期间保留；客户端应忽略已取消请求的迟到记录。
取消不会继续读取或发送该请求的后续响应分片。断连、异常、超时、关闭服务会清理所有关联任务。
取消排空与接收循环并行，其他请求的截止仍按时执行；整个入站断连或服务关闭时，
直接中止并等待该连接拥有的发送任务，不等待业务截止后才停机。
双方合法 `HEARTBEAT` 由网关消费，不计为业务分片，也不延长请求截止。
安全层通过 `SessionClosedError` 表示已完成认证关闭；网关将入站正常关闭视为结束，
不写入 `protocol_rejected`，上游在最终响应前关闭仍判定为截断。
任何业务异常均不自动重试，不构造伪成功结束记录；已收到部分响应但无最终标志时，C 应判定截断。
协议错误采用关闭当前入站连接的保守策略，其他并发请求一并终止。

## 隐私审计与健康状态

CLI 默认在 stderr 显示启动、认证、请求转发、响应开始、完成或失败和停机过程，采用标准 logging。网关过程日志使用固定事件名、独立审计 UUID、字节数与耗时，不记录业务正文或密钥；`guomi-gateway --quiet gateway` 可隐藏 INFO 日志，JSONL 审计继续执行。客户端日志中的业务 UUID 与响应一致；网关日志中的 `audit_id` 与 JSONL 的审计 UUID 对应。输出示例见 [使用说明](usage.md#响应标识与运行日志)。

`publish(AuditEvent)` 无阻塞且有界。仅允许固定事件代码、结果类别、UUID、有限非负耗时及字节数；
逐字段构造 JSON，不序列化任意对象或异常。B 为审计另生成 UUID，不输出客户端提供的请求 UUID。
安全事件使用独立容量，业务队列满不会占用安全保留位。
事件常量见 `gateway.audit.EVENTS`、`SECURITY_EVENTS` 和 `RESULTS`。

运行入口每 50 ms 调用 `audit.drain(writer, limit=128)`，停机排空剩余队列；writer 接收以换行结束的 JSON，
应为可信、快速的本机输出函数。drain 是同步小批处理，慢存储需由运行入口安排独立输出方案。
无人消费时会在队列满后拒绝新业务，不丢弃旧条目腾位置。
写入异常保留未写条目、累计失败数、锁定 unhealthy，并拒绝后续 publish；重启恢复。
已接纳请求的终结审计无法入队时，网关锁定拒绝新业务并增加 `gateway_audit_failure_total`。
运维应同时监控该计数和 `gateway_audit_healthy`，不能只看后者。

`/metrics` 只绑定回环 IP 字面量，最多同时处理 8 个 HTTP 连接、头部上限 4096 字节、2 秒超时。
它不含身份、会话或请求标签。回环是首版本机访问边界，不对同机恶意用户提供鉴权；
不得用反向代理对公网暴露，部署时需限制本机账户与访问权限。
握手成功率可由成功/失败计数计算；重放拒绝和轮换失败计数保留供 A 的安全事件适配层调用，
当前 B 不把通用认证失败冒充为重放检测或轮换结果。

## 验证与交接

在本 worktree 使用 Python 3.12.11、uv 0.8.22 及原始锁文件运行：

```powershell
.tools/uv/uv.exe run --locked ruff check src test
.tools/uv/uv.exe run --locked ruff format --check src test
.tools/uv/uv.exe run --locked mypy src
.tools/uv/uv.exe run --locked pytest test/ -m "not e2e"
```

新增测试覆盖合法/非法配置、审计容量与泄漏标记、写入故障、回环 HTTP、认证前拒绝、
固定路由、独立会话、分片、超时、取消、慢消费者、错误响应及关闭清理。
合成数据为测试文件中的固定字符串，标识运行时随机生成；无真实密钥或用户数据。
每次执行 B 线测试在 `.tools/gateway-test-report.json` 写入用例、步骤、合成用例摘要、
修改位置、预期、实际、耗时与判定；不收集异常文本或载荷。摘要用于标识测试参数组合，
输入生成规则以对应测试源码为准。报告属于被忽略的本地运行产物。
测试实测结果和独立 diff 审查结论随交付记录；真实后端与三程序已纳入集成测试，
跨平台 CI、隐私审计性能对照与吞吐测试状态见整合报告。

PR [#1](https://github.com/bljfy/gateway/pull/1) 合并后的会话兼容修复于 2026-10-05 在
Windows x86_64 / Python 3.12.11 / uv 0.8.22 / GmSSL 3.1.1 验证：
全量非 e2e 测试 185 项通过，其中网关 89 项、安全核心 73 项；5 项 Linux bootstrap 用例
因未配置 Windows Git Bash 跳过。Ruff check、format --check、mypy 与 diff 空白检查通过。
新增真实 TCP 测试验证安全会话交付心跳、认证关闭及响应发送期间重复取消；
该验证未覆盖三程序完整业务和性能验收。回归输入为固定合成文本、随机 UUID 及事件门控；
网关 JSON、安全核心 JSON 和 JUnit 报告按上述测试流程生成。

供 C 同步共享实现方案及 Unreleased 变更记录的摘要：新增 B 线配置加载、有界认证记录转发、
超时取消及背压、白名单审计和回环指标；不修改公共契约、依赖锁文件或其他线路模块。
本文件维护运行配置与 B 的行为边界，README 和开发指南链接当前整合报告。
