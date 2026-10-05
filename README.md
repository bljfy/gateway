# 国密大模型推理安全网关

Python 三程序原型：客户端通过国密安全会话连接网关，网关再通过独立安全会话连接推理服务端模拟程序。设计采用 SM2 身份认证与密钥保护、SM4-GCM 报文保护以及隐私化审计。

当前已提供公共接口、真实 GmSSL 密码后端、单链路安全协议与会话管理、uv 环境初始化和自动化检查。网关业务、客户端、模拟服务与三个程序入口仍待 B、C 线实现，当前不能作为完整网关部署。A 线交付与验证见 [协议文档](docs/protocol.md)。

## 快速开始

Windows（PowerShell）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
```

Linux x86_64（Bash）：

```bash
bash scripts/bootstrap.sh
```

脚本下载并校验固定版本 uv，安装固定 Python、同步 `uv.lock`，构建固定 GmSSL 并执行静态检查和测试；首次运行需要联网及 CMake、C 编译器。Windows 默认使用 Visual Studio C 工具链，也可按 [协议文档](docs/protocol.md#原生环境) 指定 GCC 与 Ninja。工具及环境保存在当前 checkout 的忽略目录，不需要系统 Python。仅准备 Python 环境时使用 PowerShell 的 `-SkipChecks` 或 Bash 的 `--skip-checks`。

## 文档与协作

- [开发指南](docs/developer_guide.md)：一键环境、三线职责、分支、接口使用及最终 merge。
- [公共接口](docs/interfaces.md)：数据结构、调用边界和扩展约定。
- [安全协议与 A 线交付](docs/protocol.md)：线格式、原生库重建、会话使用和实测范围。
- [实现方案与开发计划](docs/implementation_plan.md)：安全设计、三线计划及验收条件。
- [Agent 入口](AGENT.md)：按任务定位编码、测试、依赖及 CI/CD 规范。

源码在 `src/`，测试在 `test/`，设计与规范在 `docs/`。GmSSL 固定为 3.1.1、官方绑定为 `gmssl-python==2.2.2`；初始化仅在 checkout 内构建库并验证，不启动真实服务或部署。Windows 已完成 A 线验证，Linux 与云端 CI 运行结果待验证，最终三程序验收由后续集成完成。
