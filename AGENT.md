# Agent 工作入口

本项目使用 Python，文档放在 `docs/`，源码放在 `src/`，测试放在 `test/`。本文件用于定位规范，具体规则在对应文档中维护。

## 开始任务

先阅读 [开发流程](docs/development.md) 和 [沟通与协作规范](docs/communication.md)，再按任务类型读取下表文档。编辑前完整阅读待修改文件；需要调查或审计时完整阅读相关文件。

| 任务 | 必读文档 |
| --- | --- |
| 编写或重构 Python 代码 | [编码规范](docs/coding.md) |
| 修改依赖、环境或锁文件 | [uv 依赖管理](docs/dependencies.md) |
| 编写测试、验证行为或性能 | [测试规范](docs/testing.md) |
| 修改 CI、发布、部署或回滚流程 | [CI/CD 规范](docs/ci_cd.md) |
| 确定功能范围、协议、架构或开发阶段 | [实现方案与开发计划](docs/implementation_plan.md) |
| 核对题目要求 | [原始需求](docs/offical_request.md) |

所引用的规范适用于对应任务，读取后再开展相关操作；文档中的链接可继续定位专项设计，无需每次加载全部文档。

## 文档维护

用户明确指令优先；变更、文档同步与交付要求见 [开发流程](docs/development.md#文档同步)。
