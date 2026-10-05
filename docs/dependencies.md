# uv 依赖管理

统一使用 uv 管理 Python、项目依赖、锁文件和虚拟环境。本地与流水线使用相同声明和锁文件，不用临时 pip 安装绕过声明或混用其他依赖管理流程。

## 版本与文件

- `pyproject.toml` 声明 Python 兼容范围、运行依赖及 `dependency-groups.dev`；pytest、pytest-asyncio、Ruff、mypy 放在开发依赖组。
- `.python-version` 固定默认 Python 补丁版本，uv 工具版本在环境说明与流水线中固定并保持一致。
- 直接依赖使用明确版本，`uv.lock` 纳入 Git；每个 worktree 独立维护 `.venv`，虚拟环境不提交。
- GmSSL 原生库另存平台、版本或提交、来源和校验值清单，并通过安装脚本重建；Python 锁文件不覆盖原生库。
- 性能工具的开发依赖为 `cryptography==46.0.3`（临时 P-256 证书）与 `psutil==7.1.0`（进程 CPU/RSS），生产 `--no-dev` 不安装。版本与兼容范围核查依据 [cryptography 发布说明](https://cryptography.io/en/46.0.3/changelog/) 与 [psutil 文档](https://psutil.readthedocs.io/)；实验方法见 [性能对照](performance.md)。

## 依赖变更

使用 `uv add "包名==版本"` 添加运行依赖，`uv add --dev "包名==版本"` 添加开发依赖，`uv remove 包名` 删除依赖，通过 `uv lock` 更新锁文件。声明、锁文件和对应文档在同次变更提交，不手改锁文件。

依赖及锁文件按代码审查：记录更新动机、版本变化和兼容性影响，阅读目标版本发布说明并执行相关回归测试。不删除功能或弱化类型检查来迁就过时依赖；新增构建钩子或安装脚本先审查，不静默放宽执行限制或绕过提交检查。

## 环境重建与命令

```powershell
# 开发与 CI
uv sync --locked --dev
# 部署运行环境
uv sync --locked --no-dev
```

开发命令用 `uv run --locked`，部署启动用 `uv run --locked --no-dev` 或封装产物的入口。锁文件缺失或过期应失败，CI/CD 不自动重锁或升级。缓存仅用于加速，必须能在无缓存环境下重建。

实施时间与 Python 版本选择见 [实现方案](implementation_plan.md)。命令参考 [uv 锁定与同步](https://docs.astral.sh/uv/concepts/projects/sync/)。
