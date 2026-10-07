# 变更记录

## Unreleased

### 修复

- 修复 PR [#2](https://github.com/bljfy/gateway/pull/2) 的业务组装无界累计、提前结束流遗留会话、心跳误拒绝和头部/载荷请求 UUID 不一致；补齐整个编码载荷预算、绝对超时、单会话读者串行化和跨记录 UTF-8 解码。

- 修复网关将合法心跳当成协议错误、取消响应发送连带关闭其他请求、正常认证关闭误占安全审计队列的问题；新增兼容的 `SessionClosedError`，补充网关和真实安全会话回归测试。
- Linux 初始化接受 uv 的纯版本输出及附带构建信息的输出，避免正确版本被误判并导致 Ubuntu CI 中断。

### 新增

- README 增加自动演示与手动三程序启动流程图，以及双安全会话请求调用链图。

- 模拟响应前置 UTC 毫秒时间戳与请求 UUID，客户端、`demo` 和 `simulate` 支持指定请求标识；CLI 默认显示认证、转发、响应完成及停机过程，提供全局 `--quiet` 隐藏 INFO 日志。响应校验先分离首行元数据，正文预算保持原语义；新增 16 KiB 手动验证示例。

- `guomi-gateway` 命令入口和 `demo` 一次演示：自动初始化或复用配置，等待服务就绪，完成安全请求后清理。独立角色采用默认配置路径，初始化参数提供默认值，客户端支持直接输入提示词。

- 三进程性能测量工具、TLS 1.3 双向认证基线与裸 TCP 参考，覆盖握手分段计时、吞吐、普通/流式时延、进程 CPU/RSS、审计开关与停机证据；补充 TLS 身份拒绝回归和生产验收清单。

- 真实三程序 CLI：`init-demo`、`client`、`gateway`、`simulator`；严格角色配置、独立加密私钥和公钥信任、停机清理与审计排空。
- 真实 GmSSL 双链路安全测试和三进程端到端测试，以及集成 JSON/JUnit 报告、运行配置、部署回滚和整合报告。

- 公共接口与数据模型、uv 锁定环境、Windows/Linux 一键初始化、契约测试和 CI/交付产物工作流。
- 三条并行开发线路、最终 merge 验收、开发指南与项目 README。
- A 线真实 GmSSL 后端、规范二进制协议、双向认证与密钥确认、安全记录、会话轮换和异常清理；增加真实后端 TCP 安全测试、JSON 报告与协议说明。
- 固定 GmSSL 3.1.1 源码校验与本地原生构建清单，锁定官方 Python 绑定 2.2.2，并接入 bootstrap 与 CI。原生构建需要 CMake 和 C 编译器。
- 业务载荷的确定性版本化编码 `src/gateway/codec.py`，覆盖 `InferenceRequest`/`InferenceResponse` 与流式请求标志，严格拒绝截断、尾随字节与非法 UTF-8。
- 记录级分片与组装 `src/gateway/framing.py`，客户端与模拟服务端共享逻辑消息的记录边界校验。
- 客户端业务层 `src/gateway/client/`（`InferenceClient`），实现 `complete` 与 `stream`，校验响应身份与分片顺序。
- 确定性模拟推理 `src/gateway/simulator/`（`InferenceSimulator`）及其入站会话循环 `serve`。
- 命令行入口 `src/gateway/cli.py` 的 `simulate` 子命令。
- 业务层单元测试与回环集成测试，并启用 pytest `pythonpath` 以支持 `test/` 包内测试替身导入。
