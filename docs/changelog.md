# 变更记录

## Unreleased

### 新增

- 公共接口与数据模型、uv 锁定环境、Windows/Linux 一键初始化、契约测试和 CI/交付产物工作流。
- 三条并行开发线路、最终 merge 验收、开发指南与项目 README。
- 业务载荷的确定性版本化编码 `src/gateway/codec.py`，覆盖 `InferenceRequest`/`InferenceResponse` 与流式请求标志，严格拒绝截断、尾随字节与非法 UTF-8。
- 记录级分片与组装 `src/gateway/framing.py`，客户端与模拟服务端共享逻辑消息的记录边界校验。
- 客户端业务层 `src/gateway/client/`（`InferenceClient`），实现 `complete` 与 `stream`，校验响应身份与分片顺序。
- 确定性模拟推理 `src/gateway/simulator/`（`InferenceSimulator`）及其入站会话循环 `serve`。
- 命令行入口 `src/gateway/cli.py` 的 `simulate` 子命令。
- 业务层单元测试与回环集成测试，并启用 pytest `pythonpath` 以支持 `test/` 包内测试替身导入。
