# 模块地图

定位职责或变更模块边界时使用。每行标明知识归宿；实现细节以对应源码为准，术语与行为契约见 [CONTEXT.md](../../CONTEXT.md)，导入约束见 [CLAUDE.md](../../CLAUDE.md)。

`tests/test_docs_drift.py` 按域校验下列清单；新增、移动模块时同步条目。目录标记与缩进模块行是守卫的解析格式。

## 职责归属

```text
app.py ── 装配与菜单/桥接翻译；用户意图委托 services/intents，配置服务启停经 lifecycle.sync_config_server
util.py ── 资源路径（dev 按域 / frozen 平铺）与版本戳

shared/ ── 跨域叶子层，零域知识
  netloc.py ── host:port 解析、格式化与 loopback 校验
  server_shape.py ── servers[] 形状访问与 enabled 口径；代理角色的回退解析和严格归属判定
  provider_auth.py ── 供应商注册表、协议端点、认证、余额 API 卡与掩码 keep 语义
  keychain.py ── macOS Keychain 读写，Security 框架可选导入
  stats.py ── 运行统计
  config_store.py ── PATHS 路径注册表与安全原子写入口
  subprocess_monitor.py ── SSH / mitmdump / OpenVPN 共用的子进程生命周期
  defaults.py ── 跨域默认值与 CST 时区口径
  identity.py ── 稳定 id 派生与迁移错误；已落盘 id 是兼容契约
  runtime_state.py ── RuntimeProjection 跨域运行态快照容器，由 app 一处组装
  i18n.py ── 双语 catalog 取词与语言解析；资源查找自包含，不依赖 util

mpconf/ ── Magic Proxy 配置
  config.py ── 配置 I/O、merge/migrate、运行态装饰声明与转发行纯函数；转发导出 shared/server_shape
  validate.py ── MP 数值、转发/NFS 行与全局端口/挂载点冲突校验
  config_state.py ── ConfigStateStore 跨文件事务、Keychain 计划与恢复；save 含准备/提交，update_mp 供菜单读改写
  local_token.py ── 本地客户端 token；UI 只见掩码布尔态

tunnel/ ── SSH 隧道
  proxy.py ── asyncio HTTP→SOCKS5 代理与 SSHMonitor
  async_runtime.py ── daemon 线程、asyncio 循环与代际停止
  http_framer.py ── 明文 HTTP 增量定界；未定界安全关闭
  ssh_session.py ── SSH 会话连接、僵尸重建与健康泵；转发/NFS 共用
  connection_coordinator.py ── 生命周期锁内的连接/重试编排与「未连接绝不拉起」守卫
  retry_scheduler.py ── SSH 无限重试与封顶退避
  reconnect_trigger.py ── NSWorkspace 唤醒事件与去抖
  host_key.py ── SSH known_hosts 管理
  host_key_flow.py ── SSH 主机密钥信任流程
  ssh_launch.py ── SSH argv、探针、远程命令与失败分类；sudo 密码走 stdin

mount/ ── NFSv4 over SSH（ADR-007）；经白名单复用 tunnel 的 SSH 策略
  remote_setup.py ── 远程 NFS 探测、安装与 exports.d 幂等配置
  mount_control.py ── 本地挂载、卸载升级链与 sudoers 引导；mount 表为状态真相
  nfs_session.py ── 仅含 NFS 转发的 SshSession 薄子类
  coordinator.py ── 挂载状态机：断线卸载、恢复重挂；子进程走 worker

vpn/ ── OpenVPN 客户端；修改接入或特权流程前读 docs/openvpn-client-spec.md 与 ADR-011
  coordinator.py ── 接入/安装序列、启动收养、服务层重建与菜单投影；跨域动作经注入，线程纪律归 intents
  profile.py ── .ovpn 解析、校验与净化；剥除脚本/管理指令，保留 inline 块内容
  profile_store.py ── profile 存取与 0600 原子写；路径经 PATHS，解析委托 profile
  mgmt_client.py ── management 行协议：握手、命令/事件分发与参数转义
  openvpn_client.py ── VpnClient 进程/管理状态机、凭证注入、差分流量与结构化错误
  dns_scripts.py ── root DNS up/down 脚本、原值恢复与崩溃 reconcile
  privilege.py ── sudoers 精确 argv 与 root-only 资产安装；二进制探测含 brew sbin，管理密码存 Keychain 供崩溃收养

shellui/ ── 界面；导航与平台约定见 docs/agents/ui.md
  menu_builder.py ── 菜单与状态图标
  webview_window.py ── WKWebView 的 ObjC 薄 adapter
  log_window.py ── 日志窗
  bridge_protocol.py ── 设置窗 JS↔Python 协议，可独立单测
  config_ui.html ── 自包含 Web 配置面板，分纯逻辑、运行态/视图、接线三层

capture/ ── TLS MITM 抓包
  capture.py ── mitmdump 子进程管理
  capture_controller.py ── 抓包启停与信任缓存
  capture_store.py ── 抓包目录、命名、文件管理与保留策略
  resources.py ── 抓包资源解析契约与启动冒烟判据
  ai_capture_addon.py ── mitmproxy addon：AI 请求/响应抽取为 JSONL
  ca_trust.py ── 根 CA 信任检测与引导窗
  mitmdump_entry.py ── frozen mitmdump 构建入口

sysctl/ ── macOS 系统集成
  system_proxy.py ── networksetup 事务与崩溃恢复
  sys_proxy_controller.py ── 系统代理收敛状态机
  sleep_blocker.py ── 防睡眠
  login_item.py ── 登录启动 LaunchAgent
  port_check.py ── 端口占用检测与终止升级链
  instance_owner.py ── 实例所有权锁，以 pid + 启动时间抵抗 PID 复用

services/ ── 跨域服务编排
  config_server.py ── 配置 HTTP API、认证、路由表与 AI 助手指令模板
  suanpan_runtime.py ── 延迟导入的网关线程运行时与健康审计谓词
  sp_config.py ── 延迟导入的 Suanpan 配置读取桥
  claude_code_setup.py ── Agent 自动配置与 CC 角色→网关 tier 规则投影；Codex 用可选 tomlkit 增量编辑
  lifecycle_runtime.py ── 服务启停顺序、配置服务持有者收敛、抓包状态投影与网关健康对账
  gateway_watchdog.py ── macOS / Docker 共用的对账节奏、失配阈值与失败退避策略
  intents.py ── 菜单/桥接共用意图面：guard 分派、慢操作线程、通知、dirty 与接入操作串行化
  authenticated_http.py ── 带凭证出站：拒跨 origin 与 HTTPS 降级，限制响应体
  server_check.py ── 服务卡注册表、探测编排与输入/Keychain 取用，单卡失败不连坐
  balance_usage.py ── 余额 API 请求与响应归一
  provider_probe.py ── 供应商验证、模型清单与端点探测
  usage_stats.py ── 本地用量多维聚合

suanpan/ ── AI 路由网关（三协议入站，ADR-010）；同协议直通优先、失配才转换
  config.py ── Pydantic schema、掩码契约、null 归一与路由文法消费
  validate.py ── SP schema、数值、供应商 URL 与路由引用校验，宿主延迟导入
  main.py ── FastAPI app factory 与路由 handler
  middleware.py ── APIKey 常量时间认证与请求体上限
  proxy.py ── 流式转发、重试与四车道共用的发送/错误/响应纪律
  compat.py ── 请求归一与协议转换；system 抽取委托 router，max_tokens 参数名归 shared/provider_auth
  usage_extractor.py ── SSE 用量提取
  router.py ── 路由决策、system 文本抽取、目标文法与 tier 规则逆查询
  usage_log.py ── JSONL 写入/轮转与 wire usage→UsageEntry 转换
  prewarmer.py ── 启动预热 best-effort adapter
  __main__.py ── python3 -m suanpan 独立启动入口

docker/ ── 容器装配；与 macOS 共用 services，形态差异通过构造参数注入
  entry.py ── 各子命令共用路径重定向与服务装配，主循环驱动共享网关 watchdog
```

## 运行与配置入口

主线程运行 rumps NSRunLoop；代理 asyncio 循环、Suanpan uvicorn 与配置 HTTP 服务运行在后台 daemon 线程。慢操作的意图执行纪律见 `services/intents.py`；接入互斥与服务层自治见 [ADR-011](../adr/011-server-centric-config.md)。

- **配置读取/写入**：路径从 `shared/config_store.PATHS` 取用，测试通过 `patch.dict` 重定向；安全原子写归 `config_store`，跨文件事务与恢复归 `ConfigStateStore`。普通保存调用 `save()`，菜单读改写调用 `update_mp()`；成功回调在事务锁释放后执行。
- **Magic Proxy 模型/迁移**：`mpconf/config.py` 与 [ADR-011](../adr/011-server-centric-config.md)。`~/.magic-proxy.json` 使用服务器中心模型；v1 迁移保稳定 id 与 Keychain 槽位。SSH 密码与 NFS sudo 密码分别存 Keychain 的 `tunnel:` / `nfs-sudo:` 账户。
- **Suanpan 配置**：`suanpan/config.py` 与 [配置示例](../examples/suanpan.example.yaml)，默认文件 `~/.suanpan.yaml`。监听端口与密钥布尔契约见 [ADR-002](../adr/002-config-representation-and-masking.md)。
- **Agent 自动配置**：读 [ADR-010](../adr/010-protocol-matrix-and-agent-setup.md)。Codex 缺少 tomlkit 时给安装提示，其余 Agent 保持可用。
