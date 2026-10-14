# 使用说明

调用链为用户客户端 → 国密网关 → adapter → DeepSeek 模型服务。前两段使用独立国密安全会话，最后一段使用 HTTPS。

## 环境与凭据

按 [开发指南](developer_guide.md) 准备环境，在仓库根目录执行 `uv sync --locked`。Windows 入口为 `.\.venv\Scripts\guomi-gateway.exe`，Linux 为 `.venv/bin/guomi-gateway`，也可使用 `uv run --locked guomi-gateway`。Linux 先设置 [原生库加载路径](protocol.md#原生环境)。

在运行 adapter、demo 或 infer 的终端设置模型凭据：

```powershell
$env:DEEPSEEK_API_KEY = '<你的 DeepSeek API Key>'
```

Linux 使用 `export DEEPSEEK_API_KEY='<你的 DeepSeek API Key>'`。凭据不写入 JSON、Git 或命令参数；密钥缺失时调用失败。gateway、client 和 init-demo 不读取模型密钥。

## 一条命令演示

```powershell
.\.venv\Scripts\guomi-gateway.exe demo --prompt "你好"
.\.venv\Scripts\guomi-gateway.exe demo --prompt "你好" --stream
.\.venv\Scripts\guomi-gateway.exe demo --prompt-file prompt.txt --model deepseek-flash
```

`demo` 首次在 `.tools/demo` 生成身份与配置，原生清单默认 `.tools/gmssl/manifest.json`。启动 adapter 与网关后，通过两段真实 TCP 国密会话调用模型。省略提示词默认“你好”，模型默认 `deepseek-flash`，关闭思考模式，输出预算默认 512 token。

再次运行复用身份和配置；已有目录不完整时拒绝启动。`--directory`、`--manifest` 可指定位置，`--gateway-port`、`--adapter-port`、`--metrics-port` 仅在新建目录时生效。结束、失败或 Ctrl+C 时清理服务、HTTP 客户端、会话和审计。审计保留在 `.tools/demo/audit.jsonl`。

## 三程序运行

先生成新目录的配置：

```powershell
.\.venv\Scripts\guomi-gateway.exe init-demo
```

默认网关端口 18443、adapter 端口 19443、指标端口 19100。分别启动：

```powershell
# 终端 1：已设置模型密钥
.\.venv\Scripts\guomi-gateway.exe adapter
# 终端 2
.\.venv\Scripts\guomi-gateway.exe gateway
# 终端 3
.\.venv\Scripts\guomi-gateway.exe client --prompt "你好"
.\.venv\Scripts\guomi-gateway.exe client --prompt "你好" --stream
.\.venv\Scripts\guomi-gateway.exe client --prompt-file prompt.txt
Invoke-WebRequest http://127.0.0.1:19100/metrics
```

等待两个服务输出 `ready` 后发送请求。默认读取 `.tools/demo/client.json`、`gateway.json`、`adapter.json`，可以用 `--config` 指定配置。客户端提示词与 UTF-8 文件输入互斥。

使用 Ctrl+C 停止服务，或通过 `--stop-file .tools/demo/adapter.stop`、`--stop-file .tools/demo/gateway.stop` 指定停止文件。创建对应文件后服务退出；重新启动前移除停止文件。已有服务运行时使用 client，避免 demo 再次占用端口。

## 响应标识与运行日志

输出首行包含 UTC 毫秒时间戳和请求 UUID，随后为模型回答，例如：

```text
{"timestamp":"2026-10-14T08:00:00.123+00:00","request_id":"d0c082b3-a280-4ac2-8c53-0ee5c30b2dd9"}
你好！有什么可以帮助你的？
```

client、demo 和 infer 支持 `--request-id <UUID>`。流式仅生成一次元数据，记录分片可能跨 JSON 行和 UTF-8 字符。模型回答不保证确定性。

`--max-output-tokens` 对应 DeepSeek `max_tokens`，按模型 token 计费，不按 Python 字符截断。模型达到预算时可能返回截断回答；`finish_reason=length` 仍是正常完成。时间戳及 UUID 不占模型 token 预算，但计入响应字节限额。

响应写入 stdout，流式每块写入后立即刷新，固定阶段日志写入 stderr。`--quiet` 放在子命令前隐藏 INFO 过程日志；共享 TOML `[gateway].log_level` 支持五个标准级别。配置在重启后生效，审计独立写入。字段保护范围见 [配置说明](configuration.md#隐私审计与健康状态)。

## 16 KiB 手动测试

```powershell
$prompt = 'x' * 16384
$path = Join-Path $PWD '.tools/prompt-16kb.txt'
[System.IO.File]::WriteAllText($path, $prompt, [System.Text.UTF8Encoding]::new($false))
$requestId = [guid]::NewGuid().ToString()
$result = & .\.venv\Scripts\guomi-gateway.exe demo --prompt-file $path --max-output-tokens 512 --request-id $requestId
$code = $LASTEXITCODE
$parts = ($result -join "`n") -split "`n", 2
$metadata = $parts[0] | ConvertFrom-Json
"退出码：$code"
"UUID 一致：$($metadata.request_id -eq $requestId)"
"响应包含正文：$($parts.Length -eq 2 -and $parts[1].Length -gt 0)"
```

预期提示词文件 16384 字节、退出码 0、UUID 一致且收到正文。真实模型不要求回显输入；精确字节恢复由本地自动测试验证。加 `--stream` 检查流式完成。

## 直接推理

```powershell
.\.venv\Scripts\guomi-gateway.exe infer --prompt "你好"
.\.venv\Scripts\guomi-gateway.exe infer --prompt "概括资料" --context "片段A" --context "片段B" --stream
```

infer 直接调用 provider，使用 HTTPS，不经过网关。`--context` 可重复提供检索片段，按原顺序作为参考资料 user 消息，再发送提示词。地址默认 `https://api.deepseek.com`；infer 及新建演示配置可通过 `DEEPSEEK_BASE_URL` 指定地址。已有 adapter JSON 的 `base_url` 优先固定其运行地址。

## 业务层编程接口

```python
from contextlib import aclosing
from uuid import uuid4
from gateway.adapter import AdapterConfig, AdapterServer, create_provider
from gateway.config import Limits
from gateway.contracts import InferenceRequest

limits = Limits()
provider = create_provider(AdapterConfig(), limits)
adapter = AdapterServer(provider, limits=limits)
request = InferenceRequest(uuid4(), "deepseek-flash", "你好")
try:
    response = await provider.complete(request)
    async with aclosing(provider.stream(request)) as chunks:
        async for chunk in chunks:
            ...
    # 会话必须由 SessionManager.accept 完成认证后交付
    # await adapter.serve(session)
finally:
    await provider.close()
```

`InferenceClient(session, limits)` 提供 complete/stream，调用方先建立 ACTIVE 会话。adapter 在完整请求组装与 UUID 校验后调用 provider。客户端串行使用单会话；并行调用使用独立客户端会话。超时、取消或提前停止流时关闭会话并取消上游工作，后续请求重新连接。

## 验证

本地测试使用真实 GmSSL、注入 provider 或本地 HTTP/SSE 替身；不会访问 DeepSeek。外部模型测试标记 e2e，仅明确授权后运行。结果与限制见 [测试规范](testing.md)和[整合报告](integration_report.md)。
