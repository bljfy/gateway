# 使用说明

本文件说明应用线路（C）已交付的业务层如何使用。密码后端、安全会话、网关转发与隐私审计已由 A、B 线路合入；三程序的集成命令行入口尚待补齐，当前可直接运行确定性模拟器。

## 环境

按 [开发指南](developer_guide.md) 准备环境后，本地命令通过 `.tools/uv/uv.exe run --locked` 或同版本的 `uv` 执行。

## 确定性模拟器（命令行）

`cli.py` 提供 `simulate` 子命令，直接运行确定性模拟推理，不涉及网络与会话：

```powershell
.\.tools\uv\uv.exe run --locked python -m gateway.cli simulate --prompt "你好" --model "mock-model"
.\.tools\uv\uv.exe run --locked python -m gateway.cli simulate --prompt "你好" --context "片段A" --context "片段B"
.\.tools\uv\uv.exe run --locked python -m gateway.cli simulate --prompt "你好" --stream
```

`--context` 可重复提供多个检索片段；`--max-output-tokens` 控制输出预算；`--stream` 按分片输出。`--request-id` 不传时自动生成 UUID。

## 业务层编程接口

客户端与模拟服务端业务层都实现 `InferenceService`（`complete` 与 `stream`），并围绕注入的 `SecureSession` 完成收发。密码、序列号与记录分帧不暴露给调用方。

```python
from uuid import uuid4

from gateway.client import InferenceClient
from gateway.contracts import InferenceRequest
from gateway.simulator import InferenceSimulator

# 模拟服务端：确定性、无副作用，可独立运行
simulator = InferenceSimulator()
response = await simulator.complete(InferenceRequest(uuid4(), "mock-model", "你好"))
chunks = [chunk async for chunk in simulator.stream(InferenceRequest(uuid4(), "mock-model", "你好"))]

# 客户端：需要先通过 SessionManager 建立 ACTIVE 会话（由 A 线提供）
client = InferenceClient(session)  # session: SecureSession
response = await client.complete(request)
async for chunk in client.stream(request):
    ...
```

模拟服务端的入站处理由 `InferenceSimulator.serve(session)` 提供：接收已认证请求、执行确定性推理并返回，认证关闭（`SessionClosedError`）或 `CLOSE` 记录时结束循环。业务载荷的编码见 `src/gateway/codec.py`，记录分片与组装见 `src/gateway/framing.py`。

## 尚未交付

三程序命令行入口与真实 GmSSL 后端构建（需 CMake 和 C 编译器）尚待集成验收；完整链路按 [实现方案第 13 节](implementation_plan.md#13-实施顺序) 执行。不得将测试替身会话或确定性模拟器视为安全验证证据。
