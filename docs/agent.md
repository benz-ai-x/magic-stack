# Magic Stack — AI 操作指南

这份指南由当前安装版本的配置服务提供。Magic Stack 包含两个可独立使用的产品：Magic Proxy 网络工具和 Suanpan / 算盘 AI 路由网关。

## 从这里开始

1. 在运行应用的主机终端读取本指南和 `GET /api/state`。使用用户复制指令里的实际地址和 token，默认配置端口为 **9528**。`127.0.0.1` 是命令执行位置；云端网页工具、另一台电脑或另一个容器不能直接访问用户的本机回环地址。
2. 按下方能力表选接口：查询当前状态、检测目标服务，再按用户任务修改配置或发起运行操作。
3. 配置变更遵循“读取完整配置段 → 修改目标字段 → PUT → 检查结果 → 读回验证”。客户端配置使用 preview 查看 diff 后再写入。已有连接切换需说明影响；需要管理员授权的步骤交给本机用户完成。
4. 用运行状态或连通性检查确认完成。HTTP 200 可能仍有 `ok: false`；异步操作的 `ok: true` 仅表示接受请求，保存配置也不等于服务已启动。

配置 API 与模型网关是**两个独立服务**：配置 API 默认 `:9528`；模型网关默认 `:9527`，实际端口见 `sp.listen_port`。配置 API token、本地模型客户端凭证、供应商 API key 各有用途，不能互换。

## 能力与操作入口

| 任务 | AI 可通过 HTTP 完成 | 运行验证与当前边界 |
| --- | --- | --- |
| SSH 接入、系统 HTTP/SOCKS5 代理 | 保存服务器、代理角色与端口；SSH 和服务检测 | SSH 连接/断开/重连及即时系统代理开关走本机菜单或设置窗原生桥接；HTTP 没有这些运行端点 |
| 端口映射 | 保存转发行、启用标志和自启动；一次性探测远程目标 | 会话启停/重连走原生界面；`forward_running` 只表示存在会话，不能当作连通成功 |
| NFS 远程挂载 | 保存挂载项、探测远端、安装服务与配置 exports | 本机挂载/卸载走原生界面；读 `servers[].nfs_states` 的 `status/error/fixable` 验证 |
| OpenVPN | 导入 profile、存凭证、安装本机运行资产、连接/断开 | macOS 可用；管理员对话框需用户操作。轮询 `mp.vpn_state` 确认连接结果 |
| AI 路由 | 配置供应商、协议端点、默认路由和规则；探测与测试 | 已运行网关在保存后重载；网关启停/重启走原生菜单，Docker 走部署命令。网关 `/health` 只证明进程在线，模型请求才验证路由 |
| 编程客户端同步 | 检测客户端、预览配置、写入本机配置；CC 角色同步 | 检查写入结果和客户端实际请求；Docker 写入位置取决于卷挂载，不能假定改到宿主机文件 |
| 用量与余额 | 查询日志聚合、供应商余额/配额 | 本地用量与厂商账单口径不同；保留响应中的失败和缺项 |
| TLS 抓包 | 配置端口、目录、保留天数；清理抓包文件 | 启停、CA 信任和经代理启动浏览器走原生界面；`mp.capture_active` 是运行投影。清理会删除抓包文件 |
| 系统设置 | 保存语言、登录启动、防睡眠、配置 API 常驻选项 | 持久设置不等于当前接入已改变；防睡眠还取决于活跃服务 |

原生桥接消息只在应用设置窗中可用，**不是 HTTP API 路径**。当前没有统一运行状态端点、SSH/网关启停端点或日志下载端点；按表说明需用户完成的界面步骤。Docker 主要提供 AI 路由、配置与客户端同步；macOS 网络、Keychain、挂载、原生授权等能力不能假定在容器中存在，VPN 运行接口缺少宿主注入时返回 503。

## 认证、地址与生命周期

- `/agent.md` 无需 token；所有 `/api/*` 使用 `Authorization: Bearer TOKEN`，不要放在 URL query 中。浏览器登录后可使用 `cfgsess` HttpOnly cookie。
- macOS 按需监听配置端口：偏好设置打开期间、原生“复制 AI 助手指令”后的本次应用会话，或“系统选项 → 配置 API 服务”常驻开关开启时可访问。**退出再启动应用会更换配置 token**，401 时重新复制指令。
- Docker 配置服务常驻。宿主机地址和端口以实际发布映射为准；复制文本中的容器监听地址可能需要替换。其 token 由部署配置提供。
- 本机回环请求可加 `curl --noproxy '*'`，避免代理环境变量把本地请求转发到外部。凭证只用于对应接口。

## 保存契约：先读，再改完整配置段

`GET /api/state` 返回 `{"mp": {...}, "sp": {...}}`。`PUT /api/state` 可只提交 `mp` 或 `sp`；**提交的段是整段替换，不是字段级 PATCH**。先读最新状态，保留该段其它字段、数组项及稳定 id；只改 AI 路由时只提交完整 `sp`。当前没有版本号或条件写入保护，修改前重新读取，避免与设置窗同时保存。

成功返回 `{"ok": true}`；校验/保存失败返回 422 和 `{"ok": false, "errors": [...]}`。发现 `_load_error` 时先诊断加载错误，保留原文件，不以空配置覆盖。常规操作通过 API 保存，由事务负责验证、Keychain 处理和重载；直接编辑磁盘文件不能替代该流程。

凭证与运行装饰规则：

- 供应商及 `sp` 顶层凭证读出为 `api_key: null` + `api_key_set`。保留旧 key 时原样保留 `api_key_set: true`，`api_key` 留空或缺省；新 key 放 `api_key`。保留 provider `id`，重命名时也不变。
- `mp.local_client_token_set` 是本地模型客户端凭证的保留标志，原样保留。该凭证用于客户端同步，与配置 API token 不同。
- SSH 密码由临时字段 `mp.servers[i].password` 提交，`ssh.auth_type` 为 `password`。密码存 Keychain，读取时只有 `has_password`。
- VPN 密码走 `/api/vpn-credentials`；profile 正文走 `/api/vpn-profile`，均不放入通用配置。profile 可能含客户端私钥。
- `capture_active`、`vpn_state`、`is_proxy`、`forward_running`、`nfs_states` 等是只读运行装饰，不是启停命令。

## 网络配置：schema v2

持久文件为 `~/.magic-proxy.json`。现行结构是 `servers[] → ssh / services → forwards / mounts`，代理角色由 `proxy_server_id` 指定。旧 `tunnels/current_tunnel_id/current_tunnel` 仅供迁移兼容，新修改使用下面的结构。

以下是结构示例；实际提交以刚读取的完整 `mp` 为基础：

```json
{
  "schema_version": 2,
  "proxy_server_id": "t-example",
  "servers": [
    {
      "id": "t-example",
      "name": "example-server",
      "ssh": {
        "user": "dev",
        "host": "192.0.2.10",
        "port": 22,
        "auth_type": "key",
        "ssh_key": "~/.ssh/id_ed25519",
        "compression": true
      },
      "services": {
        "ssh": {
          "forwards": [{"local_port": 9000, "remote_host": "127.0.0.1", "remote_port": 8000, "enabled": true}],
          "autostart": false
        },
        "nfs": {
          "enabled": false,
          "local_port": 12049,
          "squash_to_ssh_user": false,
          "mounts": []
        },
        "openvpn": {
          "enabled": false,
          "profile_set": false,
          "auth": "none",
          "username": "",
          "password_set": false,
          "pull_dns": true,
          "autostart": false
        }
      }
    }
  ]
}
```

- 新服务器可省略 `id`，保存后读回系统生成的稳定 id，再以它设置代理角色或操作 VPN。已有 id 随重命名和地址编辑保持不变。
- `services.ssh.forwards[]` 是独立的端口映射；`remote_host` 相对于远程服务器。所有启用的本地端口跨服务器全局唯一，也不能占用代理、抓包、配置、网关或 NFS 使用的端口。
- `services.nfs.mounts[]` 为 `{"name": "data", "remote_path": "/data", "local_dir": "/Volumes/data", "auto_mount": false}`。`local_dir` 留空时使用 `/Volumes/<name>`；远程 exports 必须先配置。
- SSH 代理接入与 OpenVPN **互斥**；端口映射和 NFS 属独立服务层，不随接入切换停止。`PUT` 保存转发行不会自动调用设置窗的原生重连动作；需要重连时从界面操作。

## AI 路由与客户端同步

持久文件为 `~/.suanpan.yaml`，API 使用 JSON `sp` 段。供应商可配置 `protocol: "anthropic"` 或 `"openai"`、`base_url`、`responses_base_url`、认证信息、`models` 和 `enabled`。优先从 `/api/provider-templates` 取得当前内置端点，再以 `/api/probe-provider` 或 `/api/test-provider` 验证。

下面是新建供应商与规则的结构示例；域名、模型和 key 是占位值，实际以用户提供的供应商信息与探测结果替换。向已有 `sp` 添加内容时保留其它字段与供应商：

```json
{
  "listen_port": 9527,
  "providers": {
    "example": {
      "protocol": "openai",
      "base_url": "https://api.example.com/v1",
      "api_key": "replace-with-provider-key",
      "auth_header": "Authorization",
      "enabled": true,
      "models": ["example-model"]
    }
  },
  "router": {"default": "example/example-model"},
  "rules": [{"match_prefix": "claude-sonnet", "route_to": "example/example-model"}]
}
```

`api_key_env` 也可指向应用进程已具备的环境变量；终端中临时 export 的变量未必存在于 Finder 启动的应用里。`request_timeout_s`、`body_limit_mb`、`usage_log` 等网关设置保留读取值，按任务需要修改。

网关入站为 `/v1/messages`、`/v1/chat/completions`、`/v1/responses`。Anthropic 主端点通常不含 `/v1`；OpenAI 主端点包含版本路径。协议失配转换有边界：Chat 入站需要 OpenAI 出站，不能把所有三协议组合都视为可用。

路由顺序：有效的 `model=供应商/模型` 内联覆盖 → system 中的 `SUBAGENT-MODEL` 覆盖 → `rules[].match_prefix` 前缀规则 → `router.default`。目标使用配置里的供应商名称与模型 id；未知或停用的显式覆盖会回落，验证时查看实际响应和用量记录。

典型流程：读 `sp` → 保留既有 provider id/key 标志并新增或修改供应商 → 设置 `router.default` 或 `rules` → PUT 完整 `sp` → 测试供应商 → 检查网关 `/health` → 配置客户端并发起实际请求。`/api/test-provider` 测的是供应商，不代表网关已启动。

先通过 `/api/agents` 查看客户端及实际配置路径。Codex/OpenCode/ZCode 走 `/api/agent-setup-preview` → `/api/setup-agent`，id 分别为 `codex/opencode/zcode`。Claude Code（检测 id 为 `claude-code`）走专门的 `/api/cc-default-roles` → `/api/cc-sync-preview` → `/api/setup-claude-code`，不走通用 setup-agent。写入的是本网关地址及本地客户端凭证，不是供应商真 key；检查结果与备份信息，在客户端重启或重载后验证。

## REST API 索引

以下路径均相对于**配置服务**地址，不是模型网关地址。

| Method | Path | 请求与结果 |
| --- | --- | --- |
| GET | `/api/state` | 读取 `mp/sp` 配置与部分运行投影；凭证掩码 |
| PUT | `/api/state` | body 为受影响的完整 `mp` 和/或 `sp`；见保存契约 |
| GET | `/api/agent-instructions` | 返回 `{"text": "..."}`，内含当前配置 token |
| GET | `/api/provider-templates` | 厂商模板、协议端点与认证信息 |
| POST | `/api/probe-provider` | provider 形态对象，`base_url` 必填，`api_key/auth_header` 可选；返回协议端点探测结果 |
| POST | `/api/fetch-models` | `{"provider": "配置中的名称"}`；拉取已保存供应商模型 |
| POST | `/api/test-provider` | `{"provider": "配置中的名称", "model": "模型id"}`；测试可能发送真实模型请求 |
| GET | `/api/balance` | 各供应商余额与可用配额窗口 |
| GET | `/api/usage?range=today` | range 支持 `today/7d/month/all`，缺省 all；日期按 CST，month 为自然月 |
| GET | `/api/agents` | 客户端检测、同步状态、协议提示与配置路径 |
| POST | `/api/agent-setup-preview` | `{"agent": "codex", "options": {"model": "模型名"}}`；OpenCode 可传 protocol/models |
| POST | `/api/setup-agent` | 同 preview 的 body；支持 codex/opencode/zcode |
| GET | `/api/cc-default-roles` | CC 角色与同步漂移；`?seed=rules` 从路由规则推导 |
| POST | `/api/cc-sync-preview` | `{"roles": {"sonnet": {"model": "供应商/模型", "ctx_1m": true}}}`；省略 roles 则推导 |
| POST | `/api/setup-claude-code` | 同 CC preview；写客户端 env 并更新对应网关 tier 规则 |
| POST | `/api/test-tunnel` | `{"server_id": "稳定id"}`，也支持下述 tunnel/index；检测 SSH |
| POST | `/api/test-forward` | tunnel/index + `forward: {local_port, remote_host, remote_port}`；一次性探测 |
| POST | `/api/server-check` | tunnel/index + 可选 `only: "ssh"/"nfs"/"openvpn"`；逐服务返回 results，检查每张卡的 ok |
| POST | `/api/nfs-check-remote` | tunnel/index；只读检查发行版、安装、监听和导出状态 |
| POST | `/api/nfs-setup-remote` | tunnel/index + `mounts: ["/data"]`、可选 `squash`、`sudo_password`；安装远程服务并配置导出 |
| POST | `/api/capture-clean` | `{}`；删除当前抓包目录内的文件，返回 ok/removed |
| POST | `/api/vpn-profile` | `{"server_id": "稳定id", "content": "ovpn正文"}`；净化并存储 profile，返回 removed/needs_credentials/persisted 等 |
| POST | `/api/vpn-credentials` | `{"server_id": "稳定id", "password": "新密码"}`；密码存 Keychain，用户名在配置段保存 |
| POST | `/api/vpn-install` | `{"server_id": "稳定id"}`；按已保存配置安装本机运行资产，可能弹管理员授权 |
| POST | `/api/vpn-connect` | `{"server_id": "稳定id"}`；SSH 接入活跃时返回 ssh_active，用户已授权切换时加 `force: true`；异步完成 |
| POST | `/api/vpn-disconnect` | `{}`；异步断开当前 VPN |

`tunnel/index` 寻址：`{"tunnel": <完整 servers[i] 对象>}` 使用当前表单值，或 `{"index": 0}` 使用已保存 `mp.servers[0]`。名字 `tunnel` 是兼容 API 参数，里面仍须使用 **v2 服务器结构**。`server_id` 目前仅 test-tunnel 和 VPN 端点支持；其它检测/NFS 端点使用 tunnel/index。操作前重新读取数组以避免下标变化。

VPN 流程：先保存服务器取得 id → 导入 profile（检查 `ok` 和 `persisted`）→ 若需要凭证，调用 credentials 并保存 `services.openvpn.auth/username/password_set` → 保存 `pull_dns` 等选项 → install/connect → 轮询 `mp.vpn_state.status/error`。导入响应的 `removed` 是被净化的指令；含私钥的 profile 不放进诊断输出。

## 故障定位与完成判据

| 现象 | 检查路径 |
| --- | --- |
| 配置 API 连接被拒 | 确认执行主机、实际端口、应用是否运行；打开偏好设置或配置 API 常驻开关 |
| 401 | 配置服务 token 与模型凭证分开；应用重启后重新复制当前 token |
| 403 | 检查 Host/访问地址及是否误经代理；使用复制指令中的回环地址 |
| 422 或响应 ok 为 false | 读取 errors/error，修正请求；HTTP 200 不等于业务成功 |
| 网关不可用或 502 | 网关实际端口的 `/health` → 供应商测试 → 实际客户端请求 |
| SSH 或映射不可达 | `/api/test-tunnel`、`/api/test-forward`；首次主机密钥信任需要原生交互 |
| NFS 挂载失败 | `/api/nfs-check-remote` + `nfs_states`；fixable 为 exports 时可修远端导出，再通过界面重挂 |
| VPN 已接受连接但未连上 | 轮询 `mp.vpn_state` 的 status/error；检查 profile、凭证和本机安装步骤 |
| 端口占用 | 本机 `lsof -nP -iTCP:<端口> -sTCP:LISTEN` 确认归属；应用不会自动杀掉占端口的进程 |

本机日志：`~/Library/Logs/MagicProxy.log`；AI 用量默认 `~/.suanpan/logs/usage.jsonl`，实际以 `sp.usage_log.path` 为准；抓包目录以 `mp.capture_dir` 为准。配置持久文件为 `~/.magic-proxy.json` 和 `~/.suanpan.yaml`。这些路径只在相应运行主机/容器文件系统内有效。分享日志时保留必要错误信息，去除凭证和请求私密内容。

完成时分别报告：已保存的配置、已验证的运行效果、尚需用户在原生界面或部署环境完成的步骤。
