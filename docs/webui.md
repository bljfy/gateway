# 三角色本地 WebUI

客户端、网关与推理 adapter 各有独立中文页面。每个页面管理该进程的配置和状态，不集中远程控制其他角色。首版适用于可信实验主机，无登录、多轮对话或聊天记录持久化。

## 启动与操作

### 一键启动

Windows 首次完成 `scripts/bootstrap.ps1`，在项目根目录 `.env` 配置模型凭据后，双击根目录 `start.cmd`。启动脚本使用本地 uv 同步锁定依赖，首次生成 `.tools/demo` 身份配置，依次启动 adapter、网关和客户端的独立 WebUI 进程，自动启动 adapter／网关业务服务，等待就绪后打开三个页面。

```powershell
# 一键启动
.\start.cmd
# 修改三个 WebUI 端口
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1 -ClientPort 18090 -GatewayPort 18091 -AdapterPort 18092
# 只启动，稍后手动打开页面
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1 -NoBrowser
```

在客户端页面直接发送问题即可。保持启动窗口打开，按 Ctrl+C 依次关闭客户端、网关和 adapter；关闭浏览器页面不停止后台服务。启动失败会清理本次创建的进程，UI 或业务端口已被其他程序占用时拒绝启动。已有配置和 WebUI 旁文件会复用；配置不完整时修复后重新运行。

也可直接运行 `.\.venv\Scripts\guomi-gateway.exe webui-all`。支持 `--directory`、`--manifest`、`--client-port`、`--gateway-port`、`--adapter-port` 和 `--no-browser`；其中端口参数只改变 WebUI 端口，业务端口仍来自身份配置或旁文件。`webui-all --stop-file <路径>` 可通过创建停止文件正常退出，单角色 `webui` 也支持该选项；再次启动前移除已有停止文件。

### 分别启动

完成 bootstrap 并准备现有 SM2 身份配置；首次本地演示可执行 `guomi-gateway init-demo`。在三个终端分别运行：

```powershell
.\.venv\Scripts\guomi-gateway.exe webui --role client --config .tools/demo/client.json
.\.venv\Scripts\guomi-gateway.exe webui --role gateway --config .tools/demo/gateway.json
# 启动前在本进程环境或当前目录 .env 设置模型凭据
.\.venv\Scripts\guomi-gateway.exe webui --role adapter --config .tools/demo/adapter.json
```

Linux 使用 `.venv/bin/guomi-gateway`。不指定 `--config` 时读取 `.tools/demo/<角色>.json`；`--port` 可指定 UI 端口。UI 只绑定 `127.0.0.1`，分别在本机打开：

| 角色 | 浏览器地址 | 页面能力 |
| --- | --- | --- |
| client | http://127.0.0.1:18080 | 身份加载、目标选择、地址配置、认证测试、单轮推理、取消、复制和清空 |
| gateway | http://127.0.0.1:18081 | 监听／adapter 地址、服务启停、会话与请求指标、审计健康、脱敏事件 |
| adapter | http://127.0.0.1:18082 | 监听、模型 Base URL／凭据环境变量名、服务启停、认证与推理计数 |

启动 UI 后，业务服务保持停止。页面加载本机身份配置，修改地址并保存；依次启动 adapter 和网关。客户端可先测试网关 SM2 双向认证，再发送推理。默认模型 `deepseek-flash`、输出上限 512 token、流式开启；检索片段按添加顺序传递。推理结果按 Markdown 渲染标题、列表、表格、引用和代码块，隐藏响应首行的时间戳／UUID 元数据和结果区独立 UUID 标识。“复制结果”复制去除元数据后的 Markdown 原文，保留正文格式。

普通和流式结果使用相同展示逻辑；首行跨分片时先缓冲识别，不闪现部分元数据。只隐藏与当前请求 UUID 匹配且时间戳格式正确的约定元数据行，不删除正文中的普通 JSON、时间或标识。无换行的普通 JSON 在正常完成时展示；取消／失败时不展示尚未识别完成的可疑首行。局部正文保留并标记未完成。

Markdown 使用随包本地分发的 markdown-it，禁用原始 HTML，危险链接不生成可点击元素；合法 HTTP(S)、mailto 和锚点链接在隔离的新页面打开。图片显示替代文字，不自动加载模型提供的图片地址。渲染只影响客户端展示，原国密链路、响应元数据、CLI 和业务契约不变。

“认证成功”仅表示最近一次网关安全握手成功；每次请求仍独立建连。adapter 监听就绪不证明远端模型可用；只有本次运行已有响应发送完成时，页面才显示已有模型请求完成。

## 实时连接与客户端模拟

网关页面增加“实时连接监控”，每秒通过既有同源 WebSocket 刷新入站客户端连接。列表显示连接短标识、来源 IP／端口、认证身份、握手或已认证状态、持续时间、在途请求数，以及已认证载荷的接收／发送字节数；不包含提示词、输出、密钥或原始异常。来源地址不等于认证身份，多条连接可以使用同一个客户端身份。页面最多显示 200 条进行中连接并单独显示总数，最近结束记录最多 100 条，服务重启清空。结束原因区分认证失败、协议拒绝、超时、容量拒绝和服务停止；突然断开通常显示协议拒绝，不能据此断言发生攻击。此列表观察 client→gateway 入站连接，gateway→adapter 的上游会话仍通过汇总指标观察。

客户端页面增加“多客户端与异常连接模拟”。先保存网关连接设置并启动网关，再选择类型、客户端数（1–32）和最长保持时间（1–60 秒），点击“开始模拟”；网关页面可同时观察这些真实 TCP 连接。模拟使用当前客户端身份和已配置网关，每条正常连接独立握手，不调用模型服务，也不模拟不同身份或并发推理。

| 类型 | 行为与可观察结果 |
| --- | --- |
| 正常认证并保持 | 完成真实 SM2 认证，保持到指定时间后关闭；网关显示多条已认证连接 |
| TCP 建连后不发起握手 | 只建立 TCP；网关先显示握手中，在自身握手超时后关闭。如果保持时间更短，则由客户端先关闭 |
| 发送非法协议帧 | 发送固定非法帧，等待网关关闭；在最近结束列表观察协议拒绝 |
| 认证后突然断开 | 完成认证后直接中断 TCP，省略认证关闭报文；在最近结束列表观察异常关闭 |

页面展示每个模拟客户端的当前阶段和结果。“批次已结束”只代表所有模拟任务结束，具体结果以逐条记录为准；“网关已关闭连接”只表示收到 EOF，不单独证明拒绝原因，应对照网关记录。停止模拟、发起页面断开或 UI 退出会取消并排空本批次连接；模拟与认证测试、普通推理互斥。正常连接如遇服务停止或会话过期会提前结束。

## 手动协议与密钥实验

客户端的“手动协议与密钥实验”支持在一条真实连接上分步操作：先建连，再修改参数和发送记录，最后在网关连接列表查看结果。实验复用当前配置的网关端点；安全握手通过本机临时 TCP 中继观察原始帧和注入故障，中继只在本次实验运行期间监听回环地址。页面显示的是网关实际看到的客户端源 IP／端口，可与网关列表关联。

### 握手和密钥

“真实 SM2 安全握手”可使用当前身份，也可填写替代的客户端签名私钥、网关签名公钥和网关加密公钥。私钥输入为 32 字节十六进制，公钥为 `04` 开头的 65 字节未压缩点；留空使用原值。输入仅作用于本次新建的运行对象，提交后页面清空密钥输入框，快照和操作日志不返回密钥。握手故障下拉框可自动生成替代密钥，也可篡改 ClientHello 挑战、ServerHello／ClientKey 签名、双方 Finished 校验值。密钥故障选项优先于同一字段的手动密钥输入。声明身份、密钥版本和“信任已过期”选项分别验证未知身份、版本不匹配和客户端本地信任过期拒绝；它们不修改文件或网关的信任表。

“只建立 TCP”不自动握手，可手动发送原始帧或等待网关握手超时。安全连接建立后，可手动输入 16 字节 SM4 替代发送密钥，点击“应用替代密钥”，再发送记录观察认证失败；“恢复协商密钥”恢复后续发送使用的原协商值。替代密钥不影响客户端接收方向的验证，也不回写长期身份文件。

### 记录、重放和反射

记录编辑区支持记录类型、十进制 64 位序号、会话 ID、方向、请求 UUID、分片号、载荷及 nonce；序号、会话 ID 和 nonce 留空时使用当前会话值。心跳正常载荷为空；REQUEST 始终发送未结束分片，可测试分片乱序、请求超时和取消，不进入模型调用。其他记录类型可用于验证控制字段和不允许的消息类型。

“加密后篡改再发送”在认证加密完成后按偏移和 XOR 掩码翻转字节。偏移从记录头第一个字节算起，不含四字节长度前缀：版本 0、类型 1、会话 ID 2–17、方向 18、序号 19–26、请求 UUID 27–42、分片号 43–46、结束位 47、密文长度 48–51；其后为密文，最后 16 字节为标签。偏移留空时翻转最后一个标签字节。测试密文篡改时选择 REQUEST 并填写合成载荷，再把偏移设为 52。

“原样重放本连接上一帧”发送此前已发送记录的原始字节；“重放上次连接捕获帧”在新连接上发送前一实验保留的记录，验证跨会话拒绝。“原样反射网关记录”发送此前实际收到的网关保护记录（通常是 READY），不重新加密。测试旧 ClientKey 重放时，先建立一次正常安全连接并结束，再选“重放上次连接的 ClientKey”重新建立连接。所有捕获帧仅保留于本机内存，页面仅显示长度，不显示密文或密钥。

原始帧区输入帧内容的十六进制，发送时自动添加四字节长度前缀；例如 `ff` 用于验证非法握手帧。“突然断开 TCP”省略认证关闭流程。“结束实验”、页面断开、时限到达和 UI 退出均清理所属连接、中继及任务。每次实验最多 60 秒、64 次手动操作，载荷最多 4096 UTF-8 字节、原始帧最多 8192 字节；与推理、批量连接模拟及配置修改互斥。

### 判读结果

客户端的“已发送”只表示写入网络，不能作为网关拒绝或接受的证明。身份和密钥错误可能在客户端本地先被拒绝，网关可能只观察到断开；信任过期可在建连前被客户端拒绝。对照网关连接记录、认证失败／协议拒绝原因及计数判读。实验只覆盖页面列出的故障注入，不等同于运行完整安全测试套件。

例如，建立正常连接并发送空心跳后，改填一个 SM4 发送密钥并发送下一条心跳，可观察网关认证失败；重新建立连接、发送一条正常空心跳再点击原样重放，可观察序号重放被拒绝。加密操作仍调用既有真实 GmSSL，正常推理及协议实现不提供跳过校验的开关。

## 配置与生命周期

页面可填写本机配置文件路径，加载已有身份。只允许选择文件内已信任的目标身份；地址使用 IPv4／IPv6 字面量，端口为 1–65535。客户端选 gateway，网关选 adapter；常规配置不提供身份生成、公钥添加或信任策略修改，手动实验的替代参数只存在于独立运行对象。

保存设置前校验角色、原生库、身份和字段，写入身份 JSON 同目录的 `<文件名去扩展名>.webui.json`，原子替换。旁文件仅保存身份配置路径及本角色非敏感端点参数，加入 Git 忽略规则；原 JSON、TOML、密钥与口令文件保持原样。重新打开 UI 或加载同一配置时恢复旁文件。旁文件损坏时拒绝加载；人工移走损坏旁文件后可恢复原配置。模型 URL 沿用 AdapterConfig：远端必须 HTTPS，无 URL 凭据、查询或片段；回环 HTTP 仅用于测试。

服务运行或客户端操作期间禁止加载／保存配置。停止服务后允许修改；每次启动、连接测试与推理重新加载原身份配置并应用旁文件覆盖，创建新的运行对象。外部修改信任文件会在下一次创建运行对象时生效。`.env` 只在 WebUI 命令启动时读取当前目录；修改凭据需重新启动 WebUI 进程，页面停止／启动业务服务不会重新读取 `.env`。

客户端 UI 同时仅允许一个操作，每次请求拥有独立安全会话和 UUID。停止生成、关闭页面、WebSocket 断开、超时或截断均回收请求及会话，局部输出显示未完成。业务输出只发送给发起请求的页面，其他页面只收到状态与脱敏事件。网关／adapter 页面关闭不停止业务服务。页面按钮重复启停保持幂等；退出 WebUI 进程时关闭其拥有的业务服务、会话、provider 和审计任务。强制终止进程不保证完成审计排空，应先通过页面停止服务，再正常退出。

状态推送每秒一次。事件缓冲最多 128 条，每进程最多 8 个 WebSocket，每连接消息队列最多 32 项；普通响应分为最多 16384 字符的消息，流式消息受原记录限额限制。输出使用有界队列背压，入队／发送超过 5 秒则终止或关闭连接；状态和事件推送允许丢弃，下一快照恢复数值状态。计数在每次业务服务启动时重置；网关快照沿用原指标，重放计数仍是预留接线，不能把 0 当成未发生攻击的证明。

## 局域网三机示例

假设客户端、网关、adapter 的实验 IP 分别为 `192.168.1.10`、`.20`、`.30`：

| 主机 | 本地打开 | 页面连接设置 |
| --- | --- | --- |
| 客户端 `.10` | http://127.0.0.1:18080 | 已信任 gateway，目标 `192.168.1.20:18443` |
| 网关 `.20` | http://127.0.0.1:18081 | 监听 `192.168.1.20:18443`，已信任 adapter，目标 `192.168.1.30:19443` |
| adapter `.30` | http://127.0.0.1:18082 | 监听 `192.168.1.30:19443`，模型地址 `https://api.deepseek.com` |

在可信初始化机器生成一次配套身份，再通过带外安全渠道分发每个角色所需的 JSON、TOML、该角色加密私钥与口令文件。每台主机本地构建／校验原生库，更新自己的 `native_manifest` 路径；不要在三台机器分别独立运行 init-demo 后直接连接，独立生成的公钥不互信。对私钥与口令限制文件权限，原生库和路径配置遵循 [协议说明](protocol.md#原生环境)。实验网络只需允许业务端口 18443、19443；UI 和原指标端口保持回环访问。

## 安全边界

浏览器到本机 UI 服务使用 HTTP；只有 Python 角色间两段业务连接使用现有 SM2 / SM4-GCM，adapter 到远端模型使用 HTTPS。页面不是浏览器国密实现，也不改变网关可接触业务明文的原安全边界。回环无登录边界不隔离同机恶意进程，不应通过代理暴露 UI。

控制接口校验精确 `Host: 127.0.0.1:<UI端口>`；提供 Origin 时必须同源，写入操作及 WebSocket 必须提供同源 Origin。普通 API 还要求 `X-WebUI-Request: 1`，不启用 CORS。响应禁止缓存、限制脚本与连接为同源、防止被嵌入；静态资源不依赖 CDN。页面和事件不返回私钥、口令或 API Key，不在服务器持久化业务正文。错误返回固定代码，不回显异常全文。

## 本地 API

以下控制接口只用于本机页面，不是国密线协议，也不改变契约 2.0。JSON 请求严格限制字段和类型；HTTP 请求体上限 2 MiB，实际推理仍受编码后 1 MiB 等原业务限额约束。

| 方法与路径 | 输入／输出 |
| --- | --- |
| GET `/api/state` | role、phase、config_loaded、busy、error、最近请求 UUID／阶段、认证结果、数值 metrics |
| GET `/api/config` | 本角色 settings 与目标候选身份／地址／启用状态，不含信任公钥 |
| POST `/api/config/load` | `{config_path}`，加载该角色的本地身份与旁文件 |
| PUT `/api/config` | 完整 settings；必须先加载对应身份，禁止用保存操作切换身份 |
| POST `/api/start`、`/api/stop` | 网关／adapter 生命周期，返回 state；启动失败为 503 |
| POST `/api/test` | 客户端网关真实认证测试，202；最终状态通过 WebSocket 或 GET state 获取 |
| POST `/api/infer` | owner、prompt、model、retrieval_context 数组、max_output_tokens、stream；202 返回 request_id |
| POST `/api/cancel` | `{request_id}`；校验最近请求 UUID 后取消，错误 UUID 为 409 |
| POST `/api/simulation` | `{owner,count,duration,mode}`，客户端启动有界批次，202 返回 state；mode 为 normal／stalled_handshake／malformed_frame／abrupt_disconnect |
| POST `/api/simulation/stop` | 客户端停止当前模拟批次，返回 state；可重复调用 |
| POST `/api/manual/open` | `{owner,options}`，客户端创建手动实验；202 返回 state |
| POST `/api/manual/action` | `{owner,command}`，对发起页面拥有的手动连接执行操作；返回 state |
| GET `/api/ws` | 同源 WebSocket，只接收服务器推送，不接受客户端业务消息 |

客户端 settings 为 config_path、target_id、target_host、target_port；网关额外包含 listen_host、listen_port；adapter 为 config_path、listen_host、listen_port、base_url、api_key_env。WebSocket 首条 ready 返回随机 owner、state 和脱敏 events；后续为 state、event、output、finished。output 与 finished 仅发给对应 owner，包含 request_id；finished.result 为 completed／cancelled／failed，只有 completed 表示收齐最终响应。

role 错误为 404、运行中修改为 409、来源错误为 403、输入错误为 400、非 JSON 请求为 415、超过 HTTP 限额为 413。错误体仅为 `{error:固定代码}`。

state 新增 `connections` 和 `simulation`：网关 connections 含 active、recent、total_active、truncated；客户端 simulation 含 phase、mode、clients，每个 client 含 client、phase、result。连接统计字节只计算已认证记录的明文载荷长度，不是 TCP 线速流量。快照仍仅发给本机控制面。

手动实验 options 支持 mode（secure／raw）、duration、fault、identity、key_version、expired_trust，以及 signing_private／gateway_signing_public／gateway_encryption_public 替代值。command.action 为 key、restore_key、send、tamper、replay、cross_session、reflect、raw、abort、close；key 使用 key 字段，raw 使用 raw 字段，send／tamper 支持 sequence（十进制字符串）、session_id、direction、record_type、request_id、chunk_index、payload、nonce，tamper 额外支持 offset 和 xor。故障枚举与界面一致。不存在的连接、错误 owner 或已结束连接返回 409，参数错误为 400；接口不接受额外目标地址。state.manual 仅包含阶段、结果、源地址、下一序号、捕获长度、替代密钥状态和脱敏操作记录。
