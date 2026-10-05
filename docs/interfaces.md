# 公共接口约定

接口位于 `src/gateway/contracts.py`，契约版本为 `CONTRACT_VERSION = "1.0"`。A 线实现位于 `gateway.crypto`、`gateway.protocol`、`gateway.session`，工厂与线格式见 [protocol.md](protocol.md)；业务服务仍由 B、C 线补齐。`Protocol` 用于结构化类型检查，不创建可运行服务，也不证明对象具备安全性。

## 三线边界

| 接口 | 实现负责人 | 消费方 | 行为 |
| --- | --- | --- | --- |
| `CryptoBackend` | A | A 的安全协议 | 随机数、SM2 签名与加解密、SM4-GCM、HMAC-SM3；同步调用，由 A 管理阻塞执行 |
| `SecureSession` | A | B、C | 异步握手、发送、认证后接收及幂等关闭 |
| `SessionManager` | A | B、C | 出站 `open(peer)`、入站 `accept(reader, writer)`、轮换和统一关闭 |
| `InferenceService` | B 的转发服务、C 的客户端和模拟推理 | 各业务调用层 | `complete(request)` 返回普通响应；`stream(request)` 为可选流式扩展 |
| `AuditSink` | B | B 的网关与 A 的安全事件适配层 | 有界队列 `publish(event)`；返回 False 时拒绝接纳新业务 |

`SessionManager.open` 从可信配置解析 peer 的地址、身份和策略，调用方不能传任意上游地址。`accept` 校验入站实体后才返回 ACTIVE 会话，失败负责关闭输入传输；不得信任对端自报身份。创建 manager 的具体工厂和配置加载由 A、B 联合确定，不在业务层重复实现密码逻辑。

## 数据与标识

- `PeerIdentity`：UTF-8 长度不超过 64 字节的身份、角色和正整数密钥版本；本地信任配置提供实际公钥，业务数据不能覆盖。
- `RecordHeader`：版本 1、记录类型、16 字节会话标识、方向、64 位序列号、UUID 请求标识、32 位分片序号与结束标志。控制记录没有业务请求时使用全零 UUID；A 固定其规范编码与密文长度字段，DTO 本身不是线格式。
- `VerifiedRecord`：认证后头部与最多 64 KiB 明文。该类型可被 Python 代码构造，只有可信会话实现返回值才有认证含义，类型名不能替代校验。
- `InferenceRequest`：UUID、模型名、提示词、不可变检索片段元组和正整数输出 token 上限。`InferenceResponse` 保存同一 UUID 和输出；`InferenceChunk` 保存 UUID、连续序号、输出和结束标志。
- `SessionPolicy`：握手、绝对与空闲期限，轮换提前量以及方向报文预算；轮换提前量必须短于绝对期限，报文预算不能超出序列空间。字节预算、连接及队列限制由 B 的配置模块落实。
- `AuditEvent`：事件代码、结果类别、随机请求标识、耗时和字节数。事件代码与结果只能来自 B 的固定白名单，不可填入异常全文或业务内容。

DTO 是冻结的内部数据对象，对重要长度和范围提供检查；反序列化仍须验证类型、字段、总量及授权，不把 dataclass 当作完整的网络输入校验器。提示词、检索片段、输出及记录明文不进入默认 `repr`，日志仍须使用白名单。

## 会话与错误

`handshake()` 在双向认证与密钥确认成功后进入 ACTIVE。`send()` 由会话内部原子分配序列和 nonce，调用者仅传记录类型、载荷、请求及分片标识；`recv()` 由单一接收器调用，完整认证后才交付明文。ACTIVE/DRAINING、硬期限和失败清理的完整行为遵循 [实现方案](implementation_plan.md#6-安全协议)。

错误分为 `AuthenticationError`、`SessionExpiredError`、`ProtocolError` 和 `CapacityError`，均继承 `GatewayError`。认证失败或写入失败使当前会话不可继续业务；取消应清理关联任务和资源，保留 `asyncio.CancelledError` 的传播语义。`close()` 可重复调用。

`SessionClosedError` 是兼容契约 1.0 的新增 `ProtocolError` 子类，仅表示认证 CLOSE/CLOSE_ACK 已完成并清理。消费者先单独处理此类型，再处理真正协议错误；不能依据异常文本或 CLOSED 状态推断正常关闭。共享会话的 `send()` 一旦开始，不应随单个请求取消而中断；业务层须在原请求截止内等待该条发送结束，并保留取消传播及资源清理。

SM4-GCM 后端的 `seal_sm4_gcm` 返回密文与 16 字节标签拼接，`open_sm4_gcm` 必须先完成认证才返回完整明文，否则抛出 `AuthenticationError`。SM2 密钥、签名和密文的字节编码由 A 在 `docs/protocol.md` 固定并提供互操作测试；B、C 不直接调用这些原始密码接口。

`stream()` 是返回 `AsyncIterator[InferenceChunk]` 的普通方法；实现可使用异步生成器。连续分片从 0 开始，必须有且只有一个末尾结束分片。未实现扩展时明确抛出 `NotImplementedError`，不返回伪成功流。

## 变更与测试

接口由 C 维护，安全语义由 A、配置与审计由 B 确认。修改签名或语义时同步契约版本、消费者与测试，协调流程见 [三线计划](implementation_plan.md#13-实施顺序)。测试替身仅放在 `test/`；当前契约测试验证边界、隐私表示和异步调用，不代表实际密码或网关功能已通过验收。
