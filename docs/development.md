# 开发流程

项目开发规范见根目录 `AGENT.md`。文档保存在 `docs/`，Python 源文件保存在 `src/`，pytest 测试保存在 `test/`。

开始实现前，在 `pyproject.toml` 中声明 Python 版本、固定依赖以及 pytest 的 `testpaths = ["test"]`，并记录环境安装步骤。目前仓库尚未配置测试依赖，也没有可执行测试用例。

每项任务使用独立分支和 Git worktree。完成一次修改时，同步更新对应文档，运行相关检查后，将实现、测试和文档作为一个逻辑完整的 Git 提交保存。交付记录提交哈希和实际验证结果。

完整测试命令为 `python -m pytest test/`，提交前检查命令为 `git diff --check`。纯文档修改检查内容、路径、示例和差异格式；首次实现功能时补齐测试配置和自动化用例。

使用 `git revert <commit>` 回滚已提交修改，并同步文档和验证结果。脱敏样例数据与生成脚本随 Git 保存；运行时数据单独备份，数据迁移需提供恢复步骤。

实现方案见 [implementation_plan.md](implementation_plan.md)，其中包含需求对应、双链路架构、协议、密钥生命周期、测试、性能对比与阶段验收。方案处于设计阶段，尚未实现业务功能或产生实测数据。
