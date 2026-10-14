# 测试规范

## 配置与组织

统一使用 pytest，测试放在 `test/`。首次实现功能前在 `pyproject.toml` 设置 `testpaths = ["test"]`、注册 `e2e` 标记，并通过 [uv](dependencies.md) 添加开发依赖。

- 文件使用 `test_*.py`，函数使用 `test_*`，公共夹具放在 `test/conftest.py`。
- 单元测试验证模块逻辑，集成测试验证本地客户端、网关与模拟服务端的完整交互。
- 新增功能提供对应测试，修复缺陷增加复现问题的回归测试；关联问题单时在测试附近注释编号。
- 用参数化覆盖边界及异常输入，用临时目录隔离数据；测试不依赖执行顺序，不污染其他测试。
- 日常集成测试使用本地模拟服务、运行时生成的测试密钥及合成数据。

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
