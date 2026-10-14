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

## 配置与生命周期

页面可填写本机配置文件路径，加载已有身份。只允许选择文件内已信任的目标身份；地址使用 IPv4／IPv6 字面量，端口为 1–65535。客户端选 gateway，网关选 adapter；页面不能生成身份、添加公钥、停用认证或修改信任有效期。

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
| GET `/api/ws` | 同源 WebSocket，只接收服务器推送，不接受客户端业务消息 |

客户端 settings 为 config_path、target_id、target_host、target_port；网关额外包含 listen_host、listen_port；adapter 为 config_path、listen_host、listen_port、base_url、api_key_env。WebSocket 首条 ready 返回随机 owner、state 和脱敏 events；后续为 state、event、output、finished。output 与 finished 仅发给对应 owner，包含 request_id；finished.result 为 completed／cancelled／failed，只有 completed 表示收齐最终响应。

role 错误为 404、运行中修改为 409、来源错误为 403、输入错误为 400、非 JSON 请求为 415、超过 HTTP 限额为 413。错误体仅为 `{error:固定代码}`。
