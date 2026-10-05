# 变更记录

## Unreleased

### 修复

- 修复网关将合法心跳当成协议错误、取消响应发送连带关闭其他请求、正常认证关闭误占安全审计队列的问题；新增兼容的 `SessionClosedError`，补充网关和真实安全会话回归测试。
- Linux 初始化接受 uv 的纯版本输出及附带构建信息的输出，避免正确版本被误判并导致 Ubuntu CI 中断。

### 新增

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
