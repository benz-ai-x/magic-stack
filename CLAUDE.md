# CLAUDE.md

## Agent skills

### Issue tracker

Issue / PR operations and wayfinder maps go through `docs/agents/issue-tracker.md` (GitHub issues, `gh` CLI).

### Triage labels

Triage uses the five-label canonical vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo: `CONTEXT.md` + `docs/adr/` at the root. See `docs/agents/domain.md`.

## 概述

Magic Stack — macOS 菜单栏应用（壳），承载两个独立产品：Magic Proxy 与 Suanpan（领域定义见 `CONTEXT.md`「产品结构」）。

> **模块清单、菜单结构等易过期信息以代码为准**（`tests/test_docs_drift.py` 做防漂移守卫）；版本号见 `build.sh`。

> 用户可见名是 **Magic Stack**（见 `build.sh` / `app.py`）；内部代码和历史文档中常称 Magic Proxy / Magic-AI-Router。

## Tech Stack

Python ≥3.9（自有代码下界；**打包工具链因 mitmproxy ≥12 需构建解释器 ≥3.12**，见 ADR-001）+ rumps（菜单栏 UI）+ asyncio（HTTP→SOCKS5 代理）+ PyObjC objc/AppKit/Foundation（WKWebView 设置窗 / CA 信任引导窗 `ca_trust.py` / 日志窗 `log_window.py`）+ Pillow（图标生成）+ mitmproxy 12.2.3（抓包模式 TLS MITM）+ FastAPI + uvicorn + httpx + pydantic（Suanpan AI 路由网关）+ tomlkit（Codex config.toml 增量编辑，ADR-010 M4——缺席时 Codex 车道提示安装，其余 Agent 不受影响）+ openvpn 子进程（VPN 接入，`vpn/` 域——用户自装 Homebrew openvpn 2.6+，独立进程 mere aggregation 零 GPL 义务，缺席时安装引导，见 `docs/openvpn-client-spec.md`）；PyInstaller 打包 `.app`（`--windowed` + `LSUIElement=true`）；SSH 隧道经系统 `ssh` / `sshpass`（密码认证）；SSH 密码走 macOS Keychain。详见 ADR-000 + ADR-001。

## 命令

```
# 开发模式
python3 app.py

# 安装依赖
pip3 install -r requirements-dev.txt

# 测试（本机须 python3 -m：bare pytest=3.9 在 suanpan 类型注解崩溃）
python3 -m pytest tests/

# 打包 .app
bash build.sh
cp -R "dist/Magic Stack.app" /Applications/

# 发布分发（签名 + 公证）
bash scripts/notarize.sh
```

## 架构

单一归属原则（每域一个归宿模块；逐模块清单是防漂移守卫 `tests/test_docs_drift.py` 钉住的契约面）。分层 DAG 只向下（叶子 shared/ → 域 → services/shellui → app/docker；同层只许同域，唯一白名单见守卫内 `_ALLOWED_SAME_LAYER`）——`tests/test_arch_imports.py` 钉死：

```
app.py ── 编排器：__init__ + _on_tick + 菜单回调（子模块由 app.py 直接持有；
  用户流只做翻译——菜单回调从状态推导、_bridge_action 从 action 字符串
  映射，意图执行纪律归 services/intents；「未连接绝不拉起」守卫与守卫
  重建归 ConnectionCoordinator、:9528 启停单一路径经 lifecycle.sync_config_server）
util.py ── resource_path（frozen 平铺 / dev 按域包子目录查找）+ 版本戳

shared/ ── 跨域叶子层（零域知识，被多域共用的原语；P1 迁入）
  netloc.py ── host:port 解析/格式化/loopback 校验唯一所有者
  provider_auth.py ── 供应商认证纯逻辑 + PROVIDER_REGISTRY 注册表
    （ADR-010 端点矩阵：每厂商 anthropic/openai/responses 端点卡 +
    认证头 + 套餐变体 + 余额 API 卡带响应语法名——归一按卡路由）+
    restore_masked_key（掩码 keep 语义）
  keychain.py ── macOS Keychain 读写（Security 框架可选导入）
  stats.py ── 运行统计
  config_store.py ── PATHS 注册表 + 原子写管线（唯一安全写入口）
  subprocess_monitor.py ── 子进程生命周期基类（状态全集声明；SSH 与
    mitmdump 两类子进程共用）
  defaults.py ── 跨域默认值（抓包默认端口/目录 + 网关默认端口——
    多域共需的配置面单一归宿）
  identity.py ── 稳定 id 跨域契约：IdentityMigrationError（迁移可行动
    错误）+ stable_id 派生（mpconf 隧道 t- 与 suanpan provider p- 共用
    同一 sha1 截断——已落盘 id 的兼容契约）
  runtime_state.py ── RuntimeProjection：跨域运行态快照的叶子层容器
    （capture_active/forwards/mounts；app 一处组装，lifecycle→config
    _server 单 seam 透传——三参穿三层塌缩为一）
  i18n.py ── 跨语言文案单一归宿（ADR-012）：语义键 → 双语 catalog
    （locales/*.json，dev 包内 / frozen 平铺）+ t() 取词 + auto→
    AppleLanguages 解析；**不得 import util**（同层不同域），目录查找
    自包含；语言键不校验（resolve 全兜底）

mpconf/ ── 配置栈
  config.py ── 配置 I/O + merge/migrate（schema v2 服务器中心：servers[]
    + ssh 节 + services.{ssh,nfs}，v1 tunnels[] 自动迁移保稳定 id——
    Keychain 槽位 tunnel:{id} 保值；proxy_server_id 单一真相取代
    current_tunnel 双表示；servers 形状访问器单一归宿 servers/
    server_by_id/proxy_server/server_forwards/server_nfs）+
    decorate_runtime_state /api/state 运行态装饰单一归宿（只写
    RUNTIME_DECORATED_FIELDS 声明键，strip 名单同源派生）+
    forward_row(s)/toggle_forward_row 转发实例读写纯函数（翻转意图共用）
  validate.py ── mp 分域校验器（顶层数值 + 隧道级行[forwards/nfs] + 全局端口/挂载点冲突；prepare 的校验半边）
  config_state.py ── ConfigStateStore 事务边界：load 四态 / prepare
    分域校验 orchestrator / commit（journal+MP+SP+Keychain+回调次序）/
    recover 幂等重放 / update_mp 菜单写径；READONLY_DECORATED_FIELDS
    只读装饰剥除名单（运行态半边派生自 config.RUNTIME_DECORATED_FIELDS）
  local_token.py ── 本地客户端 token（掩码布尔契约，明文不出 UI）

tunnel/ ── SSH 隧道核心
  proxy.py ── asyncio HTTP→SOCKS5 代理（明文逐请求归属）+ SSHMonitor
  async_runtime.py ── daemon 线程 + asyncio 循环 + 代际停止
  http_framer.py ── 明文 HTTP 增量定界（未定界即安全关闭）
  ssh_session.py ── SshSession：SSH 会话 deep module（三件套编排 +
    连接序列 + 僵尸重建 + 每秒健康泵 tick；转发/NFS 会话共用，
    ADR-007 收敛）
  connection_coordinator.py ── 连接/重试编排（持 _lifecycle_lock）+
    「未连接绝不拉起」守卫单一归宿（proxy_connected/forward_connected/
    restart_forward_async guarded——菜单翻转/桥接自动应用共用）
  retry_scheduler.py ── SSH 重试退避调度（无限退避封顶 60s，永不放弃）
  reconnect_trigger.py ── 唤醒事件→立即重连触发器（去抖 + NSWorkspace 源）
  host_key.py ── SSH known_hosts 管理
  host_key_flow.py ── SSH 主机密钥信任流程
  ssh_launch.py ── SSH 调用策略单一归宿：argv 构建 + probe() +
    run_remote()（一次性远程命令，sudo 密码走 stdin 管道）+
    stderr→中文失败分类

mount/ ── NFSv4 over SSH 隧道挂载（ADR-007；跨域白名单边 mount→tunnel
  复用 ssh_launch 的调用策略）
  remote_setup.py ── 远程 NFS 一键安装（发行版探测 + exports.d 幂等
    写入 + 2049 监听验证）
  mount_control.py ── 本地挂载控制（mount 表真相源 + mount_nfs/umount
    升级链 + 一次性 sudoers.d 引导）
  nfs_session.py ── NFS 会话薄子类（SshSession + 仅含 NFS -L 的隧道
    副本投影；生命周期编排归 tunnel/ssh_session）
  coordinator.py ── MountCoordinator 挂载生命周期状态机（tick
    reconcile：断线强制卸载/恢复自动重挂，worker 线程跑子进程）

vpn/ ── OpenVPN 客户端域（spec 与调研：docs/openvpn-client-spec.md；
  M1 域核心；M2 接线已落：设置窗端点/菜单组/意图/互斥屏障——连接时停全部 SSH 会话与 NFS（断开不回切），正式 mode_gate 状态机属后续里程碑）
  profile.py ── .ovpn 解析/校验/净化单一归宿：脚本/管理类指令剥除
    （root 执行面第二道闸）+ auth-user-pass 改查询式 + inline 块整段
    跳过（块内指令形状的行绝不误伤）
  profile_store.py ── profile 落盘存取（profile_set 布尔的写者）：
    PATHS 注册目录（vpn_profiles_dir）+ 0600 原子写（config_store 管线，
    测试 patch.dict 单点重定向）；解析/净化归 profile.py，布尔翻转归
    保存流（M2 接线）
  mgmt_client.py ── management interface 行协议客户端（纯 Python 零
    依赖，PyPI 封装库全弃维）：密码握手 + version 4 宣告（≤3 静默）+
    命令-响应/实时事件分发（>STATE/>LOG/>BYTECOUNT/>PASSWORD/>FATAL）+
    参数转义（openvpn 配置词法：双引号包裹 + 反斜杠/引号转义）
  openvpn_client.py ── VpnClient（SubprocessMonitor 第三类消费者）：
    hold off+release 放行时序、STATE→隧道状态机（EXITING 只算退出中，
    须等进程退出码收尾）、凭证注入回调、bytecount 差分速率（跨重连
    基数折叠保单调）、>LOG/>FATAL→结构化错误码（域内零文案，UI 层
    映射 i18n）
  dns_scripts.py ── root 侧 DNS up/down 脚本模板（macOS 无 dhcp-option
    应用管线——up 收集 foreign_option_N 应用到默认路由服务并快照原值，
    down 恢复）+ 崩溃 reconcile 判据（marker 存在 = down 没跑过）
  privilege.py ── 特权引导：sudoers 全量钉死 argv（零通配——与 NFS 条目
    的本质差异：config 可控即 root 执行）+ root-only 文件区一次性管理
    员授权安装（conf 0600/dns 脚本 0755/mgmt.pw 0400）+ openvpn 二进制
    三级探测（env 覆盖 → brew sbin → PATH；brew 装在 sbin 默认 PATH
    探不到）；固定管理口端口登 shared/defaults.VPN_MANAGEMENT_PORT，
    管理密码稳定存 Keychain（崩溃后收养残留 root openvpn 的锚点）

shellui/ ── 界面
  menu_builder.py ── 菜单栏 UI + 状态图标
  webview_window.py ── WKWebView 窗口（ObjC 薄 adapter）
  log_window.py ── 日志窗
  bridge_protocol.py ── 设置窗 JS↔Python 协议（纯 Python 可单测）
  config_ui.html ── 自包含 Web 配置面板（三层架构）

capture/ ── 抓包
  capture.py ── mitmdump 子进程管理
  capture_controller.py ── 抓包启停 + 信任缓存控制
  capture_store.py ── 抓包目录/文件管理 + 命名知识唯一所有者
    （含 cleanup_expired_captures 保留策略）
  resources.py ── 资源契约三级链 + 冒烟判据单一归宿
  ai_capture_addon.py ── mitmproxy addon：6 家 AI 请求抽取落 JSONL
  ca_trust.py ── 根 CA 信任检测 + 引导窗
  mitmdump_entry.py ── frozen mitmdump 构建入口
  chromium_proxy.py ── Chromium 启动代理配置

sysctl/ ── 系统集成
  system_proxy.py ── networksetup 事务式 + 崩溃恢复
  sys_proxy_controller.py ── 系统代理收敛状态机
  sleep_blocker.py ── 防睡眠
  login_item.py ── 登录启动 LaunchAgent
  port_check.py ── 端口占用检测（SIGTERM→SIGKILL 升级）
  instance_owner.py ── 实例所有权锁：pid+启动时间双匹配抗 PID 复用

services/ ── 服务
  config_server.py ── Web 配置服务 :9528（JSON CRUD + bearer token +
    agent_instructions 指令模板归宿 + 路由表 dispatch——一个端点一行
    声明，do_* 只剩表遍历；index 隧道解析 _saved_tunnel_by_index 单一归宿）
  suanpan_runtime.py ── Suanpan 网关线程化运行时（延迟导入）+ audit()
    健康审计原语（stopped/healthy/mismatch 单一归宿，Docker watchdog 同谓词）
  sp_config.py ── suanpan 配置读取桥（sp_load*/suanpan_listen，lazy import）
  claude_code_setup.py ── Agent 自动配置唯一归宿（ADR-010 M4）：
    Claude Code 角色映射（写 ~/.claude/settings.json，ADR-003 不变；「保存
    并同步」同时把角色表 upsert 成网关 tier 路由规则——规则=持久真相、
    env 是 CC 投影，两面同源于一次保存，drift 结构上消失；推导路径
    roles=None 只对齐不新增；tier 规则查询经 router.first_tier_route——
    前缀语义单一归宿）+ 多 Agent 注册表引擎（codex tomlkit
    增量编辑 config.toml / opencode opencode.json models 块必写 /
    zcode kind=anthropic；JSON 家族共享 owned 槽位 plan/apply 半成品）
  lifecycle_runtime.py ── 服务生命周期编排：start_all/quit 顺序契约 +
    capture_state 单投影 + _on_sp_saved 双形态 + tick 网关健康对账
    （watchdog：running 旗标 vs 端口真相，僵尸态 worker 重建；用户
    停止/崩溃绝不拉起，防抖=连续失配阈值+退避）
  gateway_watchdog.py ── 网关对账策略单一归宿（R5）：节奏/连失配阈值/
    失败退避/忙位参数化——LifecycleRuntime 1s tick 喂拍（worker 提交）
    与 docker 主循环（内联执行）两 adapter 共用；谓词仍是
    suanpan_runtime.audit
  intents.py ── 用户意图单一归宿（R5）：菜单回调与设置窗桥接（含 VPN HTTP 端点）两套
    adapter 共用的意图面——guard 分派/线程纪律（慢操作 daemon 后台）/
    通知文案/dirty 标记独占；依赖全注入纯 Python 可构造，测试直打
    公开意图面（tests/test_intents.py 真值表）
  authenticated_http.py ── 认证出站：跨 origin 拒 / 降级必拒 / 1MB 上限
  server_check.py ── 服务卡一键检测单一归宿（ADR-011 M2）：SERVICE_CARDS
    注册表（ssh/nfs/openvpn 三卡，label + probe 统一签名）+ check_server
    编排（单卡失败不连坐）；probe_inputs（探针输入守卫 + Keychain 取用）
    自 config_server 迁入——test_tunnel/test_forward/NFS 远程操作反向复用
  balance_usage.py ── 余额 API 单一职责（R5 三刀切）：余额响应归一 =
    注册表卡名路由 + 形状嗅探兜底（_BALANCE_PARSERS）+ fetch_balance
  provider_probe.py ── 供应商验证与端点探测（自 balance_usage 拆出）：
    fetch_models / test_provider（协议分叉最小消息）+ 端点三级探测
    （存在性/认证/模型清单，ADR-010）+ /v1/models 候选链单一归宿
  usage_stats.py ── 本地用量多维聚合（自 balance_usage 拆出；CST 范围
    ——时区口径 shared.defaults.CST，含来源 Agent 维度；纯本地零网络）

suanpan/ ── AI 路由网关子包（ADR-010 三协议入站：Anthropic Messages / OpenAI Chat / Responses（Codex）→ 多家 LLM 后端，直通优先失配才转换）
  config.py ── Pydantic schema + 掩码契约 + null 节归一 + 文法消费
  validate.py ── sp 分域校验器（数值 + schema + 供应商 URL + 路由引用；经 prepare lazy import 保持无网关依赖宿主降级）
  main.py ── FastAPI app factory + 路由 handler
  middleware.py ── APIKey（常量时间比较）+ BodyLimit 中间件
  proxy.py ── 流式代理转发 + RetryPolicy + count_tokens aread + 车道共用骨架（_LaneCtx/_send_upstream 幂等探针/_send_lane 发送前置块含 make_502 wire 塑形/_reject_5xx/_lane_out_headers/_stream_response——四车道发送纪律单一归宿）
  compat.py ── 协议适配唯一归宿（ADR-010）：body 归一化（anthropic_native 旗标）+ 转换 A（Anthropic⇄OpenAI Chat 请求/响应/SSE 翻译器）；system 读取经 router.extract_system_text、max_tokens 参数名经 shared.provider_auth（R5 迁出）
  usage_extractor.py ── SSE 用量提取
  router.py ── 路由决策 + extract_system_text system 抽取单一真源（SUBAGENT 判定输入；R5 自 compat 迁入）+ parse_route_target 文法所有者 + first_tier_route tier 规则逆查询（CC 角色种子共用前缀语义）+ fallback_from 可感知
  usage_log.py ── 追加写 JSONL + 轮转 + usage_entry_from_wire 线格式 usage→UsageEntry 单一转换（proxy 四车道消费）
  prewarmer.py ── 启动预热 best-effort adapter
  __main__.py ── `python3 -m suanpan` 独立启动入口
```

**线程模型：** 主线程跑 rumps NSRunLoop（菜单栏）。后台 daemon 线程跑：asyncio 事件循环（代理服务 ProxyRuntime）、Suanpan 网关（uvicorn）、config server（http.server）。

**菜单：** 四段结构（状态总览[SSH/VPN/Router 三行同框] → 接入模式[SSH/VPN 互斥单选，点选即切换] → SSH 模式段[VPN 模式时整段置灰：会话控制/系统代理✓/端口映射/远程挂载/多服务器时的上游子菜单] → AI ▸[路由+抓包合并] ＋ 尾部[偏好/日志/指令/防睡眠✓/登录启动✓]；配置 API 与语言退役进设置窗）以 `shellui/menu_builder.py` 为准（2026-09-27 重设计，原型 docs/prototypes/menu-redesign.html）；**菜单状态语法**（A 类运行物 = 动词标题 + 状态点四值 + 状态词；B 类设置 = 中性名词 + 原生 ✓；组标题仅异常挂 ⚠；状态圆点为手绘位图）见 `CONTEXT.md`「菜单状态语法」；菜单项图标走 SF Symbols（旧系统静默降级）

**偏好设置：** 菜单「偏好设置…」打开 WKWebView 窗口（`http://127.0.0.1:9528/`）。侧边栏分组：代理（服务器（单视图：连接/端口映射/NFS/OpenVPN 横向 tab）/ 网络设置）+ AI 路由（快速接入 / 供应商 / Claude Code 同步 / 运行统计 / 余额速览）+ 系统（系统选项）。同一页面可浏览器直开（输 token 登录）——依赖原生 bridge 的操作在该场景逐项降级（重连/转发启停给 toast 提示；「复制 AI 助手指令」经认证 `GET /api/agent-instructions` 回退）。:9528 生命周期（ADR-009）：默认**不常驻**——三持有者（设置窗开着 / 复制指令会话闩锁 / `config_api_enabled` 常驻开关）任一在场才监听（细节见 `CONTEXT.md`「服务生命周期」）。

## 配置

### Magic Proxy — `~/.magic-proxy.json`

schema v2（服务器中心模型，ADR-011）：`servers[]`（Server→Service→Instance——每台服务器 `ssh` 节持连接参数（auth_type key/password，密码走 Keychain），`services.ssh` 持转发实例（forwards + autostart），`services.nfs` 持 NFS 挂载（enabled / local_port / squash_to_ssh_user / mounts，见 ADR-007；远程 sudo 密码存 Keychain 独立 `nfs-sudo:` 账户槽）；`proxy_server_id` 指定代理服务器。v1 `tunnels[]` 自动迁移保 id。监听地址存 `http_listen_port`（整型端口；旧 `"host:port"` 字符串读时兼容，见 ADR-002）。

### Suanpan AI 路由 — `~/.suanpan.yaml`

参考 `suanpan.example.yaml`。首次启动时自动创建最小默认配置。监听地址存 `listen_port`（整型端口；旧 `"host:port"` 字符串读时兼容，见 ADR-002）。

## 注意事项

**硬约束（违反即出错）**
- PyObjC 方法名不能以单下划线开头（会被当成 ObjC selector）
- Config server（:9528）和 AI 路由网关（:9527）是两个独立端口，不可合并
- 测试口径：`python3 -m pytest --cov`（omit 清单见 `.coveragerc`）+ `node --test tests/js/*.test.mjs`（glob 形式——node ≥26 目录模式报 MODULE_NOT_FOUND；`validate_mirror.mjs` 非 .test 文件，由 pytest 的跨语言报警驱动）；覆盖率数字以运行为准，不在此缓存
- 校验镜像纪律：设置窗 JS `validateConfig` 手抄镜像 Python 分域校验器（双层拦既定约定）——镜像族「同错同净」与单侧族白名单由 `tests/test_validation_mirror.py` 钉住（node 桥缺席自动 skip）；**新增单侧校验规则必须去白名单挂号**
- 文案国际化纪律（ADR-012）：用户可见新文案进 `shared/locales/*.json` 双侧补齐、Python 侧 `i18n.t("字面键")`（**动态键禁止**）、设置窗 JS 侧 `tt("字面键")`——键位奇偶/en 全译/取词/汉字字面量/HTML 渲染层残留五道闸由 `tests/test_i18n.py` 钉住；日志与注释中文直写不进 catalog；未迁移文件在 `_HAN_WHITELIST` 挂号（M4 清零）；设置窗 LAYER 1 的 zh 标签/复合文案是 node 测试钉死的数据面，渲染侧一律键化取词

**形态事实（环境即真源，改代码即改）**
- 菜单栏状态图标用 `MenubarIcon.png` 染色（绿=已连接 / 黄=连接中 / 灰=未连接）
- 打包后的 .app 设置 LSUIElement=true，不显示 Dock 图标
- Suanpan 网关依赖为延迟导入——未安装时 app 正常启动，网关功能不可用并提示安装命令
