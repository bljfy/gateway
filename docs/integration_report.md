# 三程序整合报告

日期：2026-10-05。范围为 PR 合并后的缺陷修复、A/B/C 接线、运行入口、文档整理及本地端到端验证。

## 来源与实现

| 来源 | 提交 |
| --- | --- |
| A 安全核心主线 | `5d21ebf` |
| B PR [#1](https://github.com/bljfy/gateway/pull/1) 合并 | `27af720` |
| B 合并后会话修复 | `0374f565c742fcbe101fdcd8e542e06c1b582054` |
| C PR [#2](https://github.com/bljfy/gateway/pull/2) 冻结头部 | `7d61a7b84e5bf0de43e4cfe07bfaaf98ef99a71a` |
| C PR 合并及本轮整合基准 | `6e601bd7a6045cf4fcf7a1967e260250c27a4da5` |

本轮整合在 `codex/integration` 独立 worktree 完成，最终修复由包含本报告的提交记录。修复业务组装累计限额、提前结束流清理、客户端/模拟器心跳处理、模拟器头部与载荷 UUID 绑定；补齐整个编码载荷预算、共享配置限额、UTF-8 跨记录解码、单客户端串行读者与绝对业务超时。模拟器在推理期间监控入站关闭，取消并排空未完成工作。

`gateway.runtime` 提供严格角色 JSON、原生库清单加载、加密私钥导入和新建演示身份；CLI 提供客户端、网关和模拟器独立进程。网关定期排空白名单审计、绑定本机指标，停机关闭监听、会话及任务。启动与恢复步骤见 [使用说明](usage.md)、[配置说明](configuration.md)、[部署说明](deployment.md)。

## 验证环境与命令

Windows x86_64，Python 3.12.11，uv 0.8.22，锁定 `uv.lock`，GmSSL 3.1.1，官方绑定 2.2.2。使用经校验的本地 DLL，原生 SHA256：`9df4ccb9fd007dac248034e36b69510d030ddc6e1c4736ce36e6c5427ed12689`；GmSSL 源码提交 `d655c06b3a6b0fe8cff900f293bf0e5aac6eb0a2`。运行时随机生成身份和会话材料，业务输入为固定合成标记与重复字符。

原生环境设置见 [测试规范](testing.md#本地端到端验证)，验证命令：

```powershell
.\.tools\uv\uv.exe run --locked ruff check src test scripts
.\.tools\uv\uv.exe run --locked ruff format --check src test scripts
.\.tools\uv\uv.exe run --locked mypy src scripts
.\.tools\uv\uv.exe run --locked pytest test/ -m "not e2e" --junitxml=artifacts/junit.xml
git diff --check
```

全量非外部测试：275 通过、5 跳过，27.20 秒；其中 21 个真实后端集成用例全部通过。5 项跳过是未配置 Windows Git Bash 的 Linux bootstrap 回归，不计为通过。Ruff 检查与格式、mypy（23 个源码文件）和 diff 空白检查通过。独立审查发现接收方向未应用较低记录限额，已修复并补齐普通响应、流响应及模拟器请求的拒绝回归；提交前完成最终复审。

## 端到端证据

- 两段实际 TCP 使用真实 SM2/SM4-GCM；普通与流式请求各覆盖 1 KiB、16 KiB、70000 字节，核对确定性输出、仅一个流末片、上下游独立会话标识和密钥指纹。
- 捕获两段线帧扫描合成提示词和检索标记；审计扫描提示词、输出及私钥标记，指标无业务标签。
- 分别篡改客户端及上游请求记录，验证模拟推理未执行；重复客户端记录不重复执行业务，连接拒绝后续请求。
- 两段分别覆盖错误信任公钥和过期信任，另覆盖会话过期、轮换后业务、提前流关闭与重连、真实业务超时取消、并发调用串行化。
- 记录限额降低到 8 字节，普通/流式 Unicode 仍保持完整，字符跨记录边界正确解码。
- 三个独立 CLI 进程完成普通 1 KiB/16 KiB 与流式 70000 字节请求，检查成功指标、三条成功审计、停止文件停机及零错误退出。
- 两段测试关闭后断言无遗留会话和原生任务；独立进程关闭监听并正常退出。

报告产物为 `artifacts/junit.xml`、`artifacts/integration.json`，另保留网关与安全核心局部 JSON；不收集业务载荷、私钥、口令或异常全文。临时测试身份和缓存不提交。

## 实测边界与后续验收

本轮证明 Windows 本地三程序功能和上述安全/清理路径。没有执行真实模型或外部服务器测试；Linux 最终提交的运行结果以 CI 为准。尚未完成实现方案第 12 节的吞吐、握手时延、CPU/RSS、隐私审计开销与 TLS 1.3 对照，也未执行版本化发布、生产部署和生产回滚演练。

首版采用预置公钥身份和 SM2 密钥传送，不提供前向保密；Python/原生库内存无法承诺所有密钥副本物理清零。网关每次请求新建上游会话，客户端单会话业务串行。提前关闭流必须显式关闭迭代器，后续请求重建会话。上述边界与设计保持一致，不能将本轮测试视为生产安全认证或性能验收。
