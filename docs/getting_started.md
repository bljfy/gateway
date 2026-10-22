# 开始使用

项目由三个程序组成：客户端负责提问，网关负责安全转发，Adapter 负责连接模型平台。三个网页分别管理它们。先在同一台机器运行整套服务，完成一次提问后再配置互联网访问。

## 启动网页

### 使用 Docker

安装 Docker Desktop（Windows）或 Docker Engine 与 Compose 插件（Linux），启动 Docker。在解压后的项目文件夹打开终端，执行：

```sh
docker compose up -d --build
```

首次需要下载基础镜像、依赖和 GmSSL 源码，并在镜像内校验、编译原生库；再次启动复用镜像。无需在主机安装 Python、CMake 或 C 编译器。

等启动完成后，在浏览器打开：

| 页面 | 地址 | 接下来做什么 |
| --- | --- | --- |
| Adapter | http://127.0.0.1:18082 | 填写 API Key，然后点击“启动服务” |
| 网关 | http://127.0.0.1:18081 | 确认状态是“运行中”；未运行则启动 |
| 客户端 | http://127.0.0.1:18080 | 点击“测试网关认证”，成功后发送问题 |

首次自动生成配套身份。没有模型凭据时 Adapter 保持停止，页面显示“开始使用”。粘贴模型平台提供的 API Key，保留“保存到服务器”勾选，点击保存后启动 Adapter。使用其他模型平台时，先在连接配置中设置服务地址、凭据变量或服务数组并保存；客户端模型名也需填写该平台支持的名称。

认证成功只说明客户端与网关身份校验成功。客户端收到完整模型回答，才证明整条推理链路可用。页面显示的模型服务监听状态和 Docker 健康状态都不能代替一次实际提问。

Docker 后台运行，关掉浏览器和终端后服务继续运行。默认网页端口只发布到主机回环地址。身份、凭据和会话恢复数据保存在 `gateway-data` 数据卷中，日常停止或重建容器会保留。

### 直接在主机运行

Windows 双击 `start.cmd`。第一次自动准备固定版本的 Python、依赖和 GmSSL；主机需联网并已安装 CMake 和 C 编译器。缺少编译工具时可使用上面的 Docker 路径。启动成功后自动打开三个页面，在 Adapter 中填写凭据并启动服务。

Linux x86_64 预先安装 curl、tar、CMake 和 C 编译器，在项目目录执行：

```sh
bash start.sh
```

脚本自动准备环境、设置原生库搜索路径，并启动三个页面。服务器上没有桌面浏览器时使用 `bash start.sh --no-browser`，再配置下面的 HTTPS 入口。主机启动窗口需要保持打开，按 Ctrl+C 会依次停止三个角色并清理连接。

单独启动角色而身份目录尚未创建时，页面提供“创建三方身份配置”。此按钮只创建新的演示身份目录，已有目录不会覆盖；配置损坏时按页面提示加载或修复已有文件。多机部署必须分发同一套相互信任的身份，步骤见 [WebUI 三机示例](webui.md#局域网三机示例)。

## 互联网访问

准备一台有公网入口的服务器，以及三个解析到该服务器的域名，例如 `chat.example.com`、`gateway.example.com`、`adapter.example.com`。在防火墙和云安全组允许 TCP 80、443 入站。使用独立域名，不使用 `/client` 一类路径前缀。

在项目目录新建或编辑 `.env`，填写实际地址及随机管理员密码：

```dotenv
WEBUI_PASSWORD='替换为至少16字符的随机密码'
WEBUI_CLIENT_ORIGIN=https://chat.example.com
WEBUI_GATEWAY_ORIGIN=https://gateway.example.com
WEBUI_ADAPTER_ORIGIN=https://adapter.example.com
```

地址不带尾部斜杠。如果先前运行过本机容器，先执行 `docker compose down`，再启动公网配置：

```sh
docker compose -f compose.yaml -f compose.internet.yaml up -d --build
```

打开三个 HTTPS 地址。浏览器提示登录时，用户名填写 `admin`，密码填写 `.env` 的 `WEBUI_PASSWORD`。首次配置引导与本机模式相同。

Caddy 使用域名自动申请并续期 HTTPS 证书，并转发 WebSocket；需要域名解析正确且证书验证能到达服务器。相关行为见 [Caddy HTTPS 文档](https://caddyserver.com/docs/automatic-https)和 [WebSocket 代理文档](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。证书与配置保存在单独的数据卷中。内部 WebUI 继续监听回环地址；业务端口和指标端口不发布到公网。

这三个网页都是管理员控制台，可修改服务配置并查看相关业务内容。当前使用共享管理员认证，没有普通用户账号或角色权限。密码变更后重新创建容器；浏览器可能缓存旧认证，重新登录可使用新的私密浏览窗口。

已有 HTTPS 代理或证书时，可以使用 [WebUI 的单角色远程参数](webui.md#安全边界)，直接在主机运行。

## 日常操作

本机容器的常用命令：

```sh
# 查看是否运行；gateway 的 healthy 表示三个控制面能读取身份配置
docker compose ps
# 查看启动错误
docker compose logs --tail=100 gateway web
# 停止，保留数据
docker compose down
# 再次启动
docker compose up -d
```

公网部署时，上述每条命令中的 `docker compose` 都使用 `docker compose -f compose.yaml -f compose.internet.yaml`，保持同一组配置文件。

更新项目之前，先停止并备份：

```sh
docker compose stop
docker compose cp gateway:/data/identities ./gateway-backup
```

同时保留当前源码版本、Compose 文件和 `.env`。备份含私钥、模型凭据和会话恢复秘密，应限制访问。数据卷不能代替备份；不要执行 `docker compose down -v`，它会删除持久化数据。恢复时先停止服务，将完整身份目录恢复到原数据卷的 `/data/identities`，保留 Linux 文件权限和 UID/GID 10001；验证三页配置已加载，再完成一次普通和流式提问。

Docker 镜像使用非 root 服务账户。模型凭据保存为身份目录内的 `adapter.webui.credentials.json`，Linux 文件权限为 0600；API 和页面不返回凭据值。进程中的非空环境凭据优先于保存文件，页面更新立即作用于当前 Adapter 进程；若外部环境仍保存旧值，应同步更新后再重建容器。

## 遇到问题

| 现象 | 处理方式 |
| --- | --- |
| 提示无法连接 Docker | 启动 Docker，再执行启动命令 |
| 镜像构建下载失败 | 检查服务器能否访问镜像仓库、Python 包仓库及 GitHub；保留日志后重试 |
| 提示端口被占用 | 停止旧实例或占用端口的程序，再启动 |
| 网页打不开 | 先查看 `docker compose ps` 和日志；本机使用表格里的完整 127.0.0.1 地址 |
| HTTPS 证书申请失败 | 检查三个域名的 A/AAAA 记录、80/443 防火墙规则以及 web 容器日志 |
| 网页返回来源校验错误 | 使用配置的完整域名；检查 Origin 不含路径或尾部斜杠，代理保留原 Host 和 Origin |
| 提示凭据缺失 | 打开 Adapter 的“配置引导”，选择显示为待填写的变量，保存 API Key 后启动服务 |
| 认证成功但没有回答 | 检查 Adapter 已启动、模型 URL 与模型名正确、平台账户可用；再尝试一次实际提问 |
| 服务启动失败 | 检查业务端口、身份文件、审计目录和数据卷权限；不要删除身份重新生成来掩盖错误 |

## 后续部署改进

目前容器仍在首次使用时从源码构建；本机安装仍需要编译工具。要进一步减少初次部署成本，下一步应发布带校验摘要的 Windows/Linux 完整运行包和预构建容器镜像，并在 CI 中实际验证全新安装、首次引导、保留数据升级和回滚。公网使用还需要补充独立用户账号、权限与登录会话管理。

当前代码已提供这些启动入口，但容器构建、真实域名证书签发、目标服务器升级和恢复仍需在目标环境完成验收。发布和生产验收要求见 [部署与回滚](deployment.md)。
