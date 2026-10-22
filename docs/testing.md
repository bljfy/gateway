# 测试规范

当前结果见[测试报告](integration_report.md)和[测试总览](testing-dashboard.html)。本页维护测试方法与运行命令，性能方法见[性能测量](performance.md)。

## 组织与隔离

使用 pytest，测试位于 `test/`，文件名为 `test_*.py`，用例名为 `test_*`，共享夹具放在 `conftest.py`。新增功能覆盖正常与异常路径，修复缺陷增加复现回归；用临时目录隔离身份、存储和配置，不依赖执行顺序。

日常验证先执行标记为 `e2e` 的真实服务测试。真实模型调用与 Token 消耗已获项目用户授权。E2E 成功后结束动态测试；当且仅当 E2E 失败时，才运行非 E2E 测试定位问题。非 E2E 使用随机测试密钥、合成输入、注入 provider 或 `test/fixtures/model_api.py` 的回环 HTTP/SSE。原生库缺失或校验失败时测试失败，不用跳过替代验证。

## 本地端到端验证

先完成 bootstrap 并准备已校验 GmSSL。Linux 原生库搜索路径必须在 Python 测试进程启动前配置；完整环境说明见[安全协议](protocol.md#原生环境)。

```powershell
.\.tools\uv\uv.exe run --locked ruff check src test scripts
.\.tools\uv\uv.exe run --locked ruff format --check src test scripts
.\.tools\uv\uv.exe run --locked mypy src scripts
# 在 pytest 启动前准备原生库路径，自动加载凭据；仅失败时运行非 E2E 定位
.\.venv\Scripts\python.exe scripts/run_e2e.py
if ($LASTEXITCODE -ne 0) { throw 'E2E 未通过，请查看 E2E 与诊断报告。' }

git diff --check
```

## 执行与判定

开发和修复后先运行真实服务 E2E。只有 E2E 失败时，才转向受影响的非 E2E 用例或完整非 E2E 回归定位；修复后再次以 E2E 验证。静态检查按改动需要执行。完整记录失败、警告、跳过及环境限制；非 E2E 通过不能抵消 E2E 失败，跳过项和未执行项不计为通过。

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

推荐执行 `uv run --locked python scripts/run_e2e.py`。入口在创建 pytest 子进程前设置原生库环境，E2E 失败才运行非 E2E 诊断，最终仍返回 E2E 的失败码。也可在已配置原生环境的进程直接执行 `pytest test/ -m e2e --tb=short`。E2E 夹具自动读取项目根目录 `.env`，已有进程环境优先；缺失凭据会使测试失败。模型服务从 `DEEPSEEK_BASE_URL` 读取，默认 `https://api.deepseek.com`；`E2E_MODEL` 可指定模型，默认 `deepseek-flash`。外部模型地址必须为 HTTPS。

现有 E2E 共四项，分别检查适配器直连的普通／流式请求，以及 WebUI HTTP/WebSocket → 客户端 → 网关 → Adapter → 真实模型的普通／流式请求。三角色用例在临时目录生成真实国密身份、分配临时端口，校验请求 UUID、完整正文、流式结束状态及网关和 Adapter 完成指标，退出时关闭所有测试服务。它验证 WebUI 控制接口与完整业务链路，浏览器页面渲染仍需单独验收。

三角色测试读取 `.tools/gmssl/manifest.json`，也支持已有 `GMSSL_LIBRARY` 与 `GMSSL_SHA256`；缺少原生库会明确失败。统一入口为相应平台在启动前准备原生加载路径，不使用替身密码实现。Linux 夹具不再尝试通过运行中修改 `LD_LIBRARY_PATH` 改变动态库搜索。

E2E 自动使用简短回溯、关闭局部变量和捕获内容显示，并在失败报告与 JUnit 中脱敏凭据。错误额外记录异常类型和 HTTP 状态码，不输出 SDK 请求参数、原始异常消息或认证头。真实服务结果与失败后的替身诊断结果分别记录。测试顺序同时记录于项目根目录 `AGENTS.md`。

环境同时设置 HTTPS 的 HTTP 代理和 SOCKS `ALL_PROXY` 时，如果未安装 SOCKS 支持，夹具仅在测试期间移除冗余 SOCKS `ALL_PROXY`，保留 HTTPS 代理。若只有 SOCKS 代理则不自动绕过，需提供其依赖或有效的 HTTPS 代理。系统代理设置保持原样。

性能长负载不加入日常 pytest；同一时间只运行一套基准，不同时执行回归或其他负载。复现命令、统计口径和证据判定见[性能测量](performance.md)，CI 产物保存见[CI/CD 规范](ci_cd.md)。

## 2026-10-22 验证记录

Windows 独立 worktree 使用锁定依赖和独立虚拟环境，通过统一入口完成四项真实模型 E2E，最终结果为 4 passed，耗时 7.32 秒；Ruff 和 mypy 检查通过。原生库复用经过哈希校验的固定构建产物，测试身份和运行数据独立生成。

首次独立验证中，三角色流式用例出现一次 inference_failed，其余三项通过；入口随后执行非 E2E 诊断，结果为 565 passed、6 skipped，并保留原 E2E 失败退出码。再次执行真实 E2E 全部通过；未增加自动重试，也未把诊断成功当作 E2E 成功。该次真实流式失败未确认根因，首次失败报告与后续通过报告分别保留在本地产物目录。

独立 diff 审查发现 Linux 夹具在 Python 启动后设置动态库搜索路径的问题，已改为由统一入口在创建测试子进程前传入环境。此处记录的真实服务执行平台为 Windows；Linux 真实模型 E2E 和浏览器页面渲染尚未实测。
