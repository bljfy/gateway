# 使用说明

客户端、网关与模拟服务使用两段独立的真实国密安全会话。网络入口使用可信本地配置，不从业务载荷接收目标地址或密钥。

## 环境

按 [开发指南](developer_guide.md) 准备环境后，从仓库根目录运行。Windows 入口为 `.\.venv\Scripts\guomi-gateway.exe`，Linux 入口为 `.venv/bin/guomi-gateway`。也可以用 `uv run --locked guomi-gateway`；项目自带 uv 位于 Windows 的 `.tools/uv/uv.exe` 或 Linux 的 `.tools/uv/uv`。已有环境执行一次 `uv sync --locked` 以安装新入口。`python -m gateway.cli` 仍可使用。Linux 启动前按 [原生环境](protocol.md#原生环境) 设置 `LD_LIBRARY_PATH`。

## 一条命令演示

```powershell
.\.venv\Scripts\guomi-gateway.exe demo
.\.venv\Scripts\guomi-gateway.exe demo --prompt "你好" --stream
.\.venv\Scripts\guomi-gateway.exe demo --prompt-file prompt.txt --model mock-model
```

`demo` 首次在 `.tools/demo` 创建身份和配置，读取 `.tools/gmssl/manifest.json`。在同一进程中启动模拟器和网关，等待就绪，再通过两段真实国密 TCP 会话发送请求。省略提示词默认发送“你好”。请求结束、启动失败或 Ctrl+C 时关闭服务和会话并排空审计；审计保留在 `.tools/demo/audit.jsonl`，提示词不写入配置目录。

再次运行复用已有身份和配置。已有目录不完整或配置无效时返回失败，不覆盖文件。`--directory` 和 `--manifest` 可指定目录和原生清单；`--gateway-port`、`--simulator-port`、`--metrics-port` 仅在新建目录时生效。修改已有端口需停止服务后编辑可信配置，或用新目录初始化。不要与使用同一端口的独立服务同时运行。

## 三程序演示

从仓库根目录执行，初始化目录必须尚不存在：

```powershell
.\.venv\Scripts\guomi-gateway.exe init-demo
```

默认客户端目标为 `127.0.0.1:18443`，模拟服务为 `127.0.0.1:19443`，指标为 `127.0.0.1:19100`。端口冲突时在初始化命令提供 `--gateway-port`、`--simulator-port`、`--metrics-port`，三者必须不同。初始化生成独立 SM2 签名与加密身份、加密 DER 私钥、口令文件、角色 JSON 和共享 TOML；目录权限限制为当前用户，文件留在忽略目录。JSON 与 TOML 字段见 [配置说明](configuration.md)。

分别在两个终端启动，等待各自输出 `ready`：

```powershell
.\.venv\Scripts\guomi-gateway.exe simulator
```

```powershell
.\.venv\Scripts\guomi-gateway.exe gateway
```

第三个终端发送请求：

```powershell
.\.venv\Scripts\guomi-gateway.exe client --prompt "你好"
.\.venv\Scripts\guomi-gateway.exe client --prompt "你好" --stream
.\.venv\Scripts\guomi-gateway.exe client --prompt-file prompt.txt
Invoke-WebRequest http://127.0.0.1:19100/metrics
```

客户端输出带标识的业务响应；网关审计输出到 `.tools/demo/audit.jsonl`，终端同时显示运行过程。配置、认证或业务失败返回非零退出码和固定错误。模拟器的 `max_output_tokens` 按 Python 字符数截断业务正文，不代表真实模型 token 计数；时间戳与 UUID 信息不占正文预算，但计入整个响应的字节限额。

各角色默认读取 `.tools/demo/<角色>.json`，可通过 `--config` 指定其他配置。客户端必须提供 `--prompt` 或 UTF-8 `--prompt-file`，两者互斥。命令行提示词会出现在终端历史与进程参数中，需要避免此类记录时使用文件。

停机使用 Ctrl+C。需要停止文件时，启动服务加 `--stop-file .tools/demo/gateway.stop` 或 `--stop-file .tools/demo/simulator.stop`，再创建对应文件：

```powershell
New-Item -ItemType File .tools/demo/gateway.stop
New-Item -ItemType File .tools/demo/simulator.stop
```

服务排空审计并清理任务、会话及监听端口。重新启动前删除对应停止文件；密钥恢复与版本回滚见 [部署说明](deployment.md)。Linux 使用 `.venv/bin/guomi-gateway` 和相同子命令。

## 响应标识与运行日志

`simulate`、安全服务 `simulator` 及 `demo` 返回的输出都在原业务正文前增加一行 JSON：

```text
{"timestamp":"2026-10-07T08:00:00.123+00:00","request_id":"d0c082b3-a280-4ac2-8c53-0ee5c30b2dd9"}
[mock:mock-model] 你好
```

`timestamp` 为生成响应时的 UTC 时间，精确到毫秒；`request_id` 与客户端请求 UUID 一致。每次请求默认生成 UUID，`client`、`demo` 和 `simulate` 均可用 `--request-id <UUID>` 指定。流式输出只生成一次标识信息，随后按分片发送标识和正文；单个分片不保证包含完整 JSON 行。业务正文保持确定性，整个响应会随时间变化。

CLI 的过程日志写入 stderr，响应写入 stdout。网关显示 `server_started`、`client_authenticated`、`request_started`、`upstream_authenticated`、`request_forwarded`、`response_started`、`request_finished`、`server_stopped` 等阶段，附带 UTC 时间、审计 UUID、字节数及结束耗时。网关的 `audit_id` 为独立生成的审计标识，客户端日志与响应中的 `request_id` 为业务请求标识。日志只输出固定阶段和允许的元数据，不输出提示词、响应正文、私钥或异常全文。

只查看响应时，把全局参数 `--quiet` 放在子命令前：

```powershell
.\.venv\Scripts\guomi-gateway.exe --quiet demo --prompt "你好"
```

`--quiet` 隐藏 INFO 过程日志，审计文件继续写入，失败仍返回非零退出码。Python 编程接口使用标准 logging，由调用方配置日志级别与处理器。

## 16 KiB 手动测试

从仓库根目录生成 16384 字节的 ASCII 提示词，并发送一次请求：

```powershell
$prompt = 'x' * 16384
$path = Join-Path $PWD '.tools/prompt-16kb.txt'
[System.IO.File]::WriteAllText($path, $prompt, [System.Text.UTF8Encoding]::new($false))
(Get-Item $path).Length
$requestId = [guid]::NewGuid().ToString()
$result = & .\.venv\Scripts\guomi-gateway.exe demo --prompt-file $path --model m --max-output-tokens 20000 --request-id $requestId
$code = $LASTEXITCODE
$text = $result -join "`n"
$parts = $text -split "`n", 2
$metadata = $parts[0] | ConvertFrom-Json
"退出码：$code"
"UUID 一致：$($metadata.request_id -eq $requestId)"
"响应正文完整：$($parts[1] -ceq ('[mock:m] ' + $prompt))"
$metadata.timestamp
```

预期文件大小 `16384`，退出码 `0`，两项比较均为 `True`。加 `--stream` 可执行同样的流式验证。分开启动服务时将 `demo` 改为 `client`，网关日志显示在网关终端。16 KiB 指提示词正文；响应额外包含元数据和模拟模型前缀，线上报文还包含编码与认证开销。

## 模拟器（命令行）

`cli.py` 提供 `simulate` 子命令，直接生成带标识的模拟响应，不涉及网络与会话：

```powershell
.\.venv\Scripts\guomi-gateway.exe simulate --prompt "你好" --model "mock-model"
.\.venv\Scripts\guomi-gateway.exe simulate --prompt "你好" --context "片段A" --context "片段B"
.\.venv\Scripts\guomi-gateway.exe simulate --prompt "你好" --stream
```

`--context` 可重复提供多个检索片段；`--max-output-tokens` 控制输出预算；`--stream` 按分片输出。`--request-id` 不传时自动生成 UUID。

## 业务层编程接口

客户端与模拟服务端业务层都实现 `InferenceService`（`complete` 与 `stream`），并围绕注入的 `SecureSession` 完成收发。密码、序列号与记录分帧不暴露给调用方。

```python
from uuid import uuid4

from gateway.client import InferenceClient
from gateway.contracts import InferenceRequest
from gateway.simulator import InferenceSimulator

# 模拟服务端：业务正文确定性，响应携带生成时间与请求 UUID
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
