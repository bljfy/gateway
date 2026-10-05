# 开发流程

项目开发规范见根目录 `AGENT.md`。文档保存在 `docs/`，Python 源文件保存在 `src/`，pytest 测试保存在 `test/`。

开始实现前，在 `pyproject.toml` 中声明 Python 版本、固定依赖以及 pytest 的 `testpaths = ["test"]`，并记录环境安装步骤。目前仓库尚未配置测试依赖，也没有可执行测试用例。

每项任务使用独立分支和 Git worktree。完成一次修改时，同步更新对应文档，运行相关检查后，将实现、测试和文档作为一个逻辑完整的 Git 提交保存。交付记录提交哈希和实际验证结果。

统一使用 uv 管理 Python、依赖、虚拟环境和锁文件。运行依赖在 `pyproject.toml` 声明，开发依赖放在 `dependency-groups.dev`；提交 `uv.lock` 和 `.python-version`，记录固定的 uv 版本。使用 `uv add "包名==版本"` 或 `uv add --dev "包名==版本"` 修改依赖，通过 `uv lock` 更新锁文件。本地及 CI 使用 `uv sync --locked --dev`，部署运行环境使用 `uv sync --locked --no-dev`。各 worktree 独立维护 `.venv`。

完整的非外部端到端测试命令为 `uv run --locked pytest test/ -m "not e2e"`；新增或修改测试后先运行对应文件。真实外部端到端测试必须标记为 `e2e` 并单独授权执行，日常集成测试使用本地模拟服务及测试夹具。首次实现功能时补齐测试配置、标记注册和自动化用例。

首次实现代码时配置 Ruff 和 mypy，固定依赖版本。代码修改后执行 `uv run --locked ruff check src test`、`uv run --locked ruff format --check src test`、`uv run --locked mypy src`，查看完整输出并处理诊断。当前尚未配置这些工具。纯文档修改检查内容、路径、示例和 `git diff --check`。

编辑前完整阅读文件，核查外部依赖的实际版本及 API，维护明确的 Python 类型和统一的配置、资源访问入口。依赖及锁文件变更须检查发布说明和兼容性影响。临时脚本写入系统临时目录后运行并清理。

并行协作时只暂存和提交本任务修改的明确路径，不使用批量暂存、破坏性清理或绕过钩子的命令。审查 PR 不切换工作分支，外部评论须经授权。用户可见的行为变更同步记录到 `docs/changelog.md` 的 `Unreleased` 部分，首次需要时创建；发布前遵循本项目的发布文档。

使用 `git revert <commit>` 回滚已提交修改，并同步文档和验证结果。脱敏样例数据与生成脚本随 Git 保存；运行时数据单独备份，数据迁移需提供恢复步骤。

实现方案见 [implementation_plan.md](implementation_plan.md)，其中包含需求对应、客户端—网关—推理服务端模拟程序架构、协议、密钥生命周期、测试、性能对比与阶段验收。方案处于设计阶段，尚未实现业务功能或产生实测数据。

## CI/CD 流程

使用 GitHub Actions，在 `.github/workflows/` 维护 CI 和 CD。首次实现可运行代码时配置 CI，在 PR 和 `main` 推送时从锁文件安装依赖，执行与本地相同的静态检查、非外部端到端测试和差异检查；覆盖 Windows 与 Linux，保存报告并将 CI 设为合并必要检查。

首次交付可部署产物前配置 CD，由版本标签或手动触发，确认同一提交通过 CI 后构建带版本、提交 SHA 和校验值的产物。先在测试环境验证，再经生产环境审批提升同一产物。部署后执行健康检查和冒烟测试，失败则恢复上一个已验证产物；数据迁移需独立备份及恢复验证。凭据由环境 Secrets 或短期身份管理。

当前仅规定 uv 和 CI/CD 的实施要求，尚无 `pyproject.toml`、`uv.lock`、工作流、部署目标或实测流水线结果。落地时记录工具版本、目标环境、构建方式、部署入口和回滚命令。
