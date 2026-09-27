# Magic-AI-Router — 领域术语表

## 产品结构

### Magic-AI-Router

macOS 菜单栏应用（壳）。承载两个独立产品：Magic Proxy 和 Suanpan。用户通过菜单栏图标与之交互。打包为原生 .app（LSUIElement=true，不显示 Dock 图标）。

### Magic Proxy

代理产品。通过 SSH 动态端口转发提供 HTTP→SOCKS5 代理，附带 TLS 抓包能力。与 Suanpan 完全解耦——不依赖 AI 路由，AI 路由也不依赖它。

### 本地代理（Local Proxy）

Magic Proxy 的核心服务。监听在 `:8888`，接收 HTTP/HTTPS 代理请求，通过 SOCKS5（SSH 隧道）转发到远端。浏览器和 CLI 工具指向这里。

### 系统代理（System Proxy）

macOS 全局代理设置（networksetup）。开启后系统内所有应用自动走本地代理。Magic Proxy 以事务方式管理——连接时设置、断开时恢复用户原有配置。

### 应用代理（App Proxy）

针对单个 Chromium 应用的代理（如 ChatGPT/Claude/Discord）。通过 `--proxy-server` 启动参数实现，不修改系统设置，仅影响该次启动的实例。

### 服务器与服务（Server & Services）

v0.13.0 配置模型（ADR-011）：`servers[]` 为中心的 **Server → Service → Instance** 三层——服务器（`ssh` 节持连接参数）承载 1:n 服务（`services.ssh` 隧道服务 / `services.nfs` 挂载服务 / `services.openvpn` VPN 服务），每服务承载 1:n 实例（转发行/挂载行）。设置窗「服务器」单视图（master-detail + 服务卡）是配置面；菜单保持功能分组（操作面）。服务卡「检测服务」经 `POST /api/server-check` 聚合探测（SERVICE_CARDS 注册表——新增服务类型 = 加一张卡）。代理角色 = `proxy_server_id` 单一真相。三层形状访问与 enabled 口径的单一归宿在 `shared/server_shape.py`（叶子层纯函数，全域同消费；代理角色悬空 id 两语义在此显式命名：`proxy_server` 回退版=merge 写侧同序、`is_proxy_server` 严格版=菜单读侧不猜归属）。

### 隧道（Tunnel）

一条 SSH 连接配置。**v0.9 起多活**：代理隧道 + 任意多条转发会话可并行（此前单活——切换=停旧起新）。

### 代理隧道 / 转发会话（Proxy Tunnel / Forward Session）

多活模型（v0.9，决策落档 [ADR-005](docs/adr/005-multi-active-tunnels.md)）的两种运行角色：

- **代理服务器** = `proxy_server_id`（稳定 id，角色唯一真相，schema v2 单一表示）指定的服务器：其 SSH 隧道服务是唯一携带 `-D socks5_port` 的会话（含自己的转发实例），是 :8888 HTTP 代理的 SOCKS5 上游。主图标/状态行/系统代理/暂停语义全部只反映代理会话。切换代理角色（菜单单选 / 设置窗「设为代理服务器」显式按钮）= 旧代理服务器**降级续跑**（有转发实例转纯转发会话，无则停）+ 新服务器以代理模式重启。
- **转发会话** = 其他隧道的纯 `-L` 会话（无 `-D`）：`tunnel/ssh_session.SshSession` 实例（**SSH 会话 deep module**，ADR-007 收敛落地——三件套组装/连接序列/僵尸重建/每秒健康泵 `tick()` 的单一归宿，转发会话与 NFS 会话共用，原 `_ForwardSession` 与 `NfsSession` 的两份逐行镜像已删）。`services.ssh.autostart` 持久字段控制随应用启动自动恢复；`apply_autostarts` 收敛补启。唤醒事件触发全部活跃会话僵尸重建。运行态投影是 `ForwardState`（NamedTuple，字段即契约），与 NFS 的 `MountState` 同款——消费面（菜单/UI/配置服务装饰）用字段访问。端口全局唯一性（含跨隧道）在 prepare 与 JS 双层拦——多活下两条隧道抢同端口会在 `ExitOnForwardFailure` 下互顶死循环（v0.8 的单活豁免作废）。

### 端口转发（Local Forward）

per-tunnel 的 SSH 本地端口转发（`ssh -L`）：把远程服务器可达的 `remote_host:remote_port` 映射到本机 `127.0.0.1:local_port`。配置存于 `servers[].services.ssh.forwards`（`{local_port, remote_host 缺省 127.0.0.1, remote_port, enabled 缺省 true}`）——**逐条启停**：停用行不进会话 -L 集合、不占本地端口（端口冲突检查退出，与挂载「只在用才占端口」同口径）；菜单「端口映射 ▸」逐条成行点击启停（守卫重建该隧道会话，未运行不拉起；代理隧道的转发行同样可停用，重建=代理会话重启），设置窗转发表内有开关列（保存流生效），argv 由 `ssh_launch.build_tunnel_command` 拼装（`socks5_port=None` 即纯转发模式），绑定地址恒为回环。`ExitOnForwardFailure=yes` 使本地端口被占时该会话独立退避重试。本地端口在 prepare 与 JS 校验双层拦（全局唯一——不撞保留端口、不撞任何其他隧道；镜像契约由「校验镜像与漂移报警」钉住）。「未连接绝不拉起」守卫的单一归宿在 ConnectionCoordinator（`proxy_connected`/`forward_connected` 谓词 + `restart_forward_async(guarded=True)`——菜单翻转、桥接自动应用、保存流同判一处可寻；显式重连 `guarded=False` 保持 Spec-A「会话存在即重建」）。保存后经 bridge `reconnectProxy {if_connected:true, tunnel_id?}` 守卫重连逐隧道定向应用——代理隧道保持「同一身份当前隧道」语义，转发会话按各自连接态守卫（未运行绝不拉起）；行内「测试」走 `probe_forward`（一次性 `ssh -W` 探测**表单当前值**——隧道与转发行都未保存可测，不依赖隧道状态）。

### SSH 调用策略（ssh_launch）

「按我们的策略调用 ssh」的单一归宿（`tunnel/ssh_launch.py`）：argv 构建（host-key 三件套 StrictHostKeyChecking=yes + 应用专用 known_hosts + GlobalKnownHostsFile=/dev/null；sshpass-via-fd 密码注入，密码永不出现在 argv/ps；key 认证 -i 传参）、一次性连通性探针 `probe()`、一次性远程命令执行 `run_remote()`（sudo 密码经 ssh stdin 管道传给远程 `sudo -S -p ''`，sshpass 的 pty 回显在返回前 scrub——密码绝不随 argv/stdout/stderr 泄漏）、stderr→中文短语的有序失败分类表（密钥已变更先于未信任）。调用方各留本职：`SSHMonitor.start` 只持有长驻子进程生命周期（消费 `build_tunnel_command` 的 `SshCommand`），`config_server.test_tunnel` 只持有输入校验与 Keychain 取用（委托 `probe()`），`mount/remote_setup` 只持有安装序列编排（委托 `run_remote`）——探针/远程执行与真实隧道行为恒等，改策略只落一处。

### 远程挂载（NFS over SSH Tunnel）

把远程服务器目录经 SSH 隧道以 **NFSv4** 挂载到本机（ADR-007，`mount/` 域）。仅 NFSv4——单 TCP 端口 2049、无 mount 协议，一条 `-L` 隧道承载该隧道全部挂载；macOS 自带客户端，本地零内核扩展。

- **专用 NFS 会话**（`nfs_session.py`，SshSession 薄子类）：tunnel 副本（spawn 投影）只携带 NFS 一条 -L（用户转发行归用户会话——同端口双进程互顶死）；生命周期编排（三件套/连接/健康泵）继承自 SSH 会话 deep module。运行编排归 `MountCoordinator`（`coordinator.py`，tick reconcile）：会话 connected + 未挂载 → 派发挂载；会话断开 + 已挂载 → **立即强制卸载**（hard 挂载防 Finder 卡死）；恢复自动重挂 auto_mount 项；退出先卸载后断隧道。mount/umount 子进程全走 worker 线程。
- **远程一键安装**（`remote_setup.py`，幂等）：发行版探测 → 装包（apt/dnf/yum）→ 写应用专属 `/etc/exports.d/magic-router.exports`（导出恒绑 127.0.0.1 + `insecure` 必须：sshd 转发源端口非特权）→ `exportfs -ra` → 验证 2049 监听。root 脚本经单次 `sudo sh -c` 执行；sudo 密码解析序：UI 显式输入 > 密码登录复用隧道密码 > Keychain `nfs-sudo:` 账户槽；空 = `sudo -n`。
- **本地挂载**（`mount_control.py`）：`sudo -n mount_nfs -o vers=4,port=<lp>,tcp,hard 127.0.0.1:<path> <dir>`；挂载状态真相源 = `/sbin/mount` 表（不信单次返回码）；卸载升级链 umount → `umount -f` → `diskutil unmount force`。本地提权 = 一次性 osascript 管理员授权写 `/etc/sudoers.d/magic-router-mount`（base64 过引号 + `visudo -cf` 先校验，规则仅限 mount_nfs/umount 两个二进制）。
- **配置**：`servers[].services.nfs`（enabled / local_port 默认 12049 / squash_to_ssh_user / mounts[{name, remote_path, local_dir 空缺省 /Volumes/<名>, auto_mount}]）；本地端口与全局端口面冲突在 prepare 拦（仅 enabled 或有挂载的节点参与——merge 填的纯默认节点不占端口）。
- 挂载启停经 bridge `nfsMountToggle`（运行时意图不落盘）；远程检测/安装走 config_server `/api/nfs-check-remote`、`/api/nfs-setup-remote`；`nfs_states` 是 /api/state 的只读运行态装饰。

### 抓包模式（Capture Mode）

实验性功能。启动 mitmdump 作为 TLS MITM 代理（`:8080`），级联到本地代理（`:8888`）。系统代理在抓包期间指向 mitmdump 而非本地代理。

**拦截范围与记录范围不同**：所有 HTTPS 流量都经过 mitmdump 解密转发，但只有已知 AI API（OpenAI/Anthropic/DeepSeek/豆包/Qwen/MiniMax）的请求/响应被记录到 JSONL 文件。其他流量静默放行，不落盘。

当前支持 6 家 AI 的识别与抽取，未来可扩展更多模型。首次使用需在 macOS 钥匙串信任本地根 CA。

### 用户意图（UserIntents）

菜单栏与设置窗共用的用户意图执行纪律单一归宿（`services/intents.py`，架构评审 R5）：重连分派（转发会话按 id 守卫重建 / 代理隧道整体重连，guarded=保存流「未连接绝不拉起」、显式=Spec-A 会话存在即重建）、转发会话启停、端口映射行启停写径（事务写 + 守卫重建 + 如实文案）、挂载启停、抓包开关动作半边（端口占用对话与 CA 信任引导等 UI 门控留在 adapter）、打开抓包目录、复制 AI 助手指令（ADR-009 闩锁）。线程纪律独占：慢操作（重连子进程 join ~10s、host-key 首连）daemon 线程后台跑，菜单点击即返回；通知与 dirty 经注入回调。菜单回调（从菜单状态推导意图）与 `_bridge_action`（从显式 action 字符串映射意图）是同一 seam 的两个薄 adapter——一个意图一个家，改一处两边生效。

### 菜单状态语法（Menu State Grammar）

状态栏菜单的状态表达单一语法（UX 第二批，2026-09-25；2026-09-27 定稿「行即开关」收编）：**标题永远回答「点按会发生什么」（动词），状态永远由专用通道回答「现在怎么样」**。三类实体三种通道——**A 类运行物**（转发会话、转发行、挂载、AI 路由、抓包：有生命周期与健康态）用手绘状态圆点四值（绿=运行 / 黄=进行中 / 黑=未启动 / 红=异常）+ 行尾状态词（文字冗余，glance 与 VoiceOver 共用）；**接入行**（SSH/VPN 连接方式）名词标题 + 状态点 + 开关语义——圆点即状态（蓝=SSH 已连 / 绿=VPN 已连 / 黄=进行中 / 红=异常 / 无点=空闲），点击即动作（空闲连 / 活跃断 / 对端活跃原生确认切换），连接状态不再三处重复（状态区三行与 ✓ 单选已退役，Router 状态撤进 AI ▸ 的启停行状态点，状态段仅留流量/连接日志/异常详情）；**B 类设置**（系统代理：无生命周期）用 macOS 原生 ✓（`NSMenuItem.state`）+ 中性名词标题，不染运行色。动词词表两套按宾语性质走：**启动/停止**（进程与会话）、**开启/关闭**（开关与设置），域动词保留（挂载/卸载）。组标题 rollup：健康且有活跃项挂计数（`端口映射 · 2`），组内有 error 态挂「⚠ n」（异常优先）——与 _error_counts 同源；扫一眼闭合菜单即可判健康。工程事实：状态圆点为**手绘位图**——SF Symbol 图像的 tint 在 NSMenuItem 上两轮真机实测不生效（template 渲染通道按菜单文字色单色渲染，`setTemplate_(False)` 亦无效）；idle 档黑点保持 template 随文字色明暗自适应；蓝档（info=systemBlueColor）为接入行「SSH 已连接」专用，与菜单栏主图标四色语义（灰=无连接/蓝=SSH/绿=VPN/黄=连接中）同源。

### 设置窗桥接（Settings Window Bridge）

偏好设置窗（WKWebView）内 JS 与原生 Python 之间的消息协议。单一 `bridge` 通道，消息为 `{type, payload}` JSON。协议核心在 `shellui/bridge_protocol.py`（纯 Python，可单测）；`shellui/webview_window.py` 仅为 ObjC 薄 adapter。

约定：Python 只递数据，绝不命名 JS 的 DOM 选择器、绝不手写 JS 源码（`json.dumps` 是唯一转义层）；dirty 等界面状态以 JS 为真相源，Python 侧仅为镜像，关窗拦截（`windowShouldClose_`）读镜像。

### 保存流（Save Flow）

设置界面的两阶段保存状态机（`shellui/config_ui.html` LAYER 1 的 `saveFlow`）：校验 → 保存网关配置（PUT /api/state）→ CC 同步预览（失败关闭）→ 用户确认弹窗 → 写入 Claude Code → baseline 推进 → 分支 toast。一切副作用（fetch/弹窗/toast/baseline 写回）经 deps 注入——真实现与测试桩是同一 seam 的两个 adapter，node 测试直接钉住状态机。调用方（LAYER 2 的 `saveAll`）只做表单 collect 与接线；dirty 真相源在 JS，经 `dirtyProjection` 与保存流同口径。

### 挂载错误可见性链路（2026-09-18 真机案例）

用户加挂载项未先「一键安装」→ 远端无导出 → mount_nfs ENOENT 裸透如天书、且设置窗是不刷新的快照（`/api/state` 仅打开时拉一次）——错误永远不出现，表现为「没反应」。修复为四级链路：`mount_control._classify_mount_failure` 分类出中文指引 + **fixable**（"exports" = 重跑一键安装可修）→ `MountState.fixable` → `/api/state` 的 `nfs_states` 装饰携带 `{status,error,fixable}`（error 不再丢弃）→ 设置窗 nfs 视图 5s **定向轮询**（`mergeRuntimeDecorations` 纯函数只合并装饰字段、绝不碰表单；DOM 按锚点 id 更新不 renderView）+ 错误行内显示 + `fixable==="exports"` 时给「修复导出并重挂」按钮（POST nfs-setup-remote 后自动重挂）。

### 运行态投影（RuntimeProjection）

跨域运行态的一次快照（`shared/runtime_state.py`，叶子层零域知识容器）：`capture_active`（语义单一归宿在抓包域 `CaptureController.actively_running`——enabled 且 mitmdump 就绪）+ `forwards: [ForwardState]` + `mounts: [MountState]`。app 一处组装（三个生产者），LifecycleRuntime → ConfigServer → HTTP handler 单一 seam 透传（架构评审 R3：曾经的 capture_state + tunnel_states_fn + mount_states_fn 三参穿三层）；`/api/state` 装饰（is_proxy/forward_running/nfs_states/capture_active）全部从这一个投影读——装饰构造归 `mpconf.config.decorate_runtime_state` 单一归宿（只写 `RUNTIME_DECORATED_FIELDS` 声明的键；`READONLY_DECORATED_FIELDS` 的运行态半边由它派生，剥除与注入同源；is_proxy 经 `resolve_proxy_tunnel` 与 merge 同一解析序）。下一个运行态字段 = 生产者加一个字段 + 装饰处加一个键，不再加构造参数。

### 校验镜像与漂移报警（Validation Mirror）

设置窗 JS `validateConfig`（第一道闸）手抄镜像 Python 分域校验器（prepare 422 兜底）——双层拦是既定约定，但镜像无单一真相，历史两次实际漂移（NFS 端口漏计、五端口 falsy-0 漏检）。报警器：`tests/test_validation_mirror.py` + `tests/js/validate_mirror.mjs` 同一语料两侧同跑（JS 侧经 extract.mjs 取随包发布的 LAYER 1），镜像族断言「同错同净」，单侧族（py_only：挂载点冲突/挂载行形状/数值范围/base_url origin；js_only：供应商空名）显式白名单——新增单侧规则必须挂号，静默漂移变显式决策。

### 界面国际化（i18n，ADR-012）

用户可见文案的中英双语单一归宿：语义键 → `shared/locales/{zh-CN,en}.json`，取词唯一口 `shared/i18n.t(key, **params)`（叶子层零域知识，**不得 import util**——同层不同域）。语言偏好存 mp 配置顶层 `language`（auto/zh-CN/en，缺省 zh-CN，值不校验 resolve 全兜底）；`auto` 经 plistlib 读 AppleLanguages。切换即时生效：`MenuState.language`（resolved 值）进 `struct_key()` 走既有整树重建——菜单刷新按稳定 ref_key 不按标题（PR #100 前的标题字典键坑对语言切换免疫）；写径三处（启动 / 菜单 update_mp / UI 保存回调）汇聚 `app._apply_language()`。纪律四道闸在 `tests/test_i18n.py`：键位奇偶 + 占位符一致、en 全译（值无汉字）、取词守卫（**调用点必须字面键，动态键禁止**——状态词表存键名）、汉字字面量守卫（产品 .py 字符串 AST 扫描，docstring/logging 子树豁免；未迁移文件 `_HAN_WHITELIST` 挂号，M4 清零）。日志与代码注释中文直写不进 catalog（D6）。**M2（2026-09-25 落地）**：设置窗整页 `tt()` 键化（~480 键，含校验消息——LAYER 1 消息也走键，`tests/js/extract.mjs` 的 loadLayer 求值前注入从真实 catalog 构建的 tt，node 中文断言零改动）；config_server serve 时注入 `window.__I18N__`（登录页同按语言渲染）；系统选项页语言选择器 = PUT baseline+language 后**原地重渲染**（不 reload，未保存表单不丢），菜单栏经 `_on_mp_saved → _apply_language` 同拍切换；五道闸新增 HTML 渲染层残留检查。

### 服务生命周期（LifecycleRuntime）

后台服务的单一编排点（`services/lifecycle_runtime.py`）：构造五条服务线（Suanpan 网关 / 抓包 / 系统代理 / 防睡眠 / 配置服务）并持有启停顺序契约——`start_all()`（实例锁单胜守卫 → 端口占用报告 → 配置服务 → 网关自启）与 `quit(ssh_stop)`（系统代理恢复 → SSH 停止 → 服务线 → 配置服务，SSH 停止以回调注入）。「抓包正在运行」在此持有单一投影，对 SystemProxyController（元组）与 ConfigServer（布尔）内部适配；Suanpan 保存后的 reload 链内化于模块内；tick 网关健康对账（watchdog：running 旗标 vs 端口真相，僵尸态 worker 重建，用户停止/崩溃绝不拉起）——对账**策略**（节奏/连失配阈值/失败退避/忙位）单一归宿在 `services/gateway_watchdog.GatewayWatchdog`，Docker 形态喂同一策略（R5：此前容器侧手抄丢了阈值，合法 reload 的端口空窗会误判僵尸态触发 stop/start 竞态）。app.py 经属性面（`suanpan` / `capture_ctrl` / `sys_proxy` / `capture` / `config_server`）引用子模块。:9528 持有者（设置窗/复制指令闩锁/常驻开关）经 app 的 `_set_config_holders` 唯一写口变更即收敛；**Docker 形态恒常驻**（不受持有者状态机影响）；config_server 的 API 面是路由表 dispatch（一个端点一行声明，index 隧道解析 `_saved_tunnel_by_index` 单一归宿）。

### 认证出站（AuthenticatedHttpClient）

带凭证请求的统一出站 adapter（`services/authenticated_http.py`）：模型列表、连通测试、余额查询三类调用只传请求意图；redirect/scheme/超时/响应上限策略集中一处——跨 origin（scheme/host/effective port 任一变化）重定向一律拒绝、HTTPS→HTTP 降级必拒、同 origin 重定向放行且凭证保留（最大跳数 3，允许 301/302/303/307/308）、响应体 1MB 上限。Authorization、x-api-key 及未来自定义敏感头共用同一策略：凭证绝不出原始 origin。

### 重试策略（RetryPolicy）

网关出站请求的自动重试判定（`suanpan/proxy.py`）：无法证明请求未送达时，非幂等请求绝不重放。可重试的充分条件——①pre-send-proven：连接建立阶段失败（ConnectError/ConnectTimeout/ProxyError/UnsupportedProtocol），一字节未出本机；②idempotent-transport：幂等方法（GET/HEAD/PUT/DELETE/OPTIONS 或显式幂等键）遇传输层错误，有界一次并记 attempt/reason。读/写超时即使幂等也不重试（慢上游不靠加倍修复）。`_send_with_retry` 的 req 必须可重发（bytes body，非已消费 stream）。

### 逐请求归属（Per-request Origin Binding）

明文 HTTP 代理的消息定界契约（`tunnel/http_framer.py` + `tunnel/proxy.py::handle_http`）：同一客户端 keep-alive 连接上的每条请求独立解析并验证 authority；upstream 连接只允许同 origin 复用，跨 origin 先关旧连再安全重连。body 定界支持 Content-Length 与 chunked，pipelined 字节由 StreamReader 自然缓冲留给下一轮；无法定界的消息按「本消息后关闭」安全拒绝，绝不带着未定界状态复用连接。CONNECT 隧道走独立直通路径，不经此状态机。

### 实例所有权（InstanceOwnership）

进程所有权的可验证记录（`sysctl/instance_owner.py`）：锁记录含 pid / 进程启动时间 / exe / nonce，经 O_EXCL 原子创建；启动时间入锁抵抗 PID 复用。端口占用只是发现线索、启动期一律仅告警永不发信号——活旧实例由单胜守卫拦截（app 弹窗退出），死旧实例只剩陈旧锁（acquire 接管清理）；并发启动单实例守卫由 `LifecycleRuntime.start_all` 的锁获取承担，失败方不触碰成功方的锁。basename 命令行启发式已删除。

### 资源契约（CaptureResources）

抓包模式的资源单一入口（`capture/resources.py`）：`resolve_capture_resources(cfg)` 解析并验证 mitmdump 二进制（env 覆盖 → frozen bundled → PATH 三级链）、addon 脚本（存在 + 可读）与抓包目录（可建），失败抛带可行动中文文案的 `CaptureResourcesError`。控制器只消费已验证的 `CaptureResources` 三元组，不自行拼接文件名；frozen 态资源为扁平布局（`--add-data` dest="."），addon 导入需包限定/扁平双态兼容。启动冒烟判据（宽限秒数 + 加载错误标记）以 `smoke_capture_boot`/`SMOKE_*` 为单一归宿，dev SIT 与 build.sh 打包冒烟共用。

### 配置事务（ConfigStateStore）

配置持久化的唯一事务边界（`mpconf/config_state.py`）：`load()` 区分 missing/valid/invalid/io_error（损坏不再折叠成空）；`prepare()` 是分域校验的 **orchestrator**（架构评审候选 2：mp 顶层数值/隧道级行/全局端口与挂载点冲突在 `mpconf.validate`，sp schema/路由引用在 `suanpan.validate`——校验器与被校验知识同域演进，文案与顺序被测试钉死）并派生 Keychain 变更计划（密码剥离出候选）；`commit()` 按序执行 journal（载荷内嵌）→ MP → SP → Keychain → 清 journal → 回调（`on_sp_saved` 只在完整提交后）；`recover()` 在启动时幂等重放 journal 补齐跨文件崩溃；`update_mp(mutate)` 是菜单开关的唯一写径——写前读新（磁盘真相）→ 单字段变更 → 同一事务管线，内存副本永不整文件覆写磁盘。invalid 主文件不覆盖最后已知良好的 `.bak`；首创建与保存同一 0600/0700 路径。

### 配置存储（ConfigStore）

两个配置文件（`~/.magic-proxy.json` 与 `~/.suanpan.yaml`）路径的唯一权威注册表 + 共享安全写管线，位于 `shared/config_store.py`（原 mpconf——四域共用后迁入叶子层；sp_* 编排桥在 `services/sp_config.py`）。所有读取方在调用时从 `PATHS` 注册表取路径——测试只需 `patch.dict(config_store.PATHS)` 单点重定向，任何测试都无法再写真实配置文件。

统一语义：原子写（mkstemp + chmod 0600 + os.replace）；Suanpan 侧另保留覆盖前 `.bak` 备份。配置内容的语义（mp 的 merge/migrate、sp 的 pydantic 校验与密钥掩码）不在此——留在 `mpconf/config.py` 与 `suanpan/config.py`。

### 部署形态（Deployment Form）

macOS 菜单栏壳与 Docker 容器两种形态共享同一批 `services/` 模块；形态差异**全部是构造参数**，永不复制实现：`ConfigServer(bind_host, token)`（默认 127.0.0.1 + 自造随机 token；容器传 0.0.0.0 + 配置卷 local_client_token）、`SuanpanRuntime(bind_host)`（缺省取配置监听地址 + 强制回环守卫；容器传 0.0.0.0，守卫不适用——信任边界=宿主机端口映射）。`docker/entry.py` 只做装配（`redirect_paths` → `make_config_server` / `SuanpanRuntime`，网关对账喂 `services/gateway_watchdog` 共享策略——两形态同参，主循环内联执行自愈），掏私有符号或抄写函数体都视为回归。`shared/keychain` 的 Security 为可选导入（缺失即 None + 全吞异常兜底），Linux import 链无需 stub。

### Suanpan（算盘）

AI 路由产品。三协议入站（ADR-010：Anthropic Messages / OpenAI Chat / Responses[Codex]），按模型规则路由转发到多家 LLM 后端——同协议直通优先、失配才经 compat 转换器。独立运行在自己的端口上，不经过 SSH 隧道。出站四车道（anthropic 主路径 / 转换 A / chat 直通 / responses 直通）共享发送纪律单一归宿（`proxy.py` 的 `_LaneCtx`/`_send_upstream` 幂等探针/`_send_lane` 发送前置块（含 `make_502` 的 wire 塑形——错误体按入站协议取 anthropic 平铺或 openai error.message 形状）/`_reject_5xx`/`_lane_out_headers`/`_stream_response`），各车道只剩请求整备/URL/错误体形状/响应塑形的真差异。

### 供应商（Provider）

Suanpan 的 LLM 后端（如 DeepSeek、GLM、Kimi）。每个供应商有 Base URL、API Key（或环境变量）、认证头和启用状态；协议按端点卡走（openai 协议供应商经转换 A 服务 Anthropic 入站）。内置厂商知识单一归宿是 `shared/provider_auth.PROVIDER_REGISTRY`——每厂商一张卡：端点矩阵（anthropic/openai/responses）+ 认证头 + 余额 API（含响应语法名 parser，归一按卡路由）+ 模型用量端点，新增厂商 = 加一张卡（UI 模板/余额/探测共消费）。

### 路由场景（Routing Scenario）

Suanpan 按请求特征划分的路由类别。当前实现两级：
- **模型规则**（rule）——按模型名前缀匹配转发
- **默认路由**（default）——未命中规则时的兜底

> 历史场景（background/long_context/think 等）已在重构中移除，见文末「已移除特性」。

> 注意：代码 `RouteDecision.scenario` 字段含义更广——它记录"本次请求由哪条链路命中"（取值含 `inline` / `subagent` / `rule` / `default`），实为**路由来源**而非领域意义上的"路由场景"。该字段已落盘进 `usage.jsonl`，不宜改名。

### 模型规则（Model Rule）

按请求中的模型名前缀匹配并转发到指定目标（如 `claude-sonnet → deepseek/deepseek-v4-flash`）。规则按顺序检查，命中第一条后停止。

### 内联覆盖（Inline Override）

客户端通过在请求的 model 字段中包含 `provider/model`（如 `deepseek/deepseek-chat`）直接指定后端，绕过所有规则。设计目的：让 Claude Code 在不同上下文中精确选择模型（子代理用便宜模型、主任务用强模型）。

### SUBAGENT-MODEL 标签

客户端通过在 system prompt 中嵌入 `<SUBAGENT-MODEL>provider/model</SUBAGENT-MODEL>` 标签指定后端，效果同内联覆盖。优先级仅次于内联覆盖。

### 路由优先级

请求按以下顺序判定，命中第一条即停止（高 → 低）：

**内联覆盖** → **SUBAGENT-MODEL 标签** → **模型规则** → **默认路由**

前两者（转义路径）允许客户端绕过用户配置的路由；后两者是配置路由，按既定链路判定。

> 显式意图（内联覆盖/SUBAGENT 标签）指向未知或停用 provider 时不硬失败——
> fall-through 到规则/默认路由，但决策携带 `fallback_from`（原意图），
> 响应头 `x-suanpan-fallback` 与 warning 日志宣告之（#50：可感知的
> 容错，绝不静默误投）。

## 已移除特性（历史注记）

以下场景路由已在重构中移除（见 commit `755596e`、`9b3183b`），
`RouterConfig` 仅保留 `default` 字段：background / long_context /
think / 自定义路由 / 后台任务 / 推理请求。重提它们 = 重新打开已决
事项（见 ADR 与 CLAUDE.md）。
