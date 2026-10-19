# 会话恢复与连接池验证

验证日期：2026-10-19。协议、配置、部署迁移与安全边界见[会话恢复说明](session_resumption.md)，性能口径见[性能方法](performance.md)。

## 功能与静态检查

使用 Python 3.12.11、uv 0.8.22、GmSSL 3.1.1、gmssl-python 2.2.2 和原锁文件。测试使用临时随机密钥与本地模型替身。

| 环境 | 完整非外部回归 | 最终受影响用例 | 静态检查 |
| --- | --- | --- | --- |
| Windows x86_64 | 511 通过、6 平台跳过、1 外部模型用例排除，258.17 秒 | 启动认证加固后 19 通过、1 POSIX 跳过；写入合并与持久集成 22 通过、1 POSIX 跳过 | Ruff check、format --check（src/test/scripts）、mypy（src/scripts）通过 |
| Ubuntu 24.04 / WSL2 | 517 通过、1 外部模型用例排除，409.09 秒 | 存储与启动器 25 通过，49.37 秒；写入合并与持久集成 23 通过，15.10 秒 | Ruff check、format --check（src/test/scripts）、mypy（src/scripts）通过 |

Windows 原生库 SHA256 为 `9df4ccb9fd007dac248034e36b69510d030ddc6e1c4736ce36e6c5427ed12689`；Linux 为 `3bcb28dd198d335e979bb192ee03027079ef20e56e0eb5af944355e9d9fcc258`，Linux SM2/SM3/SM4 原生自测 3/3 通过。哈希用于标识此次构建，不是跨平台固定值。

覆盖加密数据库重载、两个独立 client 进程恢复、双向新密钥、过期及不续期、信任与安全参数变化、binder/Finished 篡改、恢复请求重放、错主密钥、数据库篡改、排他锁与写入失败、时钟倒退、撤销与握手并发、运行中撤销 API、请求分片预算、池等待/取消/回收/停机以及批量退役重复取消。独立审查指出的池预算与取消清理问题已修复并复核，审查者独立执行池测试 14 项通过。

Linux 从 Windows 挂载盘运行时出现三进程启动/停机与启动器就绪超时，该次运行中止，不计为通过；将解释器、依赖及源码移至 Linux 本地文件系统后完成上表全量与追加回归。严格 POSIX 权限验证使用 Linux 文件系统。Windows 使用实际账户执行 DPAPI/ACL 验证。

资源采样证据检查在 Windows、Linux 各有 11 项最终回归通过。完整回归后增加的存储、采样检查与启动器修正执行了对应追加回归，未再次执行全部测试。

JUnit 保存在本地忽略目录 `artifacts/resumption-windows-final.xml`、`artifacts/resumption-store-final.xml`、`artifacts/resumption-linux-final.xml`、`artifacts/resumption-linux-store-final.xml`、`artifacts/resumption-batched-windows.xml`、`artifacts/resumption-batched-linux.xml`、`artifacts/resumption-acceptance-final.xml`、`artifacts/resumption-linux-acceptance-final.xml`；该目录不进入 Git。

## 性能矩阵与边界

本轮使用三个独立进程、IPv4 回环和 Echo provider，比较完整握手、恢复、连接池、恢复加池及 TLS 1.3，每模式选择一轮有效数据。每模式两段各测 100 次握手，预热 5 次；业务覆盖 1 KiB/16 KiB、1/10/50/100 并发及普通/流式，审计开启，每档至少 200 次请求及 5 秒。

```powershell
python -m test.benchmark.run --manifest .tools/gmssl/manifest.json --modes gm gm-resume gm-pool gm-combined tls --rounds 1 --handshakes 100 --requests 200 --seconds 5 --sizes 1024 16384 --concurrency 1 10 50 100 --audit 1 --stream 0 1 --output artifacts/resumption-performance.json
```

这是功能与性能探索矩阵，未执行三轮、每档 1000 次/30 秒及审计关闭的完整长期负载验收。TLS 基线禁用恢复与池，保留其原身份与记录保护口径，不能据此证明相同功能条件下的普遍性能优势。原始 JSON 记录各次握手、分位时延、CPU、峰值 RSS、错误、审计和停机证据及源码文件哈希；生产吞吐、跨主机网络、真实模型、长期稳定性和本次远端 CI 均未验证。

## 本轮实测

汇总 JSON SHA256：`eef7b93d86bb1d59cda623d7e72836a4f940bd8ded8a3a588a90c77b2e6ea73b`。
共 80 档、87445 次成功业务、0 次业务失败。所有握手与审计/停机证据检查通过；最大采样间隔 0.152 秒。

| 模式 | 链路 | 握手样本 | 恢复样本 | 均值 ms | P95 ms |
| --- | --- | --- | --- | --- | --- |
| gm | A | 100 | 0 | 25.486 | 27.619 |
| gm | B | 100 | 0 | 25.651 | 27.731 |
| gm-resume | A | 100 | 100 | 8.123 | 9.189 |
| gm-resume | B | 100 | 100 | 7.887 | 9.834 |
| gm-pool | A | 100 | 0 | 25.565 | 28.129 |
| gm-pool | B | 100 | 0 | 25.856 | 28.546 |
| gm-combined | A | 100 | 100 | 7.852 | 9.038 |
| gm-combined | B | 100 | 100 | 8.014 | 9.151 |
| tls | A | 100 | 0 | 3.968 | 5.028 |
| tls | B | 100 | 0 | 4.253 | 5.183 |

下表比较单并发普通请求；峰值 RSS 为三个进程采样的总量，CPU 为三进程合计除以逻辑核数的整机百分比。全部 80 档的原始统计及各角色 CPU/RSS 保存在 JSON，摘要为 `artifacts/resumption-performance-summary.csv`。

| 模式 | 载荷 KiB | 平均往返 ms | P95 ms | 双向 MiB/s | CPU % | 峰值 RSS MiB |
| --- | --- | --- | --- | --- | --- | --- |
| gm | 1 | 30.230 | 33.055 | 0.065 | 7.12 | 214.3 |
| gm | 16 | 36.587 | 50.567 | 0.854 | 6.80 | 259.8 |
| gm-resume | 1 | 12.638 | 14.326 | 0.154 | 7.89 | 245.0 |
| gm-resume | 16 | 19.663 | 35.514 | 1.587 | 6.99 | 285.0 |
| gm-pool | 1 | 4.022 | 5.134 | 0.484 | 7.64 | 245.4 |
| gm-pool | 16 | 11.078 | 14.520 | 2.815 | 6.73 | 254.9 |
| gm-combined | 1 | 3.901 | 4.903 | 0.499 | 7.69 | 274.5 |
| gm-combined | 16 | 9.350 | 11.150 | 3.333 | 6.48 | 282.2 |
| tls | 1 | 6.921 | 8.269 | 0.281 | 8.04 | 214.8 |
| tls | 16 | 8.407 | 11.479 | 3.704 | 7.69 | 231.6 |

本机恢复握手均值从约 25.5 毫秒降至约 8 毫秒，降低约 68%。单并发 1 KiB 普通请求启用恢复与池后平均往返从 30.230 毫秒降至 3.901 毫秒；单独启用池为 4.022 毫秒，说明此负载中主要收益来自上游连接复用。这些数值仅对应上述本地替身负载。

首轮测量曾出现 398.63 秒的资源采样间隔，伴随业务超时；保存在 `artifacts/resumption-performance-interrupted.json`，未计入上表。工具新增超过 1 秒采样间隔的拒收检查，并以独立回归验证。该停顿的外部原因未确认。另一次调整前测量发现恢复握手均值约 27 毫秒，随后主动中止，保存在 `artifacts/resumption-performance-before-batching.json`。调查发现读取时同步写入的开销，调整为每秒更新时钟高水位、合并持久化最近使用顺序后完成上表独立重测；新凭据与撤销仍同步提交。两次未完成运行均未计入最终统计。

运行开始后仅对基准工具的相邻采样 `zip` 显式补充 `strict=False` 以通过静态检查，行为不变；JSON 的工具源码哈希对应启动时版本，其余被测实现与最终版本一致。

最终汇总不是一次完整命令的输出。`resumption-performance-gm-matrix.json` 中前四种国密模式完成全部 64 档及审计／停机检查；后续 TLS 出现 64.62 秒采样停顿、27 次超时后主动中止，该文件的整轮验收不通过。其全部 TLS 样本排除，另用 `resumption-performance-tls.json` 独立完整重测 16 档；汇总仅选择这 64+16 档。`artifacts/resumption-aggregate.py` 保留选择规则并重新检查计数、预算、审计、停机与采样间隔；派生 JSON 的 aggregation 保留原命令、各源 SHA256、原源码哈希、排除项及原轮次失败原因，未修改原报告。
