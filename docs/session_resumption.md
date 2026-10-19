# 会话恢复、本地密钥存储与连接池

首次连接执行 SM2 双向认证和密钥传送。认证成功后，双方保存独立的 32 字节恢复秘密，后续连接用其证明身份并派生新 SM4 密钥。业务流量密钥只存在进程内；连接池保留健康连接，沿用其递增序号。

## 配置与启用

三端同步升级至握手 v2，套件为 `SM2-SM4-GCM-SM3-v2`；记录头及业务编码仍为 1。不接受 v1 握手，也不自动降级。旧配置未设置新开关时执行 v2 完整握手，不启用恢复或连接池；显式配置旧套件的部署须修改套件名称。首次升级前备份角色 JSON、共享 TOML、身份文件与口令，保持原文件权限。

共享 TOML 的 `[gateway]` 新增：

```toml
resumption_enabled = true
resumption_lifetime_seconds = 1800
pool_enabled = true
pool_max_connections = 8
pool_max_waiters = 100
pool_idle_seconds = 60
```

恢复有效期必须大于请求超时加轮换余量，且不超过 1800 秒；其他数字必须为正整数，开关必须为布尔值。三端使用相同会话策略、字节及在途预算、请求超时与恢复有效期。身份、公钥、版本、套件或这些安全参数改变后，旧凭据无法恢复。

每个角色 JSON 新增本地路径，均相对 JSON 所在目录解析：

```json
"secret_store": {
  "directory": "client.sessions",
  "master_key_file": "client.secrets/master.key"
}
```

网关与 adapter 使用各自独立目录。`init-demo` 自动创建并启用这些配置；不会覆盖现有目录。已有部署先创建受限主密钥父目录，再运行 `guomi-gateway session-keys init --config <角色.json>`，成功后开启恢复。Linux 主密钥父目录须为当前服务账户所有且权限 `0700`，文件生成权限为 `0600`；数据库目录为 `0700`。主密钥必须位于数据库目录之外，可由运维挂载。Windows 使用当前用户 DPAPI 保护主密钥，目录及文件仅允许当前账户、SYSTEM、Administrators 和已验证所有者的 OWNER RIGHTS。

每个存储目录只允许一个活动进程持有。顺序运行的 client 命令可跨进程恢复；多个客户端进程并发运行须配置各自独立存储目录。每个账户与角色独立托管，不能将 Windows 的主密钥文件复制给其他账户解锁。手动协议实验禁用恢复，使用独立运行对象，不更新其身份文件。

## 协议与派生

仍使用协议模块的 `F` 长度编码。完整握手 ClientHello 包含 `F(02, suite, initiator, acceptor, Nc, capability)`，capability 为一个字节的 0 或 1。ServerHello 的已签名 body 为 `F(Ns, sid, lifetime_u32, timeout_u32, ticket, expiry_u64)`；双方均启用恢复时 ticket 为 32 字节，否则为空且 expiry 为零。签名域为 `server-hello-v2` 和 `client-key-v2`。ClientKey 的 SM2 明文共 104 字节：双向 SM4 密钥 32、nonce 前缀 8、握手确认密钥 32、恢复秘密 32。Finished 域为 `server-finished-v2`、`client-finished-v2`。接收方完成双向确认并持久化后发送认证 READY，发起方验证 READY 后持久化，任一步失败不交付 ACTIVE。

恢复消息：

| 类型 | 内容 |
| --- | --- |
| 6 | `06 || F(body, binder)`，body 为 `F(02, suite, initiator, acceptor, ticket, Nc, binding)` |
| 7 | `07 || F(server, server_mac)`，server 为 `F(Ns, new_sid, lifetime_u32)` |
| 8 | `08 || client_mac` |
| 9 | `09`，仅表示凭据缺失或剩余寿命不足，不激活会话 |

binding 为套件、按发起方/接收方排序的身份及双用途公钥、规范排序的本地安全策略 JSON 经 F 编码后的 SM3。binder 为恢复秘密对 `F(resume-binder-v2, body)` 的 HMAC-SM3。`salt=SM3(F(message6, server))`；按 RFC 5869 的 HKDF-Extract/Expand，以恢复秘密为 IKM、`F(gateway-resume-v2, binding)` 为 info，派生 72 字节材料，布局同原流量与 Finished 材料。

`T=SM3(F(message6, server))`；server_mac/client_mac 使用派生的确认密钥，对 `F(resume-server-v2,T)` / `F(resume-client-v2,T)` 计算 HMAC。双方完成确认后，接收方仍发送以 T 为明文的加密 READY。比较使用恒定时间函数。每次恢复的新挑战和会话 ID 独立生成，不发送提前业务数据；未完成确认的重放不能交付业务记录。

恢复凭据到期时间从完整认证开始计算，最多 30 分钟，并受接收方信任有效期约束；恢复不延长它。恢复连接同时受凭据墙钟期限、会话单调时钟绝对期限、空闲期限和原记录/字节预算约束。剩余寿命不足请求超时加排空余量时重新完整握手。仅凭据缺失或过期允许在业务发送前另建连接完整认证一次；binder、MAC 或记录认证失败直接关闭，不重试业务。

## 存储、撤销与故障

恢复秘密及身份/到期元数据以加密信封写入 SQLite，ticket 是可公开的随机索引。每次写入产生独立 32 字节盐，以 HKDF-SM3 从随机本地主密钥派生 16 字节 SM4 密钥及 12 字节 nonce，使用 SM4-GCM 加密凭据与元数据；记录用途及 ticket 作为 AAD。新秘密使用事务和 FULL 同步立即提交；时钟高水位单独认证，每秒最多更新一次。最近使用顺序在内存更新，新增凭据或正常关闭时合并落盘，异常退出最多影响淘汰顺序，不改变密钥和到期时间。最多保存 1024 条记录，查询清理过期项，容量满后淘汰最久未使用项。

数据库、主密钥、所有者或权限错误均拒绝启动；运行时存储异常锁定 unhealthy 并拒绝新握手。已有健康连接可使用到原期限。主密钥缺失时不自动生成替代密钥。显著时钟倒退会清空恢复凭据，随后必须完整认证。持久化到期判断依赖正确系统时钟；此保护不抵抗整个数据库和系统时钟一起回滚，也不抵抗恶意冻结时钟。随机新挑战不能代替可信时钟或硬件防回滚。

停止对应角色进程后，可使用：

```text
guomi-gateway session-keys status --config <角色.json>
guomi-gateway session-keys revoke --config <角色.json> --peer <对端ID>
guomi-gateway session-keys revoke --config <角色.json>
```

运行中的 WebUI 提供 `GET /api/session-keys` 和 `POST /api/session-keys/revoke`，沿用同源、Host、Origin 与 `X-WebUI-Request` 检查。撤销请求体为 `{"peer_id":"adapter"}`，空对象表示全部撤销；撤销立即关闭本进程相关会话。直接嵌入可调用 `await manager.revoke(peer_id=None)`。状态只返回数量、对端 ID 和到期时间，不返回 ticket 或秘密。各端撤销独立；需要停止某身份全部访问时还应在信任配置停用它并重启。

恢复缓存损坏且需重建时：停止角色、备份故障目录及主密钥、将两者移至受限隔离位置，再显式初始化新存储；不要覆盖原 SM2 身份。回滚软件前关闭新开关并恢复对应版本 TOML，保留受限备份以便恢复。删除是逻辑撤销，不保证 SQLite 空闲页、SSD 或备份的物理擦除。Python 与原生临时副本同样不承诺完全清零。

## 连接池与可观察性

连接池只用于 GatewayServer 的网关→adapter 链路；ForwardingService 保持原行为。每连接一次一个请求，健康连接只有在完整最终响应转发成功且终结审计接纳后才归还。取消、超时、截断、错误响应或认证失败时淘汰。等待计入原业务截止，默认最多 8 个连接和 100 个等待者，同时受原会话/待建立限额约束；空闲 60 秒回收。租用前检查信任、身份和安全策略绑定、状态、剩余寿命及方向预算。发送方向预留本次实际请求分片数；接收方向预留半个方向总记录预算，且不少于最大响应按记录上限分片所需条数加一。不满足时重新建立连接。这是保守退役策略，不能保证任意细粒度流必能完整结束；达到硬记录预算时关闭，不重放业务。池中已有连接不断号、不重置 nonce；批量退役时先登记并启动所有关闭任务，取消也等待有界清理完成，停机收回所有连接和等待任务。

本机指标增加 `full_handshakes_total`、`resumed_handshakes_total`、`resume_rejected_total`、`credential_store_healthy` 和 `pool_hit_total`、`pool_wait_total`、`pool_evicted_total`，HTTP 输出带 `gateway_` 前缀。日志、指标和测试报告不记录恢复秘密、SM4 密钥、完整 ticket 或业务正文。

设计借鉴 [TLS 1.3 会话恢复](https://www.rfc-editor.org/rfc/rfc8446.html#section-2.2)、[HKDF](https://www.rfc-editor.org/rfc/rfc5869) 与 [GCM IV 唯一性要求](https://csrc.nist.gov/pubs/sp/800/38/d/final)。本协议为项目专用原型，不提供 TLS/TLCP 互操作、前向保密或密码模块合规认证。恢复秘密失陷会影响该凭据窗口内的恢复连接；本地加密不能抵抗已控制服务账户或管理员的攻击者。
