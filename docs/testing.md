# 测试规范

当前结果见[测试报告](integration_report.md)和[测试总览](testing-dashboard.html)。本页维护测试方法与运行命令，性能方法见[性能测量](performance.md)。

## 组织与隔离

使用 pytest，测试位于 `test/`，文件名为 `test_*.py`，用例名为 `test_*`，共享夹具放在 `conftest.py`。新增功能覆盖正常与异常路径，修复缺陷增加复现回归；用临时目录隔离身份、存储和配置，不依赖执行顺序。

默认回归使用随机测试密钥、合成业务输入、注入 provider 或 `test/fixtures/model_api.py` 的回环 HTTP/SSE，不访问外部模型。真实外部服务、凭据或费用相关测试标记 `e2e`，只有明确授权后才运行。原生库缺失或校验失败时测试失败，不用跳过替代验证。

## 本地端到端验证

先完成 bootstrap 并加载已校验 GmSSL。原生库必须在首次导入绑定前配置；完整环境说明见[安全协议](protocol.md#原生环境)。

```powershell
$nativeManifest = Get-Content .tools/gmssl/manifest.json -Raw | ConvertFrom-Json
$env:GMSSL_LIBRARY = $nativeManifest.library
$env:GMSSL_SHA256 = $nativeManifest.sha256
$env:PATH = (Split-Path -Parent $nativeManifest.library) + ';' + $env:PATH
.\.tools\uv\uv.exe run --locked ruff check src test scripts
.\.tools\uv\uv.exe run --locked ruff format --check src test scripts
.\.tools\uv\uv.exe run --locked mypy src scripts
.\.tools\uv\uv.exe run --locked pytest test/ -m "not e2e" --junitxml=artifacts/junit.xml
git diff --check
```

## 执行与判定

开发时先运行受影响测试文件，修复后完成非外部回归与静态检查，完整查看失败、警告及跳过原因。跳过项、未执行项和环境限制单独记录，不计为通过。

## 验证范围

| 模块 | 主要测试位置 | 检查重点 |
| --- | --- | --- |
| 密码与协议 | `test/unit/security_core/` | 已知答案、签名身份、认证解密、握手、序号、期限、预算与清理 |
| 网关与隐私审计 | `test/unit/gateway/` | 验证后转发、资源限制、日志白名单、审计失败、池租用与回收 |
| 业务与模型适配 | `test/unit/application/` | 编解码、Unicode、SDK 参数、UUID、流式终止、超时、取消、凭据和 CLI |
| 双链路与独立进程 | `test/integration/` | 真实 SM2/SM4-GCM TCP、三进程、抓包敏感标记、篡改/重放拒绝和停机 |
| TLS 与测量证据 | `test/benchmark/` | 双向身份、实际恢复、缓存期限、池预算和损坏证据拒收 |

安全测试同时核查返回、状态、下游调用次数与资源释放，不以“出现错误日志”作为唯一判定。报告不得保存私钥、口令、API Key 或完整敏感业务内容。JSON 记录步骤、合成输入摘要、预期、实际和判定，具体输入及注入动作由测试源码定义；JUnit 记录执行状态与耗时。

## 恢复存储与连接池验证

`test/unit/security_core/test_resumption.py` 检查加密落盘、重载、独占锁、过期不续期、密钥独立性、策略绑定、篡改与撤销。`test/unit/gateway/test_pool.py` 检查容量、独占租用、等待、取消、空闲回收及停机。`test/integration/test_persistent_sessions.py` 检查独立客户端进程恢复、池命中和运行中撤销。

Windows 秘密存储测试使用实际账户的 DPAPI/ACL；Linux 使用支持 POSIX 权限的本地文件系统，WSL 的 Windows 挂载目录不能替代该权限验证。

## WebUI 验证

`test/integration/test_webui.py` 检查本地 HTTP/WebSocket、Host/Origin、角色限制、配置、普通/流式推理、页面输出隔离和取消清理。`test_webui_manual.py` 使用真实安全链路注入篡改、重放、反射和错误参数；`test_webui_connections.py` 检查连接观察、并发模拟及回收。会话详情与有界历史由 `test/unit/application/test_session_history.py` 等覆盖。

独立前端回归使用 Node 内置模块，无需 npm 安装：

```powershell
node test/frontend/test_output.cjs
node test/frontend/test_sessions.cjs
```

浏览器人工验收需另行记录页面操作、输出、隔离和生命周期结果；Python 回归不能代替浏览器渲染检查，Node 回归也不能代替真实国密链路验证。

## 外部模型验证

获得明确授权并配置凭据后，执行 `uv run --locked pytest test/integration/test_model_e2e.py -m e2e`，使用合成提示词检查普通和流式调用。结果必须与本地替身回归分开记录，不因环境变量中存在真实密钥就自动联网。

性能长负载不加入日常 pytest；同一时间只运行一套基准，不同时执行回归或其他负载。复现命令、统计口径和证据判定见[性能测量](performance.md)，CI 产物保存见[CI/CD 规范](ci_cd.md)。
