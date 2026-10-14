# 测试规范

## WebUI 验证

Markdown 与元数据展示的前端回归使用 `node test/frontend/test_output.cjs`，无需 npm 安装。覆盖普通／流式、多处分片及逐字符首行、无换行 JSON、取消／失败时部分前缀、标题列表表格代码、危险 HTML／链接与图片外联。Node 仅为此独立开发测试提供 JavaScript 运行时，不是部署依赖。

新增 `test/unit/application/test_webui.py` 与 `test/integration/test_webui.py`，测试本地 HTTP／WebSocket 控制接口、Host／Origin／请求标识、配置旁文件恢复、字段和角色拒绝、认证失败、端口占用、普通／流式 Unicode 与检索片段、UUID、输出隔离、生命周期及取消清理。测试身份运行时生成，模型使用回环 SDK API 或 test/ 内 provider；不调用真实模型。

```powershell
uv run --locked pytest test/unit/application/test_webui.py test/integration/test_webui.py -m "not e2e"
```

先按下文配置已校验 GmSSL 环境。集成测试包含页面断开时 HTTP handler 再次取消的清理回归、协程尚未开始时取消、凭据缺失、WebSocket 容量及 HTTP 请求大小限额。浏览器验收使用三个独立 WebUI CLI 进程和回环模型替身，检查配置保存、认证、普通／流式结果、部分输出取消、复制／清空、非法端口提示及网关／adapter 重启。实测结果与平台限制见 [WebUI 补充验证](integration_report.md#webui-补充验证)。

## Adapter 验证

当前角色链路为 client→gateway→adapter。普通本地回归使用真实 GmSSL 和注入的测试 provider；CLI 与 SDK 测试使用 test/fixtures/model_api.py 提供的回环 HTTP/SSE，合成凭据由测试夹具设置并隔离环境代理。它们验证模型接口接线、取消与安全链路，不证明 DeepSeek 远端服务可用。

test/unit/application/test_deepseek.py 验证真实 SDK 参数映射、检索顺序、Unicode 流、空增量、唯一最终片、错误响应、断流、输出限额、超时和关闭清理。test/integration/ 覆盖双链路及三进程 adapter/demo；认证失败、篡改及重放检查 provider 调用次数。

真实模型请求继续使用 e2e 标记，必须明确授权后执行；默认回归不因真实密钥存在而联网调用模型。历史回归记录保持原测试版本和统计，当前验证结果另行追加。

获授权并配置模型凭据后，可执行 `uv run --locked pytest test/integration/test_model_e2e.py -m e2e`，以合成提示词验证一次普通请求和一次流式请求，各使用 32 token 上限。

2026-10-14 在 Windows x86_64 / Python 3.12.11 / GmSSL 3.1.1 / openai 3.31.0 执行完整非 e2e 回归：388 项通过、5 项 Linux bootstrap 因未配置 Git Bash 跳过、1 项外部模型测试排除。Ruff check、format --check（src/test/scripts）、mypy（src/scripts）及 diff 空白检查通过。JUnit 位于本地忽略目录 artifacts/adapter-junit.xml，包含真实双国密链路、三进程 CLI 与本地模型 API。此次未验证 Linux 或真实 DeepSeek 远端。

## 配置与组织

2026-10-14 在 Windows x86_64 / Python 3.12.11 / GmSSL 3.1.1 整合既有三角色 WebUI 与一键启动入口：完整非 e2e 回归 429 项通过、5 项 Linux bootstrap 用例因未配置 Git Bash 跳过、1 项外部模型测试排除；Ruff、format、mypy、PowerShell 语法及前端 27 个展示场景通过。新增启动测试使用三个独立 WebUI 进程和本地模型 API，验证普通／流式推理、业务端口冲突回滚和停止文件退出后的六端口释放；随后补充 KeyboardInterrupt 清理用例并单独验证。原生 `start.cmd -NoBrowser` 已验证首次生成配置、三控制台与业务服务就绪，以及子进程退出后清理。终端工具未成功传递键盘 Ctrl+C，浏览器自动打开及真实 DeepSeek 请求未在本次验证；KeyboardInterrupt 用例验证异常清理路径。JUnit 保存于本地忽略目录 `artifacts/webui-launcher-junit.xml`。

统一使用 pytest，测试放在 `test/`。首次实现功能前在 `pyproject.toml` 设置 `testpaths = ["test"]`、注册 `e2e` 标记，并通过 [uv](dependencies.md) 添加开发依赖。

- 文件使用 `test_*.py`，函数使用 `test_*`，公共夹具放在 `test/conftest.py`。
- 单元测试验证模块逻辑，集成测试验证本地客户端、网关与 adapter 的完整交互。
- 新增功能提供对应测试，修复缺陷增加复现问题的回归测试；关联问题单时在测试附近注释编号。
- 用参数化覆盖边界及异常输入，用临时目录隔离数据；测试不依赖执行顺序，不污染其他测试。
- 日常集成测试使用本地模型 API 替身或注入 provider、运行时生成的测试密钥及合成数据。

## 执行与判定

新增或修改测试文件后先运行对应文件中的已授权测试，修复测试或实现直到通过：

```powershell
uv run --locked pytest test/test_session.py -m "not e2e"
```

代码交付前执行完整的非外部端到端测试：

```powershell
uv run --locked pytest test/ -m "not e2e"
```

需要真实外部服务、凭据或费用的测试标记为 `e2e`，仅在用户明确授权后单独执行，不因环境变量存在自动启用。记录未执行用例及原因，不将跳过或空测试视为通过。

## 本地端到端验证

运行日志回归位于 `test/unit/gateway/test_logging.py`：输入为合成密钥、提示词、嵌套检索片段和输出标记，以及含标记的异常、自由文本、非法元数据和未知配置。判定为五个日志级别均无敏感标记、合法元数据保留、级别阈值正确、非法配置只返回固定错误、CLI 四个入口应用配置且退出恢复 logging 状态。既检查格式化输出，也检查处理器接收的 LogRecord；审计写入不受日志阈值影响。测试不证明密钥销毁或生命周期的完整实现。

2026-10-14 在 Windows / Python 3.12.11 / GmSSL 3.1.1 执行日志改动的全量非 e2e 回归：351 项通过，5 项 Linux bootstrap 用例因未配置 Git Bash 跳过；新增日志用例 45 项通过。Ruff check、format --check、mypy 和 diff 空白检查通过，包含真实双链路和三进程 CLI 验证。测试使用 `--basetemp=.tools/pytest-final` 将临时数据隔离到任务工作区；JUnit 和网关 JSON 报告保存在本地忽略目录。控制台写入失败用合成 OSError 验证固定诊断无敏感信息。

`test/integration/` 使用真实 GmSSL、临时生成的 SM2 身份和回环 TCP，包括三个独立 CLI 进程；属于本地测试，随全量非外部测试执行。必须先配置已校验原生库的 `GMSSL_LIBRARY`、`GMSSL_SHA256` 和加载路径，缺失时集成测试失败，不跳过。原生环境步骤见 [协议说明](protocol.md#原生环境)。

demo 停机检查使用 `socket.create_server` 在原网关、模拟器与指标端口重新监听。POSIX 下该接口设置 SO_REUSEADDR，避免已关闭 TCP 连接的 TIME_WAIT 导致误报；Windows 下使用标准库的默认行为。SO_REUSEPORT 保持关闭，仍有活跃监听器时检查必须失败。正常/阻塞启动和普通/quiet 四组 demo 用例继续核查响应、身份复用、审计与停机。Linux CI [运行 37617741412](https://github.com/bljfy/gateway/actions/runs/37617741412) 原有四项失败均发生在普通 socket 的停机后 bind 检查，而业务调用已成功。

2026-10-14 在 Ubuntu 24.04 / WSL2 / Python 3.12.11 / GmSSL 3.1.1 复现修复前 demo 测试：4 项失败、1 项通过，失败位置与上述 CI 一致。修复后 demo 测试 5 项通过，完整非 e2e 回归 356 项通过、无跳过；Linux Ruff check、format --check（src/test/scripts）、mypy（src/scripts）及 GmSSL 三项原生自测通过。Windows 同版本 Python/GmSSL 的 demo 测试 5 项通过。JUnit 复现、修复及全量报告保存在 `artifacts/linux-ci/`；这是本机验证，远端流水线须在提交推送后运行。

```powershell
$manifest = Get-Content .tools/gmssl/manifest.json -Raw | ConvertFrom-Json
$env:GMSSL_LIBRARY = $manifest.library
$env:GMSSL_SHA256 = $manifest.sha256
$env:PATH = (Split-Path -Parent $manifest.library) + ';' + $env:PATH
.\.tools\uv\uv.exe run --locked pytest test/ -m "not e2e" --junitxml=artifacts/junit.xml
```

覆盖普通/流式 1 KiB、16 KiB、跨 64 KiB 请求，两段独立密钥与会话标识、两段篡改拒绝、重放、错误公钥、过期信任及会话、轮换、提前关闭、超时取消、并发调用串行化，以及小记录跨 UTF-8 字符的恢复。三进程用例同时检查指标、审计泄漏标记和停机退出；两段用例检查捕获的线帧没有合成明文标记，关闭后无遗留会话或原生任务。

合成输入为固定标记与重复字符，UUID 和密钥运行时随机生成。临时私钥及口令由 pytest 临时目录持有，不进入 Git 或报告。`artifacts/integration.json` 只记录用例、步骤、输入生成摘要、故障位置标识、预期、结果与耗时；`artifacts/junit.xml` 为标准测试结果。实测版本、统计和未覆盖范围见 [整合报告](integration_report.md)。

安全用例、判定证据和 JSON 报告字段见 [实现方案第 11 节](implementation_plan.md#11-测试设计与验收)；性能载荷、握手次数、测量口径及 TLS 对照见 [第 12 节](implementation_plan.md#12-性能实验与公开方案对比)。

`test/benchmark/test_transport.py` 使用临时证书与真实本机 TLS，验证双向身份、缺失证书、错误证书及错误目标身份的拒绝行为，随本地回归执行。长时间负载不加入日常 pytest，单独执行 `python -m test.benchmark.run`；复现命令、完整矩阵与统计限制见 [性能对照](performance.md)。

样例数据记录来源或生成脚本、生成种子和预期结果，保证可复用。密码材料及报告字段遵循上述验收设计，大体积产物记录存储位置及校验值。

CI 报告保存要求见 [CI/CD 规范](ci_cd.md)。pytest 用法参考 [官方文档](https://docs.pytest.org/en/stable/getting-started.html)。
