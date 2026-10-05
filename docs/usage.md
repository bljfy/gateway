# 使用说明

客户端、网关与模拟服务使用两段独立的真实国密安全会话。网络入口使用可信本地配置，不从业务载荷接收目标地址或密钥。

## 环境

按 [开发指南](developer_guide.md) 准备环境后，本地命令通过 `.tools/uv/uv.exe run --locked` 或同版本的 `uv` 执行。

## 三程序演示

从仓库根目录执行，初始化目录必须尚不存在：

```powershell
.\.tools\uv\uv.exe run --locked python -m gateway.cli init-demo --directory .tools/demo --manifest .tools/gmssl/manifest.json
```

默认客户端目标为 `127.0.0.1:18443`，模拟服务为 `127.0.0.1:19443`，指标为 `127.0.0.1:19100`。端口冲突时在初始化命令提供 `--gateway-port`、`--simulator-port`、`--metrics-port`，三者必须不同。初始化生成独立 SM2 签名与加密身份、加密 DER 私钥、口令文件、角色 JSON 和共享 TOML；目录权限限制为当前用户，文件留在忽略目录。JSON 与 TOML 字段见 [配置说明](configuration.md)。

分别在两个终端启动，等待各自输出 `ready`：

```powershell
.\.tools\uv\uv.exe run --locked python -m gateway.cli simulator --config .tools/demo/simulator.json --stop-file .tools/demo/simulator.stop
```

```powershell
.\.tools\uv\uv.exe run --locked python -m gateway.cli gateway --config .tools/demo/gateway.json --stop-file .tools/demo/gateway.stop
```

第三个终端准备 UTF-8 提示词并请求：

```powershell
[System.IO.File]::WriteAllText((Join-Path $PWD '.tools/demo/prompt.txt'), '你好', [System.Text.UTF8Encoding]::new($false))
.\.tools\uv\uv.exe run --locked python -m gateway.cli client --config .tools/demo/client.json --prompt-file .tools/demo/prompt.txt --model mock-model
.\.tools\uv\uv.exe run --locked python -m gateway.cli client --config .tools/demo/client.json --prompt-file .tools/demo/prompt.txt --model mock-model --stream
Invoke-WebRequest http://127.0.0.1:19100/metrics
```

客户端输出业务响应；网关审计输出到 `.tools/demo/audit.jsonl`。配置、认证或业务失败返回非零退出码和固定错误，不输出密钥或异常全文。模拟器的 `max_output_tokens` 按 Python 字符数截断确定性输出，不代表真实模型 token 计数。

停机使用 Ctrl+C，或创建启动时指定的停止文件：

```powershell
New-Item -ItemType File .tools/demo/gateway.stop
New-Item -ItemType File .tools/demo/simulator.stop
```

服务排空审计并清理任务、会话及监听端口。重新启动前删除对应停止文件；密钥恢复与版本回滚见 [部署说明](deployment.md)。Linux 使用 `.tools/uv/uv` 和相同 Python 子命令，并在启动前按 [原生环境](protocol.md#原生环境) 设置 `LD_LIBRARY_PATH`。

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

`InferenceClient(session, limits)` 和 `InferenceSimulator(limits=limits)` 接收共享的 `gateway.config.Limits`。单个客户端串行执行完整调用，只有一个会话读者；需要并行推理时使用独立客户端会话。合法心跳被消费且不延长业务截止。请求/普通响应的限额包含整个编码载荷，流式响应按 UTF-8 累计字节计费；UTF-8 可以跨记录边界。

提前停止流时显式 `await iterator.aclose()`（或用 `contextlib.aclosing`），客户端关闭整个会话，使网关取消独立上游任务。超时、取消或协议错误也关闭该会话；后续请求重新连接。消费流的应用代码不被跨 `yield` 的超时上下文取消，下一次读取仍受原始绝对截止约束。

## 验证

真实两段 TCP 与三进程 CLI 用例位于 `test/integration/`。运行命令、结果和验收限制见 [整合报告](integration_report.md)；独立 `simulate` 的输出只验证确定性业务语义。
