# 安全协议与 A 线交付

公共契约版本 1.0。实现入口为 `gateway.crypto.GmSSLBackend` 和 `gateway.session.SecuritySessionManager`。已实现真实 SM2 双向认证、72 字节密钥材料传送、双向密钥确认、SM4-GCM 记录、严格方向序列、时间与使用预算、轮换、认证关闭和异常清理。该协议是专用原型协议，不具备 TLS/TLCP 互操作能力，也不提供前向保密。

## 原生环境

| 项目 | 固定内容 |
| --- | --- |
| Python / uv | 3.12.11 / 0.8.22 |
| 官方绑定 | `gmssl-python==2.2.2`，由 `uv.lock` 锁定；不能替换为同名 `gmssl` 包 |
| GmSSL | 3.1.1，提交 `d655c06b3a6b0fe8cff900f293bf0e5aac6eb0a2` |
| 源码 | [官方 v3.1.1 源码归档](https://github.com/guanzhi/GmSSL/archive/refs/tags/v3.1.1.zip) |
| 源码 SHA256 | `edc33efd90cedddf061aee8295dc247dadbb3df7f5f96154ed699831c87d3416` |
| 本地已验证平台 | Windows x86_64，GCC 15.2.0、CMake 3.31.8、Ninja 1.12.1 |

构建工具须预先安装；环境脚本不安装系统编译器。使用已有 CMake 与默认 C 编译器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap.ps1 -SkipChecks
.tools/uv/uv.exe run --locked python -m gateway.crypto.build_native
```

Windows 使用 GCC 时指定 Ninja 和 GCC 路径：

```powershell
.tools/uv/uv.exe run --locked python -m gateway.crypto.build_native --cmake C:/tools/cmake/bin/cmake.exe --ninja C:/tools/ninja/ninja.exe --compiler C:/tools/gcc/bin/gcc.exe
```

Windows 默认 CMake generator 使用 Visual Studio 的 C 工具链；GCC 需要 `--export-all-symbols`，构建模块自动加入此链接参数。构建先核对源码校验值，再运行原生 SM2、SM3、SM4 测试；失败不生成成功清单。库与清单位于 `.tools/gmssl/lib/` 和 `.tools/gmssl/manifest.json`，不进入 Git。二进制哈希依编译器与构建产物而定，部署需保存同一产物的清单，不能仅信任版本字符串。

独立检查前，在同一 PowerShell 会话设置：

```powershell
$nativeManifest = Get-Content .tools/gmssl/manifest.json -Raw | ConvertFrom-Json
$env:GMSSL_LIBRARY = $nativeManifest.library
$env:GMSSL_SHA256 = $nativeManifest.sha256
$env:PATH = (Split-Path -Parent $nativeManifest.library) + ';' + $env:PATH
.tools/uv/uv.exe run --locked ruff check src test scripts
.tools/uv/uv.exe run --locked ruff format --check src test scripts
.tools/uv/uv.exe run --locked mypy src scripts
.tools/uv/uv.exe run --locked pytest test/ -m 'not e2e' --junitxml=artifacts/junit.xml
```

完整 bootstrap 会读取该清单并设置测试进程的加载路径。原生库必须在进程首次导入绑定前完成配置；后端检查文件哈希、绑定版本、原生版本与实际加载路径，不允许进程中途切换不同原生库。

Linux 使用同一构建模块，设置 `GMSSL_LIBRARY` 为 `.tools/gmssl/lib/libgmssl.so.3.1` 的绝对路径，`GMSSL_SHA256` 来自清单，`LD_LIBRARY_PATH` 首项为该库所在目录。模块核查 `/proc/self/maps` 中实际映射的库。Linux 构建、MSVC 构建及云端 CI 尚未本地验收；CI 已提供 Windows/Linux 构建与测试步骤，须以实际运行记录确认支持。

## 密码编码与密钥提供

SM2 私钥接口使用 32 字节大端标量，公钥使用 65 字节未压缩点 `04 || X || Y`，公钥点由原生库校验。签名与密文均使用 GmSSL 3.1.1 的 ASN.1 DER 编码；签名长度 8–72 字节，SM2 明文长度 1–255 字节。握手已验证能传送 72 字节材料。

`sign_sm2` / `verify_sm2` 使用完整消息签名上下文 `Sm2Signature`，signer ID 明确为相应身份的 UTF-8 编码，最长 64 字节。随机数调用原生 `rand_bytes` 并检查返回值。`seal_sm4_gcm` 返回密文与 16 字节标签，nonce 固定为 12 字节；解密暂存输出直到 `finish()` 验证成功才返回。

`generate_keypair()` 供本地身份准备及合成测试使用。签名与解密必须各用独立长期密钥。`export_private_key(private, password)` 返回原生口令加密 PKCS#8 `EncryptedPrivateKeyInfo` DER；`import_private_key(der, password)` 解锁后提供契约所需的私钥字节。口令为 16–1024 字节，禁止 NUL。加密 DER 文件、秘密文件与本机 ACL 由 B 的配置层提供；该接口不接收网络上传的密钥文件。私钥字节不得写入配置、日志或命令行。

会话密钥使用可覆盖缓冲区，关闭、失败、超时和取消时覆盖并释放。Python 临时 bytes、ctypes 上下文及原生副本不能保证全部清零，此实现不承诺彻底不可恢复。长期身份对象由配置层持有，其进程内释放和文件 ACL 仍属于集成边界。

## 规范线格式

所有整数均为无符号网络序。每帧前置 `uint32` 内容长度，内容范围 1–131072 字节；接收方在读取内容前检查长度。字段编码函数 `F(a,b,...)` 对每个字段添加 `uint16` 长度，按位置连接；解析要求字段数量精确、没有尾随字节。

身份为 `F(UTF8(peer_id), ASCII(role), uint32(key_version))`。身份长度 1–64 字节，角色为 `client`、`gateway`、`simulator`，密钥版本 1–2³²−1。握手消息类型是单字节前缀：

| 类型 | 内容 |
| --- | --- |
| 1 ClientHello | `01 || F(01, suite, initiator_identity, acceptor_identity, Nc)` |
| 2 ServerHello | `02 || F(body, signature)`；`body = F(Ns, session_id, uint32(lifetime), uint32(handshake_timeout))` |
| 3 ClientKey | `03 || F(SM2_ciphertext, signature)` |
| 4 ServerFinished | `04 || HMAC-SM3(K_finish, F(server-finished, T))` |
| 5 ClientFinished | `05 || HMAC-SM3(K_finish, F(client-finished, T))` |

suite 为 ASCII `SM2-SM4-GCM-SM3-v1`。`Nc`、`Ns` 各 32 字节，会话 ID 为接收方随机生成的 16 字节，并检查待建立及活跃集合冲突。双方身份、角色、密钥版本与挑战通过完整 ClientHello 绑定到签名。

ServerHello 签名输入为 `F(server-hello, SM3(ClientHello), body)`；ClientKey 签名输入为 `F(client-key, SM3(F(ClientHello, ServerHello)), SM2_ciphertext)`。`T = SM3(F(ClientHello, ServerHello, ClientKey))`，包含完整签名消息。双方 Finished 使用不同域并恒定时间比较；接收方先验证 ClientKey 签名，再解密。

密钥材料顺序为发起方向 SM4 key 16 字节、接收方向 key 16 字节、发起方向 nonce 前缀 4 字节、接收方向前缀 4 字节、Finished key 32 字节。Finished 后释放确认密钥，仅保留前 40 字节记录保护材料。

接收方发送认证 READY，明文为 `T`，使用接收方向序号 0。发起方验证 READY 后进入 ACTIVE；接收方完成 READY 写入后进入 ACTIVE。后续接收方向序号从 1 开始，发起方向从 0 开始。握手期间业务接口不可用。

记录固定头部共 52 字节，格式 `!BB16sBQ16sIBI`：

| 字段 | 长度 |
| --- | --- |
| 版本 / 类型 | 各 1 字节；版本仅 1，类型沿用契约枚举 0–7 |
| 会话 ID / 方向 | 16 / 1 字节；方向 0 发起→接收，1 接收→发起 |
| 序号 / 请求 UUID | 8 / 16 字节 |
| 分片号 / 结束标志 | 4 / 1 字节；结束标志只能 0 或 1 |
| 密文与标签总长度 | 4 字节；16–65552 字节 |

`nonce = prefix_direction || uint64(sequence)`，AAD 为全部 52 字节头部；帧内容为 `header || ciphertext || tag`。明文上限 65536 字节。READY、HEARTBEAT、CLOSE、CLOSE_ACK 使用全零 UUID、分片 0、结束标志 1；除 READY 外控制载荷为空。业务记录 UUID 不能为零。

按会话、方向及严格期望序号检查记录，标签成功后更新接收计数、解析控制语义并返回 `VerifiedRecord`。所有控制与业务记录共享本方向计数；发送锁覆盖序号分配、密码操作和写入，失败不回退序号。分片连续性、业务请求总量、截断结束和取消传播由 B、C 的业务层落实，安全模块保护这些字段并约束每条记录。

## 会话工厂与生命周期

B、C 通过可信配置创建 `LocalIdentity` 与 `TrustRecord`，再注入 `SecuritySessionManager(backend, local, trust, policy)`。trust 以 peer ID 索引，记录包含两个固定公钥、精确角色与版本、启用状态、墙上时钟有效区间和可选出站地址。仅允许 client→gateway 和 gateway→simulator；其他角色组合拒绝。`open(peer)` 只能解析 trust 中固定地址，不接受业务传入目标。`accept(reader, writer)` 负责认证和失败后的传输关闭。

manager 限制活跃及待建立总量、待建立量和密码执行并发。密码上下文不跨任务共享；原生操作在线程执行，即使任务被重复取消也等待当前原生操作结束才释放执行槽。manager 关闭会取消并等待正在建连和握手的任务，停止新密码操作，并等待已启动的原生操作结束。默认活跃上限 1000、待建立上限 100、密码并发 4、每会话在途请求 16、每方向明文字节预算 1 GiB。具体参数由 B 配置入口校验并传入。

`send` 内部分配序号，`recv` 要求单一调用者。`send` 不开放 READY/CLOSE/CLOSE_ACK，关闭使用 `close()`。通信期限为绝对寿命与空闲期限中较早者；每次使用及认证完成后检查硬期限，定时器在没有 API 调用时也释放过期会话。READY 计入报文与字节预算。

进入寿命的轮换提前区间后状态为 DRAINING，允许已登记 UUID 的响应与排空，不接纳新请求。`rotate(outbound_session)` 建立全新握手后将原会话排空；新会话供调用者切换，新旧密钥、ID 和计数独立。轮换失败不延长旧期限；入站轮换由对端发起新连接。到达硬期限即关闭。

`close()` 发出认证 CLOSE，等待认证 CLOSE_ACK 或同时关闭，最多等待配置的关闭超时；业务接收循环遇到 CLOSE 时发送 ACK、清理并抛出 `SessionClosedError`，收到预期 CLOSE_ACK 后也使用该类型。它继承 `ProtocolError`，保留现有异常捕获兼容性，消费者应先单独识别正常认证关闭，避免误记为协议拒绝。HEARTBEAT 由 `recv()` 交付，业务接收循环应消费它而不推进分片索引。断连、错误记录、认证失败、预算用尽或取消均清理；`close()` 可重复调用。调用者应先停止 listener 接纳，再关闭 manager，最后等待 listener 完全关闭。

## 验证记录

2026-10-05 在上述 Windows 环境从固定源码重建原生库，原生 SM2、SM3、SM4 测试 3/3 通过。重建 DLL SHA256 为 `1bec8a0f350bebe998b729bee1c03dabbf3fc84433597fcd765340299b89a0e4`；这是本次构建产物的记录，不是跨环境固定二进制哈希。

安全单元测试在 `test/unit/security_core/`：SM3 已知答案、[RFC 8998 A.1 SM4-GCM 向量](https://www.rfc-editor.org/rfc/rfc8998.html#appendix-A.1)、独立 HMAC-SM3 对照、SM2 signer ID/错误公钥、72 字节加密、加密私钥存储，以及本地 TCP 正常通信、篡改、重放、跨会话、反射、并发序号、时间与预算、轮换、关闭、取消、容量与碰撞清理。载荷由测试构造，密钥每次使用真实安全随机源生成。

pytest 生成 `artifacts/security-core.json`，记录每个用例编号、输入摘要、修改位置、步骤、预期、实际状态、耗时与判定；完整命令生成 `artifacts/junit.xml`。A 分支基于 `3516243` 独立验证时全仓库 94 项非外部测试通过，其中安全核心 71 项；Ruff、格式检查、mypy 与完整 Windows bootstrap 通过。独立审查结果见交付说明。当前验证是 A 线单链路验证，尚未完成三程序、双链路业务、隐私审计、100 次性能实验、TLS 对照、部署与回滚验收。
