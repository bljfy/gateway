# 开发指南

## 当前基线

已整合 A 的真实密码与会话、B 的转发与隐私审计、C 的业务客户端和模拟服务，并提供三程序 CLI。公共接口见 [interfaces.md](interfaces.md)，原生环境见 [protocol.md](protocol.md)，启动见 [usage.md](usage.md)，实测与后续验收项见 [integration_report.md](integration_report.md)。三线分工保留在 [实现方案第 13 节](implementation_plan.md#13-实施顺序)。

## 一键准备环境

固定 Python 3.12.11、uv 0.8.22，版本文件分别为 `.python-version`、`.uv-version`。开发依赖精确版本在 `pyproject.toml`，全部解析结果在 `uv.lock`。

Windows x86_64 使用 PowerShell 5.1 或更新版本，从仓库根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
```

Linux x86_64 需要 Bash、curl、tar、sha256sum：

```bash
bash scripts/bootstrap.sh
```

脚本从官方发布下载固定 uv 并核对预置 SHA256，安装本地 Python，执行锁定同步、Ruff、mypy 和 pytest。第一次运行需访问 GitHub 与 Python 包索引；失败即停止，成功才显示环境就绪。重复执行复用已安装工具和环境，不修改锁文件。只准备环境可用 `-SkipChecks` 或 `--skip-checks`，这不表示验证通过。

工具、Python 和缓存位于当前 worktree 的 `.tools/`，依赖位于 `.venv/`；脚本不修改系统 PATH。环境就绪后本地 uv 路径如下，也可使用自行安装且版本一致的 uv：

```powershell
.\.tools\uv\uv.exe run --locked pytest test/ -m "not e2e"
```

```bash
.tools/uv/uv run --locked pytest test/ -m 'not e2e'
```

首次下载失败时检查网络后重试；版本或校验失败时核对 `.uv-version` 与脚本中的校验值，不跳过校验。工具版本更新由 C 统一维护版本文件、两平台校验值和工作流。

Linux 初始化的版本判断回归测试在 Ubuntu CI 中执行。Windows 本地验证该测试时，可将 `GATEWAY_TEST_BASH` 环境变量设为 Git Bash 的 `bash.exe` 绝对路径；未指定时跳过这组测试。测试使用临时目录中的工具替身，不下载或安装软件。

该脚本部署开发环境。完整检查会构建并自测 GmSSL 3.1.1，需要 CMake 和 C 编译器；已有 `.tools/gmssl/manifest.json` 时复用经校验库。`SkipChecks` 仅同步 Python 环境。独立运行 pytest 前须按 [协议文档](protocol.md#原生环境) 设置 DLL 路径、`GMSSL_LIBRARY` 和 `GMSSL_SHA256`；Linux 另需 `LD_LIBRARY_PATH`。本地服务配置与密钥生成见 [使用说明](usage.md)，回滚见 [部署说明](deployment.md)，生产发布条件见 [CI/CD 规范](ci_cd.md)。

环境准备后可直接运行 Windows 的 `.\.venv\Scripts\guomi-gateway.exe demo` 或 Linux 的 `.venv/bin/guomi-gateway demo`，完成一次真实安全链路演示。Linux 仍需设置原生库加载路径；提示词、流式输出和独立服务启动见 [使用说明](usage.md)。

## 三人开工

确认公共接口后记录共同基准提交，分别创建工作目录。下面的 `main` 应指向三人确认的同一提交：

```powershell
git worktree add -b codex/security-core .worktrees/security-core main
git worktree add -b codex/gateway-service .worktrees/gateway-service main
git worktree add -b codex/application-delivery .worktrees/application-delivery main
```

每人在自己的 worktree 执行初始化。A 实现密码、协议与会话，B 实现网关与隐私审计，C 实现客户端、模拟服务和交付工具。模块所有权以计划归属表为准；公共接口、依赖与公共夹具集中由 C 维护，各人使用自己测试目录下的局部夹具。

业务模块导入 `gateway.contracts` 中的类型并注入 `SecureSession` 或 `SessionManager`，不自行生成密码材料、不传任意上游地址。暂时未完成的下游使用符合接口的测试替身，仅用于本线测试；真实运行和最终验收不得使用替身。

## 日常验证与交付

按 [开发流程](development.md) 执行修改、文档同步、独立 subagent diff 审查、提交和复审。编码与测试命令分别维护在 [编码规范](coding.md) 和 [测试规范](testing.md)，依赖更新遵循 [uv 规范](dependencies.md)。本线交付记录接口版本、提交 SHA、真实验证结果和待联调项。

三线都就绪后，C 在独立集成 worktree 中按计划 merge 指定交付提交，再使用真实后端做三程序、安全与性能验收。集成冲突由文件负责人处理，最终 diff 独立审查后合入 `main`。基于测试替身的通过记录不能替代全链路验收。

## CI 与交付产物

CI 在 PR、`main` 推送或手动触发时构建固定 GmSSL、执行原生自测、静态检查与全量非外部测试，保存 JUnit、安全核心和集成 JSON。Windows 三程序已本地验证；最终提交的 Linux 和 GitHub Actions 结果以实际流水线记录为准。

`Delivery artifacts` 工作流由 `v*` 标签或手动触发，复用同一提交的 CI，校验标签版本，再构建 Python wheel 与源码包，保存锁文件、工具版本、提交 SHA 和 SHA256 校验清单。包包含三程序源码，原生库需单独构建；生产凭据和运行配置不进入产物。实际云端运行结果以 GitHub Actions 记录为准，生产部署需按 CI/CD 规范配置目标环境。
