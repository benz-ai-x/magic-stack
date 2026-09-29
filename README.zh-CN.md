# Magic Stack — macOS AI 网关、SSH 代理与 OpenVPN 客户端

[English](README.md) · [简体中文](README.zh-CN.md)

**Magic Stack 是开源的 macOS 菜单栏应用，集成本地 AI 网关、SSH 代理与 OpenVPN 客户端。** 在同一个应用中，把 Claude Code、Codex、OpenCode、ZCode 接到你自己的模型供应商，访问远程服务和文件，并检查 AI API 流量。界面支持简体中文与英文。

你可以让 AI 助手通过本地 API 完成配置，也可以手动编辑设置。网络功能包括 HTTP/SOCKS5 代理、SSH 端口转发、NFS 远程挂载，以及 SSH/VPN 上传下载速度与累计流量。AI 网关也可独立部署在 Linux 或其他 Docker 宿主机上。

**[下载 macOS 版](https://github.com/benz-ai-x/magic-stack/releases/latest)** · [让 AI 帮你配置](#让-ai-助手帮你配置-magic-stack) · [快速开始](#快速开始接入你的-ai-编程工具) · [OpenVPN](#macos-openvpn-客户端) · [Docker](#docker自托管-ai-网关) · [常见问题](#常见问题)

[![Release](https://img.shields.io/github/v/release/benz-ai-x/magic-stack)](https://github.com/benz-ai-x/magic-stack/releases)
[![CI](https://github.com/benz-ai-x/magic-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/benz-ai-x/magic-stack/actions/workflows/ci.yml)
![平台：macOS Apple Silicon](https://img.shields.io/badge/macOS-Apple%20Silicon-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

![Magic Stack 中文设置界面：SSH 服务器列表、连接参数、端口映射与 NFS 挂载入口](assets/docs/settings-servers-zh-v0140.png)

## Magic Stack 能做什么？

Magic Stack 承载两个可独立使用的产品：**Suanpan（算盘）** 是本地大模型路由网关，**Magic Proxy** 负责 SSH、OpenVPN、端口映射、NFS 和 TLS 抓包。不配置 SSH 服务器也能使用 AI 路由；不接入模型供应商，也能使用网络工具。

| 你的使用场景 | Magic Stack 提供的能力 |
|---|---|
| 用自然语言告诉 AI 你想怎样配置 | 复制本地配置 API 指令，让助手读指南、检查设置、修改配置并验证结果 |
| 在 AI 编程工具中切换不同大模型 | 按模型前缀、显式 `provider/model` 或默认路由选择后端，并单独配置 Claude Code 角色映射 |
| 让多个 Agent 共用一个网关 | 预览并写入 Claude Code、Codex、OpenCode、ZCode 配置，Agent 使用本地网关 token |
| 经自己的服务器访问远程服务 | HTTP → SSH SOCKS5 代理、多服务器并行端口转发、自动重试与唤醒重连 |
| 让 Mac 接入 OpenVPN 网络 | 导入服务端提供的 `.ovpn` 配置，将密码凭证存入钥匙串，在应用内连接或断开 |
| 查看 SSH 或 VPN 网络流量 | 在展开菜单中查看上传下载速度与累计流量，区分 SSH 和 VPN 的统计口径 |
| 在 Mac 上使用远程文件 | 通过 SSH 隧道挂载 NFSv4，辅助安装远程服务，连接恢复后自动重挂 |
| 了解 AI 调用与用量 | 本地请求数、token、缓存、错误统计，以及受支持供应商的余额与配额查询 |
| 查看 AI 请求和响应 | 可选的 mitmproxy TLS 抓包，将识别到的 AI API 调用保存为本地 JSONL |

查看 [Agent 兼容性](#ai-网关agent-与协议兼容性)、[SSH 与 NFS](#ssh-代理端口转发与-nfs-远程挂载)、[网络流量统计](#sshvpn-上传下载速度与累计流量)、[AI 用量与抓包](#ai-用量统计与流量抓包)，或[数据存放方式](#本地端口数据与凭证)。

本 README 描述当前 `main` 分支。使用已打包的应用时，请以对应[发行说明](https://github.com/benz-ai-x/magic-stack/releases)中的功能范围为准。

## 快速开始：接入你的 AI 编程工具

### 安装 macOS 应用

1. 从 [GitHub Releases](https://github.com/benz-ai-x/magic-stack/releases/latest) 下载 Apple Silicon 版 `.dmg`。
2. 将 **Magic Stack.app** 拖入「应用程序」并打开，应用入口位于菜单栏。

接入 AI 路由需要已安装的编程 Agent 和所选供应商的凭证。SSH 代理、端口转发和 NFS 需要 SSH 服务器；OpenVPN 需要 VPN 服务端和客户端配置文件。这些网络条件独立于 AI 网关。

### 让 AI 助手帮你配置 Magic Stack

1. 打开 **偏好设置**，点击 **复制 AI 助手指令**。原生复制会启动配置 API，并让它保持可用直到应用退出。
2. 将指令粘贴给能在这台 Mac 上执行终端命令的 AI 助手，再说明你的目标。指令包含当前操作指南地址、配置 API 地址和认证 token。
3. 让助手先读指南和当前配置，预览编程客户端的变更，并在应用变更后验证结果。重新打开偏好设置，检查已保存的配置。部分服务操作需要使用应用菜单，系统授权需要你在本机完成。

例如，粘贴指令后可以这样提出需求：

> 检查我的 Claude Code 供应商和模型路由。先读取当前配置，说明不一致之处，预览需要修改的客户端配置，应用后再验证一次模型请求。

助手需要访问这台 Mac：云端网页抓取工具无法访问指令中的本机 `127.0.0.1`。复制的 token 用于配置 API，不是模型网关或供应商密钥。[Agent API 文档](docs/agent.md)列出了支持的操作、配置路径和验证步骤。

### 在偏好设置中接入编程工具

1. 打开 **偏好设置 → AI 路由 → 快速接入**，选择供应商，填写该供应商的 API Key。
2. 执行端点探测，查看可用模型，勾选需要配置的已安装 Agent；确认配置预览后应用。
3. 在 **Claude Code 同步**页检查默认模型及角色映射；如果 AI 路由尚未启动，从菜单栏启动，然后重新启动 Agent 使新配置生效。

可用模型和协议取决于供应商、端点及账户套餐，网关支持的转发路径见下方兼容性表。

自动配置会把网关地址和**本地客户端 token** 写入 Agent 配置。供应商 API Key 保存在网关配置中，由网关在向所选供应商发起请求时用于认证。

## AI 网关：Agent 与协议兼容性

**Suanpan（算盘）是 Magic Stack 的本地大模型路由网关**，默认地址为 `http://127.0.0.1:9527`。它接收 Anthropic Messages、OpenAI Chat Completions、OpenAI Responses 三种请求；协议匹配时优先直通，也支持把 Anthropic Messages 请求转换到 OpenAI Chat 后端，包括流式响应和工具调用。

| Agent 或客户端 | 网关端点 | 支持的上游路径 |
|---|---|---|
| Claude Code、ZCode、Anthropic Messages 客户端 | `POST /v1/messages` | Anthropic 兼容端点，或转换到 OpenAI Chat 端点 |
| OpenCode | `POST /v1/messages` 或 `POST /v1/chat/completions` | 根据供应商配置选择匹配的客户端协议 |
| OpenAI Chat 兼容客户端 | `POST /v1/chat/completions` | 供应商配置为 `protocol: openai` |
| Codex、OpenAI Responses 客户端 | `POST /v1/responses` | 通过 `responses_base_url` 配置可用的原生 Responses 端点 |

**目前尚未实现 Responses → Anthropic/Chat 转换**，因此 Codex 需要上游提供原生 Responses 端点。Chat 入站也需要 OpenAI Chat 上游，不会转换为 Anthropic。具体行为见[请求处理实现](suanpan/main.py)与[协议设计](docs/adr/010-protocol-matrix-and-agent-setup.md)。

内置供应商模板包括 **GLM（智谱）、DeepSeek、Kimi、OpenAI、Anthropic、OpenRouter、通义千问 Qwen、硅基流动 SiliconFlow、MiniMax 和火山方舟**，也可配置自定义兼容端点。模板用于选择地址和认证方式，不代表每个账户都能使用该厂商的全部协议或模型。模板定义见[供应商注册表](shared/provider_auth.py)。

### 按角色或模型前缀路由 Claude Code

在 **Claude Code 同步**页，为 Opus、Sonnet、Haiku 和 Subagent 角色选择供应商模型。显式保存角色映射时，会同时更新网关的档位路由规则，让 Agent 设置与网关路由保持一致。

手动配置时，可以参考下面的 YAML **片段**。其中 `primary`、`economy` 应替换为你已在 `providers` 中定义的供应商名，示例模型 ID 应替换为对应供应商实际可用的模型：

```yaml
router:
  default: primary/main-model

rules:
  - match_prefix: claude-opus
    route_to: primary/main-model
  - match_prefix: claude-sonnet
    route_to: primary/main-model
  - match_prefix: claude-haiku
    route_to: economy/small-model
```

规则按顺序检查，命中第一条前缀即停止。请求中显式指定的 `provider/model` 优先，其后依次是 system 中的 `SUBAGENT-MODEL` 标签、前缀规则和默认路由。更多字段见[配置示例](docs/examples/suanpan.example.yaml)，判定逻辑见[路由实现](suanpan/router.py)。

提示词缓存（prompt caching）取决于上游协议和供应商。兼容端点可通过 `anthropic_native` 保留 Anthropic 缓存标记；Anthropic → Chat 转换会移除 `cache_control`，因为 Chat 没有对应字段。详见[提示词缓存设计](docs/adr/004-prompt-caching-and-prefix-stability.md)。

## SSH 代理、端口转发与 NFS 远程挂载

**Magic Proxy 管理你自己的 SSH 服务器连接。** 在 **偏好设置 → 代理 → 服务器** 中添加服务器后，可在同一页配置连接、端口映射和 NFS。

- **HTTP/HTTPS → SSH 代理：** 选择代理服务器并启动连接。应用使用本地 `127.0.0.1:8888` HTTP 代理，SSH 默认在 `127.0.0.1:1080` 提供 SOCKS5 上游。
- **系统代理与应用代理：** 可开启 macOS 系统代理，或使用独立代理参数启动受支持的 Chromium 应用；断开时恢复原有系统代理设置。
- **多服务器并行端口转发：** 把远端可达的开发服务、数据库等映射到本地端口。多个服务器会话可同时运行，每条映射支持单独启停和连接测试。
- **NFSv4 over SSH：** 使用 macOS 内置 NFS 客户端挂载远程目录，支持在兼容的 Linux 服务器上辅助安装和配置 NFS；远端安装及首次本地挂载授权需要管理员权限。
- **连接恢复：** 活跃 SSH 会话自动退避重试，系统唤醒后触发重连；NFS 断线后强制卸载，连接恢复后重新挂载已配置为自动挂载的目录。

Magic Proxy 连接成功后，支持 HTTP 代理的工具可这样使用：

```bash
curl --proxy http://127.0.0.1:8888 https://example.com
```

![Magic Stack SSH 端口映射：本地端口到远端服务的映射、逐条启停开关与连接测试](assets/docs/portforwards-zh-v0140.png)

![Magic Stack NFS 远程挂载：通过 SSH 隧道配置远程目录与本地挂载点](assets/docs/nfs-zh-v0140.png)

## macOS OpenVPN 客户端

**Magic Stack 使用你自己的 VPN 服务端和 `.ovpn` 配置，在 macOS 上管理 OpenVPN 连接。** 支持导入配置、将 VPN 密码存入钥匙串、查看连接状态、应用服务端 DNS 和统计流量。OpenVPN 可执行程序需要单独安装，应用不内置该程序。

1. 如果尚未安装 OpenVPN，执行 `brew install openvpn`。
2. 打开 **偏好设置 → 代理 → 服务器**，添加并保存服务器条目，然后进入它的 **OpenVPN** 标签页。
3. 导入服务端提供的 `.ovpn` 文件。如果需要密码认证，填写用户名，并将密码**存入钥匙串**。点击**保存更改**，保存用户名和 DNS 设置。
4. 点击 **安装到系统**，完成 macOS 管理员授权，再点击 **连接**。修改 profile 后需要重新安装到系统。

SSH 代理接入与 VPN 接入互斥，切换时会停止前一种接入；SSH 端口映射与 NFS 挂载使用独立的服务会话。哪些目标流量经过 VPN，由 profile 和服务端下发的路由决定。Magic Stack 不提供 VPN 服务器或订阅。连接和导入行为见 [VPN 协调器](vpn/coordinator.py)与 [profile 处理](vpn/profile.py)。

## SSH/VPN 上传下载速度与累计流量

SSH 或 OpenVPN 连接成功后，**展开 Magic Stack 菜单**即可查看 `↓` 下载速度、`↑` 上传速度和累计流量。菜单保持展开时持续刷新，顶部菜单栏保留状态图标。

| 接入方式 | 统计范围 | 累计周期 |
|---|---|---|
| SSH 代理 | 经本地 HTTP/HTTPS 代理转发的字节，同时显示活跃代理连接数 | 自本次应用启动以来 |
| OpenVPN | OpenVPN 上报的隧道字节，不显示 HTTP 代理连接数 | 自本次 VPN 连接启动以来，包含自动重连 |

SSH 累计量不包含直连 SOCKS5、独立端口映射和 NFS 挂载。断开、暂停或重连时隐藏流量行；VPN 上报过期后速率归零。这些数值统计网络字节，与 AI 网关的 token 用量和供应商余额分开。具体口径见[菜单显示](shellui/menu_builder.py)与 [VPN 计数](vpn/openvpn_client.py)。

## AI 用量统计与流量抓包

### 查看 token、缓存命中与供应商余额

**运行统计**页从网关的本地用量日志读取数据，展示请求数、输入/输出 token、缓存读取与写入、错误数和总体平均延迟，并按供应商、路由来源、识别到的 Agent 和日期聚合。Agent 来源根据请求的 User-Agent 识别，无法识别的客户端归入未知分组。

**余额速览**页独立查询受支持供应商的 API，目前包含 DeepSeek、GLM、Kimi 的集成。可显示的余额、配额与重置时间取决于账户和供应商。Token 统计用于理解用量，不等同于账单，也不是覆盖所有供应商的逐请求费用计算器。

可查看[英文用量统计截图](assets/docs/usage-stats-en-v0140.png)，了解请求数、缓存命中率、延迟与每日用量的展示方式。

### 通过 TLS 抓包查看 AI API 请求

抓包模式是**实验性、可选**的 mitmproxy 集成，可将识别到的 **OpenAI、Anthropic、DeepSeek、豆包、Qwen、MiniMax** API 请求中的提示词和响应提取到本地 JSONL 文件。

只有经过抓包代理的流量才能被检查；仅启动 AI 网关，不会自动抓取它的出站流量。首次使用需要信任本地根 CA。经过抓包代理的 HTTPS 流量会被解密后转发，其中只有识别到的 AI API 调用会落盘。抓包文件可包含提示词与响应正文，多模态内容以占位符表示。支持的端点范围见[抓包实现](capture/ai_capture_addon.py)。

## Docker：自托管 AI 网关

不使用 macOS 应用时，可以在 Linux 或 Docker 宿主机运行 **Suanpan 网关和 Web 配置界面**。安装带 Compose 的 Docker 后，从仓库构建并启动：

```bash
git clone https://github.com/benz-ai-x/magic-stack.git
cd magic-stack
bash docker/suanpan.sh up
bash docker/suanpan.sh config-ui
```

在 Docker 宿主机打开 `http://127.0.0.1:9528/`，使用 `config-ui` 输出的 token 登录。先配置供应商与默认路由，再接入 Agent。

```bash
# 检查网关是否启动；此检查不验证供应商凭证。
curl http://127.0.0.1:9527/health

# 先预览，再写入 Docker 宿主机上的 Claude Code 配置。
bash docker/suanpan.sh sync --dry-run
bash docker/suanpan.sh sync
```

Compose 默认只向宿主机回环地址映射 `9527`、`9528`，配置持久化在 `docker/data/`。`sync` 命令通过挂载目录更新宿主机的 `~/.claude/settings.json`。Docker 包含网关和 Web 配置；SSH 管理、OpenVPN 连接、NFS 挂载、TLS 抓包及菜单栏需要 macOS 应用。远程访问与部署细节见 [Docker 部署文档](docs/docker-deploy.md)。

## 本地端口、数据与凭证

| 默认本地端口 | 用途 |
|---|---|
| `9527` | Suanpan AI 路由网关 |
| `9528` | Web 设置界面与经过认证的配置 API |
| `8888` | Magic Proxy HTTP/HTTPS 代理 |
| `1080` | SSH SOCKS5 代理 |
| `8080` | 可选的 TLS 抓包代理 |

macOS 侧服务绑定回环地址。`9528` 配置服务在设置窗打开时启动；点击 **复制 AI 助手指令** 后会保持到应用退出，也可通过配置 API 开关设为常驻。Docker 中配置服务常驻，对宿主机的暴露范围由 [Compose 端口映射](docker/compose.yml)控制。

| 数据 | macOS 默认位置或处理方式 |
|---|---|
| 服务器、代理与应用设置 | `~/.magic-proxy.json` |
| 供应商密钥与路由规则 | `~/.suanpan.yaml`；供应商密钥也可引用环境变量 |
| SSH、远端 sudo 和 VPN 密码 | macOS 钥匙串 |
| 导入的 OpenVPN profile | `~/.magic-stack/openvpn/` |
| 网关用量元数据 | `~/.suanpan/logs/usage.jsonl` |
| AI 请求/响应抓包内容 | `~/.magic-proxy-captures/` |

**本地优先意味着网关和配置由你掌控。** AI 请求内容和供应商凭证会发送给你选择的上游服务，无需经过 Magic Stack 托管的中转服务。Agent 使用单独的本地 token，该 token 在上游转发前移除。配置文件以 `0600` 权限原子写入，设置 API 对已保存的供应商密钥做掩码处理。实现见[凭证处理](shared/provider_auth.py)和[配置存储](shared/config_store.py)。

## 常见问题

### Claude Code 可以接入 GLM、DeepSeek、Kimi 或 Qwen 吗？

可以，前提是使用 Suanpan 支持的供应商端点：Anthropic 兼容端点，或通过转换接入的 OpenAI Chat 端点。配置好供应商和模型后，使用 Claude Code 同步功能完成接入。模型质量、工具行为和缓存支持取决于所选上游模型与协议。

### Codex 能使用所有供应商模板吗？

不能。Codex 通过 `/v1/responses` 接入，需要供应商提供可用的原生 Responses 端点，并配置 `responses_base_url`。仅有 Chat Completions 或 Anthropic Messages 接口，不能满足当前的 Codex 转发路径。

### 使用 AI 网关必须有 SSH 服务器吗？

不需要。Suanpan 与 Magic Proxy 独立运行：AI 路由不依赖 SSH，端口映射与 NFS 挂载也不依赖 AI 路由。网关请求不会自动经过 SSH 隧道。

### Magic Stack 可以在本地运行大模型吗？

Magic Stack 负责请求路由，不执行模型推理。如果本地或自托管的 API 服务提供受支持的协议和端点路径，可以把它配置为上游供应商。

### Magic Stack 是 VPN 客户端吗？

是。macOS 应用同时提供 OpenVPN 客户端和 SSH 代理。导入服务端的 `.ovpn` 配置并安装本机 OpenVPN 程序后，即可连接。SSH 代理接入与 VPN 接入不能同时运行，端口映射和 NFS 则独立管理。操作步骤见 [OpenVPN 配置](#macos-openvpn-客户端)。

### AI 助手可以帮我配置 Magic Stack 吗？

可以，前提是助手能在运行 Magic Stack 的主机上执行终端命令。**复制 AI 助手指令**会提供操作指南和经过认证的配置 API 入口。助手可以读取设置、配置供应商和服务器，并调用指南中列出的 API。部分服务操作和管理员授权仍需在本机界面完成。详见[让 AI 帮你配置](#让-ai-助手帮你配置-magic-stack)。

### SSH 流量显示统计的是整台 Mac 的网络流量吗？

不是。它只统计经过 Magic Stack 本地 HTTP/HTTPS 代理的流量，不包含直连 SOCKS5、独立端口映射和 NFS。VPN 使用独立的 OpenVPN 隧道统计，详见[流量统计口径](#sshvpn-上传下载速度与累计流量)。

### Magic Stack 免费吗？是否包含模型额度？

Magic Stack 以 MIT 协议免费开源。模型供应商账户和 SSH 服务器由你提供，对应的 API、订阅和服务器费用另行计算。

### Magic Stack 和 Magic AI Router 是同一个项目吗？

是。Magic AI Router（也写作 Magic-AI-Router）更名为 Magic Stack，以覆盖网络接入、AI 路由和抓包等产品能力。Magic Proxy 与 Suanpan 仍是其中两个产品组件，原有配置路径继续保留。更名说明见[更新日志](CHANGELOG.md)。

### Linux 和 Windows 可以用吗？

桌面应用面向 Apple Silicon Mac。Suanpan 可通过 Docker 自托管；仓库暂未提供原生 Linux 或 Windows 桌面应用。无界面部署方式见 [Docker 文档](docs/docker-deploy.md)。

## 开发与贡献

完整 macOS 开发依赖请使用 **Python 3.12+**，内置 mitmproxy 打包工具链需要该版本。

```bash
git clone https://github.com/benz-ai-x/magic-stack.git
cd magic-stack
python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 app.py
```

运行 Python 与设置界面 JavaScript 检查：

```bash
python3 -m pytest tests/ --cov
node --test tests/js/*.test.mjs
```

贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，应用打包与版本号以 [build.sh](build.sh) 为准，签名和公证流程见 [scripts/notarize.sh](scripts/notarize.sh)。

| 文档 | 内容 |
|---|---|
| [更新日志](CHANGELOG.md) | 版本历史与变更 |
| [配置示例](docs/examples/suanpan.example.yaml) | 网关 YAML 字段与路由示例 |
| [Agent API 文档](docs/agent.md) | 经过认证的应用配置 API |
| [领域术语表](CONTEXT.md) | 产品边界、路由优先级与生命周期 |
| [架构决策](docs/adr/) | 协议兼容、凭证、抓包、NFS 与界面设计 |

欢迎用中文或英文提交问题与贡献：[问题反馈](https://github.com/benz-ai-x/magic-stack/issues) · [讨论区](https://github.com/benz-ai-x/magic-stack/discussions)。

## 许可

[MIT](LICENSE) — Copyright (c) 2026 benz-ai-x。
