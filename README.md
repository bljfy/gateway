# 国密大模型推理安全网关

Python 三程序原型：客户端通过国密安全会话连接网关，网关再通过独立安全会话连接推理服务端模拟程序。设计采用 SM2 身份认证与密钥保护、SM4-GCM 报文保护以及隐私化审计。

当前已提供公共类型与接口、uv 环境初始化、自动化检查和协作计划。密码后端、网络协议、网关业务与三个程序的运行入口尚待三条线路实现，当前不能作为可运行网关部署。

## 快速开始

Windows（PowerShell）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
```

Linux x86_64（Bash）：

```bash
bash scripts/bootstrap.sh
```

脚本下载并校验固定版本 uv，安装固定 Python、同步 `uv.lock`，执行静态检查和测试；首次运行需要联网。工具及环境保存在当前 checkout 的忽略目录，不需要系统 Python，也不修改系统 PATH。仅准备环境时使用 PowerShell 的 `-SkipChecks` 或 Bash 的 `--skip-checks`。

## 文档与协作

- [开发指南](docs/developer_guide.md)：一键环境、三线职责、分支、接口使用及最终 merge。
- [公共接口](docs/interfaces.md)：数据结构、调用边界和扩展约定。
- [实现方案与开发计划](docs/implementation_plan.md)：安全设计、三线计划及验收条件。
- [使用说明](docs/usage.md)：确定性模拟器与业务层编程接口。
- [Agent 入口](AGENT.md)：按任务定位编码、测试、依赖及 CI/CD 规范。

源码在 `src/`，测试在 `test/`，设计与规范在 `docs/`。环境初始化不安装尚未选定版本的 GmSSL 原生库，也不执行真实服务或生产部署；A 线负责后端验证，最终安全验收使用真实后端。
