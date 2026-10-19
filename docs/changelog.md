# 变更记录

## Unreleased

### 破坏性变更

- 契约升级为 2.0：simulator 角色、模块与命令迁移为 adapter，simulate 改为 infer，--simulator-port 改为 --adapter-port。已有身份密钥可复用，角色与信任配置须同步更新，步骤见 [迁移说明](deployment.md#从模拟服务迁移)。
- 生产推理接入真实模型，默认 deepseek-flash、关闭思考模式。max_output_tokens 改为真实模型 token 上限，回答不再是确定性回显。

### 变更

- 网关 WebUI 改为可展开的连接行，指标和脱敏事件默认折叠；新增在线对话、历史对话和已断开连接弹窗。普通／流式正文仅通过受保护详情接口读取，进程内限量保留，关闭 WebUI 清空，日志和审计不记录正文。

- 客户端推理结果改为本地 Markdown 渲染，支持标题、列表、表格、引用和代码块；普通／流式隐藏时间戳与 UUID 元数据，复制只含正文。首行跨分片不闪现，普通 JSON 正文保留；禁用原始 HTML、危险链接和图片自动外联，密码协议与 CLI 输出保持兼容。

- 同步 `.env` 自动加载的入口、查找目录和配置优先级，以及真实 DeepSeek 三进程验证状态；部署验收区分历史 wheel 冒烟与当前 adapter 依赖，保留首次真实请求失败、根因未确认及生产验收待完成的边界。

- 增加独立 AdapterServer、ModelProvider 扩展接口及 DeepSeekProvider，使用锁定 openai==3.31.0 的异步 SDK，支持普通与流式响应、绝对截止、取消清理和固定错误。
- adapter 配置新增 provider、base_url、api_key_env；模型密钥由进程环境读取。demo 启动 adapter 与网关，infer 直接调用 provider；测试与性能工具使用 test/ 中的本地模型替身。
- 同步架构、接口、运行配置、启动命令、安全边界、部署迁移及测试文档。


### 修复

- 修复 Linux CI 中 demo 停机端口检查误报 `Address already in use`：使用 `socket.create_server` 重新监听，允许 POSIX 复用 TIME_WAIT 地址，同时继续拒绝仍被活跃监听器占用的端口。

- 修复 PR [#2](https://github.com/bljfy/gateway/pull/2) 的业务组装无界累计、提前结束流遗留会话、心跳误拒绝和头部/载荷请求 UUID 不一致；补齐整个编码载荷预算、绝对超时、单会话读者串行化和跨记录 UTF-8 解码。

- 修复网关将合法心跳当成协议错误、取消响应发送连带关闭其他请求、正常认证关闭误占安全审计队列的问题；新增兼容的 `SessionClosedError`，补充网关和真实安全会话回归测试。
- Linux 初始化接受 uv 的纯版本输出及附带构建信息的输出，避免正确版本被误判并导致 Ubuntu CI 中断。

### 新增

- adapter 可配置多个 DeepSeek 与 OpenAI-compatible Chat Completions 服务，并按请求轮询普通及流式推理；旧单服务配置继续可用，故障不会自动重试。

- 增加测试结果总览页面，展示 2026-10-18 非 e2e 回归快照的分类通过数、耗时和真实测试样例。

- 客户端增加分步手动协议实验：临时替换 SM2／SM4 密钥、编辑记录字段和 nonce、按字节篡改、同连接及跨会话重放、真实网关记录反射、双向握手篡改和旧 ClientKey 重放；实验隔离身份文件，记录实际观察结果并支持取消清理。

- 网关 WebUI 增加逐连接实时监控和最近结束记录；客户端可模拟 1–32 条正常认证连接、握手停滞、非法协议帧和认证后突然断开，支持批次停止及页面断开清理。

- 整合已有三角色 WebUI，增加 Windows `start.cmd`／`scripts/start.ps1` 和 `webui-all` 一键入口，自动初始化身份、依次启动 adapter 与网关业务服务并打开三个页面；启动失败和正常退出时清理本次创建的进程，单角色 WebUI 支持停止文件。

- 三角色独立中文 WebUI：`webui --role client|gateway|adapter`，回环 HTTP 与同源 WebSocket，手动加载现有身份和保存非敏感端点旁文件；客户端支持检索片段、普通／流式推理、认证测试及取消，网关和 adapter 支持启停、数值指标与脱敏事件。页面断开取消对应推理，服务重启创建新的安全运行对象；角色间国密链路与现有 CLI 保持兼容。新增固定 aiohttp 依赖、本地安全与生命周期回归、局域网三机使用说明。

- 增加题目要求与项目实现说明，逐项关联认证、报文保护、会话和密钥生命周期、测试输出及 TLS 性能对照，区分历史性能证据与当前 adapter 版本待验收项。

- 增加 `.env.example` 和 CLI 的 `.env` 自动加载，已有进程环境变量优先；本地凭据文件加入 Git 忽略规则。

- 增加 `gateway.logging` 运行日志入口、固定事件与元数据白名单、敏感字段脱敏和异常栈过滤；客户端、网关、模拟器及 demo 从共享 TOML 的 `[gateway].log_level` 加载日志级别。新增五个级别的泄漏、阈值、非法配置与 CLI 接线回归测试；JSONL 审计继续独立执行。

- A 线密码后端、原生库构建、协议编解码和安全会话补充中文注释，说明握手身份绑定、密钥材料布局、方向 nonce、记录认证、资源限额及取消与停机清理。

- README 按 A/B/C 分框展示密码与安全会话、网关与审计、客户端与模拟服务的职责；启动流程与请求调用链标注对应文件和函数，并附可点击的源码索引。

- README 增加自动演示与手动三程序启动流程图，以及双安全会话请求调用链图。

- 模拟响应前置 UTC 毫秒时间戳与请求 UUID，客户端、`demo` 和 `simulate` 支持指定请求标识；CLI 默认显示认证、转发、响应完成及停机过程，提供全局 `--quiet` 隐藏 INFO 日志。响应校验先分离首行元数据，正文预算保持原语义；新增 16 KiB 手动验证示例。

- `guomi-gateway` 命令入口和 `demo` 一次演示：自动初始化或复用配置，等待服务就绪，完成安全请求后清理。独立角色采用默认配置路径，初始化参数提供默认值，客户端支持直接输入提示词。

- 三进程性能测量工具、TLS 1.3 双向认证基线与裸 TCP 参考，覆盖握手分段计时、吞吐、普通/流式时延、进程 CPU/RSS、审计开关与停机证据；补充 TLS 身份拒绝回归和生产验收清单。

- 真实三程序 CLI：`init-demo`、`client`、`gateway`、`simulator`；严格角色配置、独立加密私钥和公钥信任、停机清理与审计排空。
- 真实 GmSSL 双链路安全测试和三进程端到端测试，以及集成 JSON/JUnit 报告、运行配置、部署回滚和整合报告。

- 公共接口与数据模型、uv 锁定环境、Windows/Linux 一键初始化、契约测试和 CI/交付产物工作流。
- 三条并行开发线路、最终 merge 验收、开发指南与项目 README。
- A 线真实 GmSSL 后端、规范二进制协议、双向认证与密钥确认、安全记录、会话轮换和异常清理；增加真实后端 TCP 安全测试、JSON 报告与协议说明。
- 固定 GmSSL 3.1.1 源码校验与本地原生构建清单，锁定官方 Python 绑定 2.2.2，并接入 bootstrap 与 CI。原生构建需要 CMake 和 C 编译器。
- 增加 Windows DPAPI／Linux 独立主密钥保护的本地恢复存储、30 分钟恢复凭据、HMAC-SM3 恢复握手与网关独占连接池；增加状态及撤销 CLI/API、重启恢复与异常回归。握手升级至 v2、套件升级至 `SM2-SM4-GCM-SM3-v2`，三端须同步升级，迁移与安全边界见[会话恢复说明](session_resumption.md)。
- 业务载荷的确定性版本化编码 `src/gateway/codec.py`，覆盖 `InferenceRequest`/`InferenceResponse` 与流式请求标志，严格拒绝截断、尾随字节与非法 UTF-8。
- 记录级分片与组装 `src/gateway/framing.py`，客户端与模拟服务端共享逻辑消息的记录边界校验。
- 客户端业务层 `src/gateway/client/`（`InferenceClient`），实现 `complete` 与 `stream`，校验响应身份与分片顺序。
- 确定性模拟推理 `src/gateway/simulator/`（`InferenceSimulator`）及其入站会话循环 `serve`。
- 命令行入口 `src/gateway/cli.py` 的 `simulate` 子命令。
- 业务层单元测试与回环集成测试，并启用 pytest `pythonpath` 以支持 `test/` 包内测试替身导入。
