# B 线：网关配置、转发与隐私审计

本交付基于 `3516243a8fb48b79c975cac27c6c80b93734ac32`、公共契约 1.0。
实现 `GatewayServer`、`ForwardingService`、`load_config`、`PrivacyAudit` 与本机 `/metrics`。
测试使用 `test/unit/gateway/` 内的会话替身，仅证明 B 线调用边界、资源控制与隐私行为，
不证明 SM2/SM4、身份认证、抗重放、真实全链路或性能目标已通过。
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
当前 B 未提供可独立启动的 CLI；真实启动与 A/C 集成后验收。

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
这里的传输分片用于普通完整响应，并不宣称实现 token 流业务扩展。

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
`CANCEL` 取消关联任务并关闭其独立上游会话；断连、异常、超时、关闭服务会清理所有关联任务。
任何业务异常均不自动重试，不构造伪成功结束记录；已收到部分响应但无最终标志时，C 应判定截断。
协议错误采用关闭当前入站连接的保守策略，其他并发请求一并终止。

## 隐私审计与健康状态

`publish(AuditEvent)` 无阻塞且有界。仅允许固定事件代码、结果类别、UUID、有限非负耗时及字节数；
逐字段构造 JSON，不序列化任意对象或异常。B 为审计另生成 UUID，不输出客户端提供的请求 UUID。
安全事件使用独立容量，业务队列满不会占用安全保留位。
事件常量见 `gateway.audit.EVENTS`、`SECURITY_EVENTS` 和 `RESULTS`。

运行入口必须定期调用 `audit.drain(writer, limit=128)`；writer 接收以换行结束的 JSON，
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
测试实测结果和独立 diff 审查结论随 PR 交付；真实后端、跨平台 CI、三程序安全测试、
隐私审计性能对照与吞吐测试留待 C 集成，不填写未经测量的数值。

供 C 同步共享实现方案及 Unreleased 变更记录的摘要：新增 B 线配置加载、有界认证记录转发、
超时取消及背压、白名单审计和回环指标；不修改公共契约、依赖锁文件或其他线路模块。
本文件是 B 的交付入口；C 集成时需将此入口及实测结论同步到 README/开发指南。
