# Changelog

All notable changes to Magic-AI-Router are documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/), adheres to [SemVer](https://semver.org/).

## [Unreleased]

### Removed
- **删除「经代理启动」功能**（菜单/回调/`capture/chromium_proxy.py` 整模块/双语键/测试）：SSH 模式经 :8888 注入的代理环境变量无法从已启动进程回收，SSH→VPN 切换后（接入层互斥，8888 关闭）旧终端与经其拉起的守护进程全部撞死无人监听的端口（真机案例：codex app-server 守护携带 `https_proxy=:8888` 持续 `Connection refused`）。系统代理开关（networksetup）不受影响——它由应用事务式管理，无环境变量残留。

## [v0.14.0] — 2026-09-25 — 产品国际化（中英双语）+ NFS 挂载回归修复

**产品国际化落地（ADR-012，M0–M2）**——菜单栏与设置窗全量中英双语，一处切换即时生效。自研语义键 catalog（`shared/locales/{zh-CN,en}.json` 单一真相，非 gettext）+ `shared/i18n` 叶子模块；语言偏好 auto/zh-CN/en（auto 经 AppleLanguages 跟随系统），存 mp 配置顶层 `language`，缺省中文。

### Added
- **M0 地基**：双语 catalog + i18n 模块（`t()`/resolve/catalog，语言键不校验全兜底）+ 五道守卫（键位奇偶+占位符一致 / en 全译无汉字 / 取词守卫——动态键禁止、状态表存键名 / 汉字字面量 AST 扫描（未迁移文件白名单挂号，M4 清零）/ HTML 渲染层残留零容忍）
- **M1 菜单栏双语（#114）**：六组菜单、弹窗、关于对话框全量取词；「选项 ▸ 语言」子菜单（自动/中文/English，✓ 跟随磁盘偏好）；切换即时生效——`MenuState.language` 进 `struct_key()` 走既有整树重建（PR #100 前的标题字典键坑免疫）
- **M2 设置窗双语（#116）**：整页 `tt()` 键化 ~490 键（LAYER 1 校验消息同样走键——node 测试经真实 catalog 构建的 tt 求值，中文断言零改动）；config_server serve 时注入 `window.__I18N__`（浏览器直开同路径）；系统选项页语言选择器（PUT baseline+language 后**原地重渲染不 reload**，未保存表单不丢）；登录页按语言渲染

### Fixed
- **NFS 挂载 v2 schema 回归（#115，v0.13.0 真机事故）**：`_injected_tunnel` 仍写 v1 顶层 `forwards` 落空 → 连接会话与 NFS 会话双 spawn 抢绑同口 → 重试死循环、NFS 本地端口无监听。改为 v2 `services.ssh.forwards` 投影 + 命令级回归测试（原测试全 v1 形状掩盖回归）
- **设置窗侧边栏溢出**：侧边栏内容 880px > 默认视口 852px——默认尺寸即溢出，「复制 AI 助手指令」被窗口下缘裁掉；导航区改内部滚动（矮窗口优雅降级）+ 侧边栏垂直压缩（导航项/组距/提示卡 133→97px），默认窗口零滚动
- **全页面双语审查五项（#116，真实渲染 24 截图 + DOM 实测驱动）**：快速接入页 `tt()` 字面量渲染；页头副标题英文态全中文；pending 脏状态条按钮残留中文；登录页四文案假拼接（`ui.login.*` 键缺失，一并补齐）；服务器页导语与全宽面板错位 + 侧边栏副标题折行（nowrap+省略号兜底）
- **R5 遗留清偿（#113）**：capture 状态漂移跨语言报警 + 状态文案换轴、余额页三刀切（来源 Agent 维度/端点探测态/时区边界）、网关缝卫生

### Docs
- README SEO 深度重写（痛点开场+关键词前置，172→134 行）；v0.13.0 服务器视图真机截图（端口映射/NFS tab，隐私脱敏）；仓库 URL 随更名同步 magic-stack

## [v0.13.0] — 2026-09-25 — 更名 Magic Stack + 服务器中心配置模型

**产品更名 Magic AI Router → Magic Stack**——「Router」只命名了三层闭环（接入/路由/审计）里的一层，Stack 如实命名整套本地 AI 网络栈。用户可见面全量替换（应用名/bundle/菜单/通知/设置窗/文案/文档）；兼容契约一律不动：`~/.magic-proxy.json` 配置路径、Keychain 服务名与密码槽、日志文件名、内部代码标识（magic-proxy/算盘 Suanpan 子品牌）——老用户配置与密钥零迁移。

大版本换轴（ADR-011）：配置从「连接中心的 tunnels[]」重构为「服务器中心的 servers[]」——一台远程服务器一页配完（连接 + 全部服务），服务卡一键检测这台机器上有什么可以用。四批落地：

### Added
- **服务器中心 schema v2**：`servers[]`（`ssh` 节持连接参数 / `services.ssh` 持转发实例+autostart / `services.nfs` 持挂载实例）+ `proxy_server_id` 单一真相 + `schema_version` 显式版本化；**v1 `tunnels[]` 自动迁移保稳定 id**——Keychain 密码槽（`tunnel:{id}`）与运行时会话身份零损失，服务器名/密钥/挂载原样保留（#106）
- **设置窗「服务器」单视图**（#107，按定稿原型）：master-detail——服务器列表（代理徽标/转发 n/m/NFS ×n 标签）→ 服务器页 = 连接卡 + 服务卡区（SSH 隧道服务卡〔转发实例表+启停+autostart〕· NFS 服务卡〔挂载实例表全功能含 5s 运行态轮询〕· **OpenVPN 占位卡**〔即将支持〕）+ 可见滑动条；侧边栏「代理」组从三页收敛为 服务器+网络设置；未保存的新服务器不能被设为代理（id 是角色寻址前提）；真机验收反馈：服务卡横向 tab 展开（连接/端口映射/NFS/OpenVPN，tab 标签带实时计数）——四 pane 全量常驻渲染 + 纯 DOM 显隐切换，collect/NFS 轮询/探针零改动，切 tab 不重渲染不冲未保存表单
- **服务卡一键检测框架**（#108，M2）：`services/server_check.SERVICE_CARDS` 服务注册表（新增服务类型 = 加一张卡，对齐 PROVIDER_REGISTRY 模式）+ `POST /api/server-check` 聚合端点——SSH 复用 probe（可达性+延迟）/ NFS 复用 check_remote（发行版/2049 监听/导出表）/ **OpenVPN 新探针**（`command -v openvpn`，占位卡「检测服务」已激活）；探针输入归一单一归宿（probe_inputs 迁入 server_check，keychain 参数化）

### Changed
- **图标体系：Lucide 线性单笔触语言内联**（v0.462.0 几何，ISC）——`ICONS` 注册表 + `icon()` 单一归宿（24×24 / currentColor / stroke 1.6 / round cap-join，颜色随宿主自动明暗），侧边栏 / 服务 tab / 能力徽标 / 区块头 / 空态五档布点（尺寸阶梯 16/14/12/28），密码可见性与复制等功能图标统一过同一语言——真机验收反馈「高级感」
- 消费面九类随 schema 换轴（validate→`server_rows_errors`、校验文案 隧道→服务器、两协调器 `current_server`/`proxy_server_id`、菜单角色单选按 id、设置窗 JS 访问器 sshOf/forwardsOf/nfsOf 单一归宿）；`/api/test-tunnel` 等 agent 契约端点语义不变；docs/agent.md API 表补 `/api/server-check`

## [v0.12.0] — 2026-09-24 — 端口转发逐条启停 + 网关自愈 + 架构评审落地

### Added
- **端口映射逐条启停（像远程挂载一样 per-item）**：forwards 行新增 `enabled`（缺省 true，旧配置零迁移）——停用行不进会话 `-L` 集合、不占本地端口（端口冲突检查退出，与挂载「只在用才占端口」同口径；行级形状校验保持全量）。菜单「端口映射 ▸」重排：端口摘要从隧道行标题移出（治一行塞 N 组端口的拥挤），每条转发独立成行 `8030 → 3080 · 已映射/已停用/待会话`，**点击即启停**（圆点随会话状态着色，停用灰点）；代理隧道的转发行同样可停用（守卫重建代理会话，仅连接/连接中时）。设置窗转发表加启用开关列（保存流生效——`enabled` 纳入 changedForwardTunnels 签名，翻转保存即触发守卫重连）。启停在架构上与"编辑转发行保存"同构：`-L` 集合只在会话启动时生效，全部复用既有守卫重连机器，未运行的会话绝不拉起
- **网关僵尸态自动检出与重建（watchdog）**：`SuanpanRuntime.audit()` 健康审计原语（running 旗标 vs 端口真相的 TCP 探测，stopped/healthy/mismatch 单一归宿）+ `LifecycleRuntime.tick` 对账（挂载协调器同款纪律：主线程轻检查、自愈动作丢 worker；5s 审计节奏 × 连续 3 次失配 ≈15s 检出，合法 reload 空窗不误触；失败退避 30s）。谓词只认"running 但端口无人听"——用户显式停止与崩溃态绝不拉起。Docker 形态共享同一对账策略（见下 Fixed 的容器防误触修复），补上 compose 无 healthcheck 的缺口。与 v0.11.0 的停机有界修复构成纵深防御

### Changed
- **网关车道共用骨架（架构评审 R2-1）**：四个 forward_* 共享的发送纪律上收单一归宿——`_LaneCtx` 记账上下文（prologue ×4）、`_send_upstream`（build + RetryPolicy + 幂等探针，曾逐字 ×4）、`_reject_5xx`（5xx 拒绝块 ×4）、`_lane_out_headers`（过滤+provider 宣告+fallback 三件套 ×4）、`_stream_response`（流式尾 ×3）；各车道只剩真差异（请求整备/URL/错误体形状/响应塑形）。顺手数据质量修复：anthropic 流式路径与转换车道非流式读体错误路径此前漏记 agent 维度（ADR-010 M5 统计口径），现四车道统一归因。count_tokens 契约 genuinely 不同（无 5xx 改写/无用量日志）保持独立
- **ConfigServer 路由表化（架构评审 R2-2）**：23 个端点从「白名单 tuple ↔ elif 链双份声明 + 每 elif 手抄 auth/shape/域调用仪式」收敛为三张路由表（GET 7 / POST 12 / PUT 1，一个端点一行声明）——do_* 只剩表遍历，新增端点 = 表里加一行 + 一个 handler 方法；index 隧道解析三处手抄（test-tunnel / test-forward 旧载荷 / NFS 端点）归 `_saved_tunnel_by_index` 单一归宿。行为零变化（含认证顺序、401/404 边界、旧载荷兼容）；路由表自洽测试钉住表项可调、POST/PUT 不共享路径、端点计数
- **app.py 用户流下沉（架构评审 C1，第一批）**：「未连接绝不拉起」守卫三处手写（菜单翻转 / 桥接 if_connected / 注释互引的保存流同判）收敛进 `ConnectionCoordinator` 单一归宿——`proxy_connected`/`forward_connected` 谓词 + `restart_forward_async(guarded=True)` 守卫重建入口（daemon 线程纪律同归）；65 行翻转闭包的 mutate 构造归 `mpconf.toggle_forward_row` 纯函数（读侧 `forward_row(s)`），app.py 只剩意图胶水；`_open_config_window` 的第二条直启路径（直调 `config_server.start()`）删除——:9528 启停全经 `lifecycle.sync_config_server` 单一归宿。coordinator 新增守卫真值表测试；lifecycle 属性别名清理留作后续批次
- **运行态装饰单一归宿（架构评审 C2）**：`/api/state` 的内联装饰（capture_active/is_proxy/forward_running/nfs_states 约 30 行）上收为 `mpconf.config.decorate_runtime_state` 纯函数——装饰写入的键 = `RUNTIME_DECORATED_FIELDS` 单点声明，`READONLY_DECORATED_FIELDS` 的运行态半边由它派生（两份手维护名单合一）；is_proxy 的角色解析（id→下标→首条）与 merge 收敛为 `resolve_proxy_tunnel` 单一判定（装饰处原为手写第三种变体）。新增一条运行态事实 = 投影加字段 + 装饰处加一个键，config_server 的 `/api/state` handler 不再持有装饰知识
- **供应商卡片 additive（架构评审 C3）**：余额响应语法从 `normalize_balance` 的形状嗅探链移入注册表 API 卡（第 4 元 `parser` 名 + `model_usage_url` 升 `(url, parser)`）——归一按卡精确路由、形状嗅探只作兜底，新增厂商=加一张卡不再往嗅探链加分支；测试机器检查每张卡都带语法名。设置窗 Anthropic 原生端点提示删 JS 平行硬编码表（漂移源），改派生自 `/api/provider-templates` 的 `anthropic_native` 位（单一真源注册表）。刻意的例外：compat 的 `_NEEDS_COMPLETION_TOKENS` 是模型族知识（按模型名前缀、跨厂商适用）不进厂商卡；capture identify() 维持资源契约独立
- **claude_code_setup 收敛（架构评审 C4）**：①tier 规则前缀命中语义归还 `suanpan/router.first_tier_route`（原 `_first_tier_rule` 镜像 router 语义——前缀知识不落第二处，CC 角色种子与 decide_route 同源）；②OpenCode/ZCode 两条 JSON 车道的 owned 槽位 plan/apply 半成品合 `_json_provider_plan/_json_provider_apply`（两份 apply 原为逐字节镜像）——行为零变化，router 侧新增测试钉住逆查询语义
- **四车道发送前置块收敛（架构评审 R5）**：R2-1 骨架之后残留的「try 发送 → except 502 → 5xx 拒绝」前置块仍逐字 ×4（每份 ~14 行，只差 502 塑形器形状）——上收为 `_send_lane(…, wire=)` 单一归宿（返回上游响应或已成形的 502，车道 isinstance 判别即返）；孪生塑形器 `make_502`/`make_502_openai` 合一（`wire` 参数决定 anthropic 平铺 / openai error.message 错误体形状，签名漂移正是此前无法机械合一的原因）；`_LaneCtx.from_body` 折叠三处 `source_model`+ctx 样板；count_tokens 蹭 `_lane_out_headers` 免费复用（aread 非流式纪律保持独立）。四车道各减 ~14 行，只剩请求整备/URL/wire/响应塑形
- **网关对账策略单一归宿 `services/gateway_watchdog.py`（架构评审 R5）**：节奏/连失配阈值/失败退避/忙位此前 macOS 与 Docker 两份手抄——策略参数化收拢，两形态同参喂拍（macOS 1s tick + worker 提交 / Docker 主循环内联），docker/entry.py 回到纯装配（CONTEXT.md 部署形态契约）
- **顺手刀 ×4（架构评审 R5，deletion test 全过）**：`_api_test_forward` 手抄的隧道解析归 `_resolve_tunnel`（与 test-tunnel/NFS 同一守卫与文案）；OpenCode/ZCode 两个单行 apply 代理删除（注册表卡直指 `_json_provider_apply`）；备份三态判断（不存在/首次/保留）CC 与多 Agent 预览双份合一 `_backup_decision`（措辞统一为 CC 版，node 测试钉住）；注册表 `unverified` 死字段删除（零代码消费方——注释宣称的「探测转正」机制从未存在，文档化疑问改注释）
- **用户意图单一归宿 `services/intents.py`（架构评审 R5 C1）**：同一批用户意图（重连分派/转发启停/端口映射行启停写径/挂载启停/抓包开关/打开抓包目录/复制 AI 助手指令）此前在菜单回调与 `_bridge_action` 两套手写 adapter 各写一遍，线程纪律分叉（菜单主线程同步 vs 桥接 daemon Thread）——guard 分派、线程纪律、通知文案、dirty 标记上收 intents 独占，两个 adapter 退化为薄翻译（菜单从状态推导、桥接从 action 字符串映射），`_bridge_action` 的 64 行 if/elif 塌缩为单行分发。行为变化（有意）：菜单「重新连接」与启动转发改为后台线程执行（点击即返回，慢操作不再卡菜单——与桥接路径对齐，host-key 首连的 AppKit alert 经 callAfter 回主线程不受影响；线程名统一为 ReconnectProxy/ForwardStart——前者旧为 BridgeReconnect，现菜单/桥接共路径取中性名）；桥接转发重连分支补 dirty 标记（与菜单路径统一，多一次菜单刷新）；守卫语义零变化（真值表测试钉住）。测试面：新增 tests/test_intents.py 直打公开意图面（含 latch 次序、写径失败中止、如实文案）；test_menu_actions 的 `__new__`+privates 装配改为组合真 intents（同步执行器注入），意图级断言不再经 rumps.App
- **菜单栏 UX 批次（13 项，用户视角走查）**：①故障可见性——转发/挂载 error 态此前顶部完全静默（只数成功），状态行新增「⚠ N 转发异常/挂载异常」计数；②空态死行可点——「添加转发规则…」「配置挂载…」深链偏好设置对应视图（设置窗 hash 从 #quickstart 特判通用化为任意视图 id）；③端口映射单隧道拍平（转发行一级直达，免一层嵌套）+ 代理隧道行明示「启停将重启代理」（点击该组转发行会重启整个代理会话、全流量中断 ~10s，此前无事前提示）；④后台状态翻转不再整树重建——struct_key 改身份签名（隧道/端口对/挂载名），行内状态/尾标/圆点/启停标签由 `_refresh_forward_rows`/`_refresh_mount_rows` 就地刷新，用户展开子菜单时不再被塌掉（挂载 error 详情折进行标题以保结构恒定）；⑤开关统一动词式（「开启/关闭 X」——抓包/系统代理/防睡眠/登录启动/配置 API 五处原「X：开」状态标签式）；⑥断开时状态行保留隧道名；⑦路由启动失败显示短状态（原始错误串不再截断灌进菜单）；⑧「待会话」改「未连接」（内部术语出 UI）；⑨「N 挂载」补量词成「N 个挂载」；⑩「SOCKS5 上游」改「本地代理的上游」；⑪「系 统」组改名「选 项」（与「系统代理」命名撞车）；⑫挂载行统一「·」分隔（原「·」+「—」双分隔符）；⑬SF Symbol 补 accessibilityDescription（圆点着色此前只有视觉通道，VoiceOver 拿不到状态；动态行随刷新更新描述）。工程注记：rumps Menu 以标题为字典键去重——动态行占位标题必须按身份唯一化（重名行互相覆盖，测试实锄）；「重新连接」在转发隧道行改为恒常驻（原仅会话运行中显示——行结构恒定的代价，显式意图本就允许重建已停会话）；挂载尾标保留「挂载中…/卸载中…」省略号
- **状态点着色渲染修复（真机试用两轮实锄）**：菜单里所有着色圆点自始显示黑色（含本应绿色的「已映射/已挂载/随代理运行」）。第一轮归因 SF Symbol 图像默认 `isTemplate=True`（NSMenuItem 对 template 图像按菜单文字色单色渲染），`setTemplate_(False)` 修复——**真机复测仍黑**，符号渲染通道的 template 语义更顽固。第二轮换通道：状态圆点不再用 SF Symbol，改为**手绘位图**（画布直接填色，无可踩的符号机器）——idle 档画黑点保持 template（随菜单文字色自动适配明暗，浅色黑/深色白），绿/黄/红固定色非模板（两种外观可读）；状态语义经行标题文字到达 VoiceOver。同批修复转发子菜单行序回归（逐条转发行回到会话动作之前——v0.12 既定行序在 UX 批次重构中被倒置）
- **菜单状态语法统一（UX 第二批，用户设计评审拍板）**：此前菜单里并存 7 种状态编码通道（彩点/纯文字尾标/动词反推/括号后缀/结构性出现/✓ 单选/计数拼接）与 3 套动词词表。收敛为单一语法——**A 类运行物**（代理/转发会话、转发行、挂载、AI 路由、抓包、系统代理）= 标题动词 + 状态点四值（绿=运行/黄=进行中/黑=未启动/红=异常）+ 行尾状态词；**B 类设置**（防睡眠/登录启动/配置 API）= 中性名词标题 + macOS 原生 ✓（`NSMenuItem.state`），设置不染运行色。落地：转发会话隧道行/AI 路由开关/系统代理开关补状态点（此前有状态无点或状态藏在结构里）；抓包标题纯化（「启动抓包/停止抓包」，状态进点，证书引导与崩溃详情进 hint 行——原「开启抓包模式（需先信任证书）」类后缀全部退役）；设置三项弃动词反推（「关闭防睡眠 ✓」直读当前值）；动词词表两套（启动/停止=进程会话、开启/关闭=开关设置）；组标题异常 rollup（五组 ⚠ n、正常静默——闭合菜单一眼判健康）；状态区分隔符统一「·」。MenuState 去掉四个视图串字段（设置读 config、抓包结构化 enabled/state/hint——视图文本不再由 app 制造）；`capture_controller.menu_state()/hint()` 替代 `menu_title()/error_hint()`

### Fixed
- **五个全局端口的 JS 预检漏显式 0（架构评审 R2-3，报警器首跑实锄）**：`S.mp.socks5_port&&(…)` 的 falsy 短路让 `端口=0` 过第一道闸、只在提交 422 现形（与 R1-C5 的 NFS 端口同类洞）——socks5/http/抓包/配置服务/网关五处全部改 `portInvalid` 共享跳过语义（null/''=未填跳过，0 必报），与 prepare 同判
- **Docker watchdog 与合法 reload 竞态（架构评审 R5 实锤活隐患）**：容器版对账循环是 macOS 版的简化手抄——丢了「连续失配阈值」，单采样落进保存配置触发的合法 reload 端口空窗（3-5s）即误判僵尸态，`start()` 内含 `stop()`，与 reload 线程竞态（每次 reload 约 10-17% 概率触发）。修复即上述策略共享：容器同享 5s 节奏 × 3 连失配阈值 + 30s 失败退避，瞬时失配清零计数绝不误触（回归测试钉住）

### Added
- **跨语言校验漂移报警（架构评审 R2-3）**：`tests/test_validation_mirror.py` + `tests/js/validate_mirror.mjs`——同一语料两侧同跑（Python 分域校验器直调 + 经 extract.mjs 取设置窗真实发布的 LAYER 1），镜像族断言「同错同净」、单侧族（py_only 6 项 / js_only 1 项）显式白名单登记。镜像结构本身保留（双层拦是既定约定），但单侧改规则不跟另一侧、或新增单侧规则不挂号都会红——静默漂移变显式决策。首跑即抓到上述 falsy-0 活缺陷

- **:9528 孤儿监听（架构评审 R2-4）**：开设置窗路径的 except 分支此前重置持有者但不收敛——`show_config_window` 在服务已启动后抛异常时，零持有者状态下 :9528 持续常驻直到下一个偶然的收敛事件。修复：持有者变更收进 `_set_config_holders` 唯一写口（置位/清位与收敛是一个动作，开窗/关窗/复制闩锁/异常路径全部走此口）；唯一刻意不收敛的是开窗启动失败分支（服务未在听，收敛无益——文档化）。测试钉住：异常路径收敛两次、关窗经写口、闩锁经写口
- **远程挂载服务器清单「N 挂载中」徽标恒为 0**：v0.10.1 把 `/api/state` 的 `nfs_states` 值升为对象 `{status,error,fixable}` 后，清单徽标计数仍按旧字符串比较——对象形状全部漏计。计数移入设置窗 LAYER 1，与状态单元格共用同一形状归一助手 `nfsStateValue`，node 测试钉住两种形状的计数。同批修复：挂载 reconcile 确认已挂载/卸载完成两条路径此前不清 `fixable`（陈旧「修复导出」标记可能附着在非异常态进 `/api/state`，现随 error 同步清除）；`/api/nfs-setup-remote` 对 `mounts:[null]` 直接 400 拒绝（此前穿透校验，`shlex.quote(None)` 在 handler 线程抛 TypeError）
- **设置窗端口冲突预检漏 NFS 本地端口（架构评审 C5）**：JS `validateConfig` 手抄镜像 prepare 校验器时漏了 NFS 端口——NFS×转发/全局端口撞车过第一道闸、只在提交时 422 现形。现补齐同一命名空间与同口径「实际在用才占端口」（enabled 或配置了挂载；纯默认节点不占），并补 NFS 端口行级范围校验；node 测试钉住冲突双向 + 默认节点不误报 + 越界行级报

## [v0.11.0] — 2026-09-22 — ADR-010 协议矩阵：三协议入站 + 一个 Key 配好全部 Agent

### Added
- **三协议入站网关（ADR-010）**：Anthropic Messages / OpenAI Chat / **Responses（Codex）** 三协议入站，路由到 GLM/DeepSeek/Kimi/Qwen 等多供应商——同协议直通优先、失配才经 `compat.py` 转换器（Anthropic⇄OpenAI Chat 请求/响应/SSE 全量翻译，含流式）
- **一个 Key 配好全部 Agent（多 Agent 注册表引擎）**：快速接入向导选厂商填 Key → 自动探测端点（存在性/认证/模型清单三级）→ 勾选 Agent 一键配置；支持 **Claude Code / Codex（tomlkit 增量编辑 config.toml）/ OpenCode / ZCode**——Agent 里写入的是本地网关凭证，厂商 Key 永不落 Agent 配置。菜单新增「配置 Agent…」深链直达向导；供应商注册表升级端点矩阵（每厂商 anthropic/openai/responses 端点卡 + 认证头 + 套餐变体）
- **配置 API 常驻开关**（ADR-009 配套）：系 统 ▸ 菜单 + 设置窗系统页双入口

### Changed
- **「保存并同步」同时写入网关 tier 路由规则与 Claude Code env（规则=持久真相）**：同一用户意图（"opus 档用哪个模型"）此前存在两份持久真相——网关 `sp.rules`（别名流量路由）与 settings.json env（Claude Code 发什么模型名），且后者有编辑器、前者无任何 UI 入口——env 已升 glm-5.3 而规则停留在 glm-5.2 的"幽灵规则"持续吃别名流量（真机案例：2966 次请求路由到已裁剪模型，Claude Code 同步页三行「未在清单」）。修复：`setup()` 把角色表 upsert 成 tier 规则（`_plan_rule_changes` 纯函数：只改精确前缀匹配的既有规则或追加，不重排、不删更细前缀的自定义规则；推导路径 roles=None 只对齐不新增），经 ConfigStateStore 事务写 `~/.suanpan.yaml`（与 UI 保存同一校验，规则先行失败则 settings.json 不动）；`already` 两面都一致才成立；规则写成功即触发网关热重载。确认弹窗新增规则 diff 表 + 「未在清单」软警告（上游可能仍服务未列模型——警示不拦截）；drift 横幅文案讲清两面结构与两条对齐路径
- **配置 API 默认不再常驻监听 :9528（ADR-009）**：设置窗对配置 UI 而言是"用完即走"的，此前却随应用启动常驻占端口 + 维持一整面 token/host 守卫。改为三持有者状态机（`config_server_wanted` 纯函数）：设置窗开着 / 「复制 AI 助手指令」会话闩锁（复制即自动开启供 agent curl，本次会话保持）/ `config_api_enabled` 常驻开关——任一在场才监听，全离场即释放端口。Docker 形态不受影响（恒常驻）；`agent_instructions()` 文案与 agent.md 补服务开启提示；启动期端口占用报告对未绑定端口不再告警
- **「关于」页全量重写**：从一行式旧文案到五域全景（代理/挂载/AI 路由/抓包/系统各一条关键信息）

### Fixed
- **保存触发网关热重载可致其永久下线（真机案例）**：uvicorn 优雅停机默认**无限等待**在连连接关闭——reload 时 Claude Code 的 keep-alive/重试连接挂着不放 → `join(3s)` 超时 → 僵尸线程使 `running` 恒 True → 后续 `start()` 被拒，:9527 再也不监听（用户视角：Claude Code Connection refused 重试 10 次）。修复：`uvicorn.Config(timeout_graceful_shutdown=2)`——2s 宽限后强断残余连接，停机有界（< join 3s），reload 自愈

## [v0.10.1] — 2026-09-20 — 挂载失败可见性链路 + 浏览器设置页复制指令修复

### Added
- **挂载失败「没反应」可见性链路 + 一键修复导出（真机案例驱动）**：加挂载项未先「一键安装」→ 远端无导出 → `mount_nfs` ENOENT 裸透如天书，且设置窗是不刷新的快照——错误永远不出现。四级修复：失败分类给中文指引与 `fixable` 标记（MountState 第 6 字段）→ `/api/state` 的 `nfs_states` 装饰升级 `{status,error,fixable}`（error 不再丢弃）→ 设置窗状态徽标 + 错误行内红字 + `fixable=exports` 时「修复导出并重挂」按钮（一键安装 → 自动重挂）→ NFS 视图 5s 定向轮询（mergeRuntimeDecorations 纯函数只合并装饰、绝不碰表单，node 测试钉死不变量）

### Fixed
- **浏览器直开设置页时「复制 AI 助手指令」按钮无反应**：#70 S13 把文案拼装移到原生侧后，浏览器场景的 `nativeSend` 静默短路，按钮成了哑巴（其余 bridge 操作均有降级 toast，独此一处缺席）。新增认证 `GET /api/agent-instructions`（文本经 `instructions_fn` 取自 `agent_instructions()` 单一归宿，与原生路径逐字节一致）；JS 双通道——WKWebView 走 bridge 不变，浏览器 fetch 后 Clipboard API 写剪贴板（Safari 手势不跨 await 时退 execCommand 兜底）+ toast 反馈

## [v0.10.0] — 2026-09-18 — 保存流可靠性三连修（随 NFSv4 版本发布）

### Fixed
- **保存后 SSH 隧道变成另一条连接串（实测）**：设置窗隧道页 collect 曾把「正在查看/编辑的隧道」（activeTunnel，纯 UI 状态）隐式写进代理角色并随保存落盘——变更潜伏到下一次重连才显形，用户视角即隧道随机切换。collect 自此只读表单；角色在设置窗的唯一写径是新增的显式**「设为代理隧道」**按钮（dirty 跟踪、可放弃；当前代理渲染徽标），保存后经「重新连接」应用（切换会断现有会话，绝不随保存自动断）
- **UI 保存后应用按旧配置行动**：PUT 只落盘 + reload 网关，app 内存副本要等下一次重连才重读——防睡眠/抓包设置/代理角色在窗口期全按旧值行动。新增 `on_mp_saved` 回调链（config_server 按事务段分发 → app 重读磁盘刷新内存 + 标记菜单重建）
- **登录启动两条写径漂移**：UI 保存路径此前只写配置文件、从不注册 LaunchAgent（只有菜单路径注册）——`on_mp_saved` 收敛时 `launch_at_login` 有变即补注册/注销，与菜单路径对齐
- **代理角色下标漂移（结构性）**：`current_tunnel` 存数组下标，删除/调序隧道后同一下标指向另一条隧道。角色迁移稳定 id 双表示：`current_tunnel_id`（t- 前缀）是唯一真相，`current_tunnel` 降为旧版本读兼容 + merge 派生投影——解析序全链路同一语义（id → 悬空/缺省回退下标 → 首条：merge / 连接协调器 / 菜单 / is_proxy 装饰 / 前端）。旧配置零迁移即兼容，回滚旧版 app 亦不断

## [v0.9.1] — 2026-09-17 — 菜单栏重组：代理/端口映射分离 + SF Symbols 图标

### Changed
- **菜单五组分区**（HTML 原型定稿）：状态区（着色圆点图标取代 emoji，流量行图标化）→ **代 理 ▸**（只管唯一 -D 会话：启停/暂停/重连/系统代理/代理角色单选/经代理启动 ▸）→ **端口映射 ▸**（只管纯 -L 多活会话：代理隧道显示「随代理运行」信息行，其余隧道各自启停/单会话重连/端口摘要，无规则给指引）→ AI 路由 ▸ / 抓 包 ▸ → **系 统 ▸**（防睡眠/登录启动从页脚收进来）→ 精简页脚
- **SF Symbols 图标体系**：`menu_builder._ICON` 符号表（SF 1/2 · macOS 11 基线）+ `_symbol_image`（尺寸/着色配置链）+ `_apply_icon`（挂 `NSMenuItem.setImage_`，任何失败静默降级纯文本）；状态行动态系统色明暗自适应
- 防睡眠/登录启动开关文案 refs 化，随 refresh_titles 动态刷新（不再依赖整菜单重建）

### Added
- 页脚新增「复制 AI 助手指令」直通项（免开设置窗；app 公开 `copy_agent_instructions` 回调）

## [v0.9.0] — 2026-09-16 — 多隧道并行（代理隧道 + 转发会话）

### Added
- **多活模型**（决策落档 ADR-005）：服务器 A 跑 SOCKS5 代理、服务器 B 同时跑端口映射不再是梦想——`current_tunnel` 语义明示为**代理隧道**（唯一 -D 会话），其余隧道可各自「启动端口转发」为纯 `-L` 转发会话并行运行；每会话独立 monitor/retry/host-key 三件套（host-key 告警互不吞）、独立退避重试、唤醒全量僵尸重建
- **`forward_autostart` 持久字段**：转发会话随应用启动自动恢复（`apply_autostarts` 收敛）
- **菜单栏每隧道子菜单**：设为代理隧道 / 启停端口转发 / 单隧道重连；行尾状态（已连接・代理 / 转发中 / 未连接）；状态行附「N 条转发」计数（主图标语义不变——只反映代理会话）
- **设置窗多活面**：非代理隧道详情栏「启动/停止转发」（经 bridge `forwardSession`）；master 列表「转发中」徽标；「随应用启动转发」开关；「重新连接」带隧道身份（修正既有偏差——此前重连的永远是 current 而非正在查看的隧道）
- **守卫重连逐隧道定向**：`reconnectProxy {if_connected, tunnel_id?}`——运行中的转发会话各自按连接态守卫重建，未运行绝不拉起；代理隧道保持 v0.8「同一身份当前隧道」语义
- `/api/state` per-tunnel 装饰：`is_proxy` / `forward_running`（READONLY_DECORATED_FIELDS 模式，永不落盘）

### Changed
- **[破坏性] 转发本地端口全局唯一**：多活下任意隧道可并行，两条隧道抢同端口会在 `ExitOnForwardFailure` 下互顶死循环——v0.8 的「跨隧道同端口合法（单活豁免）」作废，prepare 与 JS 双层拦（0.8.0 当日发布，存量影响≈0）
- **切换代理角色 = 降级续跑**：旧代理隧道有 forwards 则转纯转发会话继续跑，无则停（此前单活语义为彻底断开）
- 防睡眠按聚合状态：任一会话在跑即防睡；「暂停」仍仅作用于代理会话

### Fixed
- **切换流降级判定 bug**（本迭代实测）：restart 的降级对象改为 `_launched_proxy_id`（实际跑着的隧道）——`current_tunnel` 在 restart 前就已被切换流写成新值，读它永远降级失败

## [v0.8.0] — 2026-09-16 — 端口转发（ssh -L 本地转发）

### Added
- **per-tunnel 端口转发**：把远程服务器可达的 `remote_host:remote_port` 映射到本机 `127.0.0.1:local_port`（如远程 8000 → 本机 9000）。配置存 `tunnels[i].forwards`，设置窗「隧道」详情内以表格编辑（增/删/行内测试），绑定地址恒为回环；旧配置无此字段自动兼容，无需迁移
- **行内一击式测试**：`POST /api/test-forward` + `ssh_launch.probe_forward`——用与隧道完全一致的认证/主机密钥策略发起一次性 `ssh -W` 探测**表单当前值**（隧道与转发行都未保存可测），返回可达性 + 延迟 + 分类中文错误（远程拒绝/超时/SSH 层失败分口径）；不依赖隧道当前状态
- **保存后守卫自动重连**：**同一身份的当前隧道**自身 forwards 有变且已连接时，经 bridge `reconnectProxy {if_connected:true}` 自动重连应用（新增载荷旗标；原生侧仅 `status=="connected"` 才执行，绝不拉起未连接的隧道；改其他隧道的转发不打断当前连接，切换当前隧道走手动流）。显式点击「重新连接」行为不变
- **校验双层拦**：prepare 与 JS validateConfig 同口径——同隧道 local_port 互斥、不撞全局保留端口（socks5/http/抓包/9527/9528）、remote_host 须主机名或 IPv4（暂不支持 IPv6）；跨隧道同端口合法（单活）

### Fixed
- **merge 浅拷贝防护**：`DEFAULT_TUNNEL.copy()` 浅拷贝下 forwards 列表默认值会跨隧道共享——归一化逐行全新构造（新增测试钉住）；非法端口读路径落 0（下次保存被拦）绝不静默丢行

## [v0.7.3] — 2026-09-05 — 分层架构 DAG + 双语 README / 开源基建

### Changed
- **分层 DAG 落地（#91）**：新增 `shared/` 跨域叶子层（netloc / provider_auth / keychain / stats / config_store / subprocess_monitor / defaults / identity），消除全部 6 条域间横向依赖边（含 tunnel→services 核心域反依赖编排层）；`config_store` strip 成纯持久化原语（PATHS + 原子写），sp_* 编排桥拆出 `services/sp_config.py`；`IdentityMigrationError` 归位 `shared/identity.py`（消 suanpan/config 底部延迟导入）。零行为变化，1631 测试全绿
- **评审收尾（#92）**：文档尾巴（entry.py / docker-deploy.md import 链）+ mpconf/config.py 中部导入归位 + 字面量 `9527` 三处收敛 `shared/defaults.py DEFAULT_GATEWAY_PORT`

### Added
- **层次守卫 `tests/test_arch_imports.py`（#91）**：分层 DAG 钉死成测试契约——只许向下 import、同层只许同域；唯一白名单 `mpconf→suanpan`（config_state 双文件事务边界，带理由）。新增横边在测试里红，不在三个月后的依赖迷宫里红
- **开源基建**：README 双语重写（英文主 + `README.zh-CN.md`，GitHub SEO 关键词布局 + mermaid 架构图）、MIT LICENSE、CONTRIBUTING.md、社交预览图；GitHub 侧 description / 16 topics / Releases 补齐

## [v0.7.2] — 2026-09-02 — relay 死写告警治理

### Fixed
- **relay 死写告警刷屏**：relay 写端断开后转发循环曾继续向已关闭 socket 死写，asyncio `socket.send()` 告警刷屏日志 → 写端断开即停止转发

### Added
- **CC 角色表种子读回**：Claude Code 同步页——已同步的机器按 live env 回显角色模型映射，配置与实际环境漂移时提示

## [v0.7.1] — 2026-08-29 — 隧道断线四连修（#85–#88）

### Fixed
- **重试耗尽永久躺平（#85）**：退避表耗尽后曾直接放弃且无自愈路径（实测断线 2h18m 靠手动恢复）→ 无限退避（60s 封顶）+ `check_ssh` 放行 error 状态持续调度；暂停态不重连语义保留。断线自动恢复 ≤60s
- **SSH 参数调优（#87）**：`ServerAliveInterval` 30→20（判死 90s→60s，闪断恢复快 30s）+ `IPQoS=none`（防中间设备按 DSCP 针对性丢包）+ `ConnectionAttempts=3`（建连自带重试）

### Added
- **唤醒立即重连（#86）**：新模块 `tunnel/reconnect_trigger.py`——NSWorkspace 唤醒事件 → 跳过退避立即重建（connected 视为僵尸链路主动拆建，恢复 ~2 分钟 → ≤5s）；事件去抖 ≥2s。Wi-Fi 切换即时恢复需可选依赖 `pyobjc-framework-SystemConfiguration`（未装时由 #85 兜底 ≤60s）

### Changed
- **日志降噪（#88）**：SOCKS5 连接失败按分钟聚合（首条完整 + 滚动汇总）——曾占日志 82% 的 Errno 61 刷屏压到每分钟 ≤2 条；stats 计数语义不变

## [v0.7.0] — 2026-08-23 — 两轮架构评审全量落地（19 issue，可靠性/一致性大修）

### Fixed
- **count_tokens 必 500（#44）**：流式响应未读即 `r.json()`（ResponseNotRead 逃出 except 链）→ `aread()` 后消费；读体错误并入 502 塑形
- **AsyncRuntime 自死锁（#45）**：非可重入锁二次获取（冻结整个菜单栏）+ 一并修复揭开的 join 未启动线程竞态
- **配置写入大一统（#46）**：菜单开关/校验双轨/local_token 弱写/stale 副本丢更新四违例收敛唯一事务管线（`update_mp` 写前读新 + pydantic 并入事务路径）
- **手编配置体验三件套（#47）**：发货样例过自己的 schema + null 节归一（`rules:` 空节不再炸）+ 网关配置错误中文塑形
- **Docker 两缺口（#48）**：journal 启动重放（此前无人调 recover）+ 网关失败 stderr 宣告 + web 修复闭环补真
- **配置体验/凭证（#66）**：local_client_token 穿越保存存活（merge 保留注册字段 + 掩码契约合规——明文不出 UI）
- **桥接重连线程契约（#68）**：ConnectionCoordinator 持锁（假注释成真）+ alert 经 AppHelper.callAfter 回主线程
- **崩溃族整批（#69）**：prepare 畸形 rules / addon request 守卫 / Connection 多 token / config_server 非 dict body / middleware 非 ASCII / decide_route 非字符串 model 等 10 点形状守卫
- **校验单主化（#70）**：端口冲突上收 prepare（agent 直 PUT 也拦）+ 路由文法收敛（逗号 default 不再误拦）+ 复制 AI 助手指令原生侧拼 token（不再复制空 Bearer 401）+ 抓包目录经 prepare 建（不再自毁安全契约）
- **单实例守卫生效（#65）**：ps 单行输出解析修正（此前活实例锁永远被抢走）+ LC_ALL=C 钉死
- **CI 覆盖率门禁（#64）**：pytest-cov 入 lock（此前 --cov 即 exit 4，门禁从未验证任何 commit）

### Changed
- **structlog→stdlib（#67）**：统一日志栈——#50 可感知回退告警在打包形态下进 MagicProxy.log/日志窗
- **架构深化批（#71）**：keychain PEP604（3.9 下界）+ on_sp_saved 双形态统一 + ConfigServer 端口 seam + 状态全集声明 + 抓包命名归位
- **供应商注册表收敛（#51）**：单一真源（余额端点/UI 模板共消费）
- **路由 fall-through 可感知化（#50）**：显式意图误投携带 `x-suanpan-fallback` 响应头 + 日志宣告（绝不静默误投）

### Chore / Docs
- 清道夫（#49）：死代码簇/rolling 只写状态/manifest 反向守卫/ADR 自洽
- 遗留 WE 处置（#52）：只读装饰字段单点声明 + 评估记录
- 摩擦点清理（#53）：常量时间比较/哈希合一/错误分类/文档指针等 8 项
- 测试债清理（#72）：恒真断言/死 helper + R7a 两项（重命名级联/toast 代际守卫）
- 文档卫生（#82/#83）：废弃引用清理 + CLAUDE.md/CONTEXT.md 写作卫生

## [Unreleased]

### Changed
- **SSH 调用策略单一归宿（#42）**：新模块 `tunnel/ssh_launch.py` 独占 argv 构建（host-key 三件套 / sshpass-via-fd / -i 传参）、一次性探针 `probe()` 与 stderr→中文失败分类；`SSHMonitor.start` 与 `config_server.test_tunnel` 退化为调用方——探针与真实隧道行为恒等。顺带修复 `_start_process` 抛异常时密码 fd 泄漏。
- **ConfigServer / SuanpanRuntime 参数化（#43）**：`bind_host`/`token` 构造参数取代 Docker adapter 的私有符号接触与管线抄写；`install_macos_stubs` 机制删除（`sysctl/keychain` 的 Security 可选导入）；Docker 网关首启缺配置时自建默认配置（原为静默死线程；默认配置含 usage_log → 数据卷，重建不丢）。

## [v0.6.1] — 2026-08-22 — 套餐三窗口配额 + 文档整固

### Added
- **余额速览三窗口配额（#41）**：GLM/Kimi 等套餐类供应商统一展示 5小时/每周/每月。月度「两者结合」——API 有月度（Kimi totalQuota）用供应商口径，否则聚合本网关 usage.jsonl（UI 标注「每月（网关）· N 次」，无本地数据补 0 行）；GLM Max 的 TIME_LIMIT 工具时长配额标注「每月·工具」，不抑制本地 token 行。GLM 配额显示 nextResetTime 重置时间，所有 reset 统一转 CST。`fetch_usage` 新增 `month` 范围（CST 自然月），`/api/usage?range=month` 随之可用。

### Fixed
- **agent.md 六处陈旧**：`listen_port` 整型示例（旧 `listen` 读时兼容）、usage 日志真实路径、Docker 取 token 路径（`suanpan.sh config-ui`）、登录页行为、`range=month`、菜单顺序对齐代码装配序；CLAUDE.md 测试口径改 `node --test tests/js/*.test.mjs`（node ≥26 目录模式失效）。

### Development
- README 补「方式四：Linux / 无 GUI（Docker）」部署说明。

## [v0.6.0] — 2026-08-21 — Docker 版：Linux 无 GUI 部署 + Web 管理

### Added
- **Suanpan 网关 Docker 版（#22 / PR #35）**：python:3.12-slim 镜像 + compose + `docker/suanpan.sh` 管理脚本（up/down/status/logs/sync/config-ui）；Linux 无 GUI 跑网关（:9527），`sync-claude-code` 一键写入 Claude Code 配置；PyObjC Security stub 让 macOS 专属 import 链在 Linux 容器零修改通过；首启引导默认配置落 /data 卷。
- **Docker 配置页面 :9528（PR #37）**：复用 config_server 的完整 Web 管理界面（供应商/路由/统计/余额），token 与 sync 同源零新 secret；Linux 无 Keychain 经守卫走 SP-only 保存。
- **登录页（PR #39）**：裸 GET `/` 无凭证返回自包含登录页——浏览器打开 :9528 输 token 即入；`/api/*` 的 401 保持纯 JSON，macOS 桥接带 Bearer 零回归。

### Fixed
- **配置页保存后网关热重载（PR #40）**：GatewayRunner 线程化 uvicorn + `on_sp_saved` 回调接线——Docker 版保存配置即时生效，无需重启容器。

### Development
- 新增 docker-deploy.md 部署文档，两轮 writing-for-agents 收紧（PR #36/#38）。

## [v0.5.0] — 2026-08-21 — 安全·事务·韧性：16 issue 加固收口

### Added
- **实例所有权 InstanceOwnership（#3 / PR #18）**：pid+启动时间双匹配抗 PID 复用，取代 basename 误杀；O_EXCL 原子创建/陈旧接管/release。
- **AuthenticatedHttpClient（#4 / PR #21）**：认证出站统一 adapter——跨 origin 重定向一律拒绝、HTTPS→HTTP 降级必拒、1MB 响应上限；凭证不出原始 origin。
- **ConfigStateStore（#6 / PR #23）**：配置持久化事务边界——load 四态 / prepare 全量校验 / commit（journal+MP+SP+Keychain 次序）/ recover 幂等重放，收编 PUT/首创建/启动恢复全部路径。
- **RetryPolicy（#7 / PR #24）**：流式代理有界重试——pre-send 证明或幂等才重试；非幂等 POST 送达后不明即不重放。
- **Provider/Tunnel 稳定持久 id（#8 / PR #26）**：凭证与可编辑字段解耦；live PUT 掩码恢复 + re-pin 串线守卫。
- **本地客户端 token（#9 / PR #34）**：sync 与 config-ui 同源同值，落配置卷跨容器重建稳定。
- **header-only token（#10 / PR #25）**：token 只进 Authorization 头与 `cfgsess` HttpOnly SameSite=Strict 会话 cookie；query-string 认证删除。
- **UsageExtractor 有界线性增量 scanner（#13 / PR #30）**；**UsageSink 吞错韧性 + ProviderPrewarmer 有界预热（#15 / PR #31）**。
- **发布工程单一契约（#14）**：资源清单 + requirements-lock 锁定依赖 + CI 门禁（lock 漂移校验）。

### Fixed
- **抓包资源契约（#2 / PR #17）**：`resolve_capture_resources` 收编 mitmdump 三级链 + addon 校验 + 目录 preflight；frozen/dev 双态冒烟判据单一归宿。
- **明文 HTTP 逐请求归属（#5 / PR #19）**：跨 origin 安全重连状态机——keep-alive 连接绝不静默误投他站；1xx 接续、拒绝先于转发。
- **AsyncRuntime 锁下状态机（#12 / PR #28）**：竞争窗口与 coroutine 泄漏封死；start() 保 bool 契约（拒绝返回 False 不抛异常）。
- **抓包默认流式 + 单一聚合预算 + store 收敛（#11 / PR #27）**。
- **HTTP framing 加固收尾（#20 / PR #32）**。

### Changed
- **文档收口（#16 / PR #33）**：漂移修正 + 历史标记 + 高风险守卫；CLAUDE.md 按 writing-for-agents 杠杆修整。

### Development
- lifecycle_runtime 测试覆盖率 86%→100%。

## [v0.4.11] — 2026-08-19 — 仓库重建 + 全域包架构

> 仓库历史自 2026-08-19 重建起算（初始提交 VERSION=0.4.9，bundle ID 更换为 `com.benzai.magic-ai-router`，remote 迁往 benz-ai-x）；此前条目中的 issue 编号指向重建前仓库。

### Changed
- **根目录全域包整理**：38 个模块归入 6 个域包（mpconf/tunnel/shellui/capture/sysctl/services），根目录 54→14→9 项，可重建产物清除。
- **架构候选落地**：saveAll 状态机下沉为 LAYER 1 深模块 saveFlow；ConfigServer 回调改 server 实例注入（并行测试不再串话）；ServiceCoordinator 升格 LifecycleRuntime（start_all/quit 顺序契约）。

### Development
- ADR 重编号为连续序列（022-025 → 001-004），全量引用同步；CI 路径同步 scripts/ 迁移，sit 用例 import 补域包迁移。

## [v0.4.8] — 2026-08-14 — Claude Code 同步 + 安全与文档整固

### Added
- **Claude Code 同步功能**：角色映射 UI（default/tier/subagent 独立控件）+ 后端派生；种子载荷 `{roles, order, labels, readonly}`，Python 单一真源（`/api/cc-default-roles`）。

### Fixed
- **安全（#39）**：bearer token 改走 `Authorization` 头，不再出现在 URL query。
- **代码审查修复（#40，16 项）**：明文迁移失败隔离 + keychain 移出 `-w` argv；host-key 后台线程守卫；菜单每秒重建与退出阻塞修复；进程身份识别（`_clear_app_ports` 只杀自身旧实例）；bare IPv6 解析；pgrep/osascript 转义；LaunchAgent 原子写；keychain 密码仅显式切换才删；SSE 流错误记录 + usage 载荷守卫；`count_tokens` body 归一化。
- **CC 推导语义（#42/#43）**：角色 tier 推导改双向前缀首击（对齐 router.py）；subagent 与 default 解耦（haiku tier 目标）。

### Changed
- **CC 模块清理（#44）**：删 Middle-Man 适配器 + 双份角色定义 + 死参数；`one_m` → `ctx_1m`（保留旧键只读 fallback）。

### Development
- **文档防漂移（#41）**：CLAUDE.md 全面更新 + `tests/test_docs_drift.py`（版本单一真源 / 模块清单 / 陈旧短语守卫）；ADR-000 移除陈旧现在时表述。

## [v0.4.7] — 2026-08-13 — 架构深化 + 覆盖率 100% + 安全修复（#37）

### Changed
- **架构深化（4 候选 + ADR-002）**：ServiceCoordinator 瘦身；`http_listen`/`listen` 收敛为整型端口字段 + 读时兼容旧格式；原子写收敛到 `config_store`；`claude_code_setup` 独立模块。
- **UI 信息架构优化（#36）**：导航副标题 + 面板分组对齐产品 + 状态页引导文案 + Base URL 预设提示。
- **余额配额行结构化（#34）**：`normalize_balance` 返回结构化 quotas 数据，UI 统一渲染。

### Added
- **ADR-002**：配置表示与掩码决策固化（`api_key_set` 布尔契约，真实 key 不出进程）。

### Fixed
- **安全 + 正确性（#37，15 项）**：明文密码不落盘 + keychain 日志脱敏；token 常量时间比较 + body 上限 + 负数 Content-Length 防护；SSE CRLF 兼容 + 逗号 target 校验 + auth_header 大小写；PyObjC 方法名规避单下划线 + CA 路径延迟计算。

### Development
- 测试覆盖率 94% → 100%（+134 测试，5 个 `test_cov_*.py`）；CLAUDE.md 去 cache（#38）；新供应商调研：MiniMax / Qwen / 豆包原生 Anthropic 兼容（#35）。

## [v0.4.6] — 2026-08-12 — Claude Code 自动配置 + 网关兼容层

### Added
- **AI agent 集成**：`agent.md` 产品上下文 + Claude Code 自动配置（`claude_code_setup.py`，写 `~/.claude/settings.json`）。
- **网关兼容层（`suanpan/compat.py`）**：system 数组展平 / document 块剥离 / beta tool 字段剥离 + 客户端 `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` 兼容变量。

### Changed
- **架构深化**：`drain_and_log` 从闭包提取为独立 async generator；middleware 提取到 `suanpan/middleware.py`；删除 MenuActionHandler 浅模块；SSH 错误结构化（`is_host_key_changed`）。

### Fixed
- `stop_all` 停止 Suanpan 网关；删除死 admin.html；config_ui 描述路径修正（`~/.zshrc` → `~/.claude/settings.json`）。

## [v0.4.5] — 2026-08-11 — 传输级重试

### Fixed
- **传输级错误自动重试一次**（`_RETRYABLE`：NetworkError/ProtocolError/ProxyError/UnsupportedProtocol），消除上游静默关闭空闲 HTTP/2 连接导致的 502；Timeout 明确不重试。

## [v0.4.4] — 2026-08-11 — 用量提取兼容 + 服务重启菜单

### Added
- 代理隧道 / AI 路由菜单新增**重启服务**；KIMI 5 小时/周额度显示 + 余额按百分比染色；关于菜单版本号追加编译时间戳。

### Fixed
- **SSE 解析兼容 `data:` 无空格前缀**（修复 KIMI token 提取）；token 提取改 **max 合并**，适配四家供应商 SSE 格式分歧。

### Development
- 版本断言从 build.sh 动态读取；README 营销口吻重写 + 修正过时事实。

## [v0.4.3] — 2026-08-10 — 架构重构 6 候选落地 + config_ui 模块化

### Changed
- **config_ui 内部模块化（#21）**：三层结构（LAYER 1 纯逻辑可 node:test / VIEWS 注册表 / 渲染层）。
- **ConfigStore 统一配置栈**：路径注册表 `PATHS` + 共享原子写原语；`bridge_protocol` 纯 Python 核心；`netloc` 收敛 host:port 解析为唯一所有者；供应商认证收敛为单一纯实现（`provider_auth.py`）。

### Fixed
- 菜单信任检查加 30s TTL 缓存（消除每秒 verify-cert 子进程）；测试套件不再触碰真实配置文件。

## [v0.4.2] — 2026-08-10 — 架构重构 + UIUX 优化 + AI 路由增强

### Changed
- **架构重构**：抽取 `AsyncRuntime` / `ConnectionCoordinator` / `ServiceCoordinator`；菜单栏重构（双状态行 + 连接控制收入子菜单）。
- 设置页面 18 项 UIUX 修复。

### Added
- **GET `/v1/models`** 端点（Anthropic 兼容格式，`provider/model` 形式）；**网关热重载**（保存后自动 reload）；端口可配置（9527/9528）；启动时自动清理端口占用。

### Removed
- `suanpan/admin.py` 孤儿代码（−336 行，管理面并入 :9528 配置面板）。

## [v0.4.0] — 2026-08-09 — Suanpan AI 路由网关 + Webview 配置 + 架构重构

### Added
- **Suanpan AI 路由网关**（`suanpan/` 子包）：将多家 LLM 后端统一为 Anthropic Messages API，按请求场景（默认 / 后台任务 / 长上下文 / 推理）与模型规则路由转发，支持内联覆盖与 `SUBAGENT-MODEL` 标签。含流式代理、SSE 用量提取、JSONL 用量日志（50MB 轮转）、token 估算、admin 控制台。
- **Webview 配置界面**：以 WKWebView 窗口（`webview_window.py`）+ Web 配置服务（`config_server.py`，`:9528`，JSON CRUD + bearer token + 余额 / 用量查询）替换原生偏好设置窗。自包含 HTML 面板（`config_ui.html`）采用侧边栏分组导航，覆盖供应商 / 路由策略 / 模型规则 / 运行状态。
- **Suanpan 运行时集成**（`suanpan_runtime.py`）：延迟导入，依赖未安装时应用正常启动并提示安装命令。
- **领域术语表**（`CONTEXT.md`）：建立单上下文仓库的领域 SSOT，统一产品结构与路由术语。
- `suanpan.example.yaml` 示例配置。

### Changed
- **架构重构**：`app.py` 瘦身为纯编排器，提取 `config.py`（配置 I/O）、`menu_builder.py`（菜单 UI + 状态图标）、`sys_proxy_controller.py`（系统代理收敛状态机）、`retry_scheduler.py`（SSH 重试退避）、`host_key_flow.py`（主机密钥信任流程）、`subprocess_monitor.py`（`SSHMonitor` / `CaptureMonitor` 共同的子进程生命周期基类）。
- 仓库更名 Magic-AI-Router，反映双产品（Magic Proxy + Suanpan）定位。

### Fixed
- Config server：路径匹配前剥离 query string；webview autoresizing mask 布局问题；bearer token 认证。
- Config UI 全量 bug 修复 + 布局重设计。
- `SSHMonitor` 封装改进与死代码清理；3 项代码审查发现修复。

### Security
- 移除 allowlist 中的 blanket `curl` 权限。

### Development
- 新增 6 个测试文件，覆盖 `config_server`、`ai_capture_addon` 深度边缘用例（+43 测试）、`suanpan/proxy`、`suanpan/router`、`suanpan_runtime`、`subprocess_monitor`。

## [v0.3.7] — 2026-07-25 — 防睡眠与登录启动

### Added
- **防睡眠模式**（`sleep_blocker.py`）：代理运行期间阻止系统进入睡眠，避免 SSH 隧道因休眠断开。
- **登录启动**（`login_item.py`）：可将应用注册为 macOS 登录项，开机自启。
- 新增 `test_login_item.py` / `test_sleep_blocker.py` / `test_config_compat.py` 覆盖新功能与配置兼容性。

## [v0.3.6] — 2026-07-13 — 全新图标与三态连接指示

### Changed
- 全新 macOS 应用图标，以“网络隧道 + 双向代理流量”取代通用网络节点图形。
- 菜单栏使用自定义隧道双向箭头图标，不再使用三节点 SF Symbol。
- 连接状态统一为三种颜色：灰色表示未连接，黄色表示连接中，绿色表示已连接。

### Fixed
- 已停止、连接失败和暂停状态不再误显示为黄色或红色，统一显示为未连接的灰色。
- 构建流程明确使用新版 `.icns`，并验证 44px Retina 菜单栏图标资源随应用打包。

## [v0.3.5] — 2026-07-13 — 安全与运行时稳定性修复

### Security
- 抓包代理强制仅监听 loopback，抓包目录/文件强制使用 `0700`/`0600` 权限。
- HTTP 代理拒绝 LAN 监听，限制请求头大小和读取时间，并过滤代理认证头。
- SSH 首次连接显示 SHA256 主机指纹，确认后使用应用专用 `known_hosts` 严格校验。

### Fixed
- 系统代理在关闭时恢复用户原有配置，不再无条件关闭。
- 修复抓包目录 `~` 展开、公证脚本 mitmdump 验证路径和子进程回收问题。
- 增加配置结构与端口校验，损坏配置会备份而不是导致应用崩溃。

### Development
- 构建统一使用 Python 3.12，新增固定版本开发依赖、macOS CI 和代理核心回归测试。

## [v0.2.0] — 2026-07-08 — 菜单栏增强：About / 实时日志窗口 / 偏好快捷键

### Added
- **About 菜单项**：点击弹窗显示当前版本号与一行应用说明（版本号与 `build.sh` 同源）。
- **实时日志窗口**（`log_window.py`）：菜单"📜 查看日志"打开原生窗口，实时滚动展示代理 / SSH 运行日志，快捷键 `Cmd+L`。
- **偏好设置快捷键**：菜单内可直接打开原生配置窗；配置窗口加宽更易读。
- **分发工具链**：`build_dmg.sh` 将 `.app` 封装为带 Applications 拖拽安装的 `.dmg`；`notarize.sh` 走 Developer ID 签名 + notarytool 公证全流程（`.app` + `.dmg`，凭证存 Keychain）。

### Changed
- 移除"启动终端"功能及 `launcher.py`（不再经 osascript 启动 iTerm2 / Terminal）。

### Fixed
- `build.sh`：`launcher.py` 已在批次1删除但 `--add-data` 仍引用，导致 PyInstaller 构建失败；替换为新模块 `log_window.py`。
- `build.sh`：PlistBuddy 修改 `Info.plist` 后破坏 ad-hoc 签名 seal，新增 `codesign --force --sign -` 重签，避免 arm64 下载后 `killed: 9`。

## [v0.1.2] — 退出死锁修复

- 首个带版本号的发布基线（退出死锁修复）。

[v0.1.2]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.1.2
[v0.2.0]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.2.0
[v0.3.5]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.3.5
[v0.3.6]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.3.6
[v0.3.7]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.3.7
[v0.4.0]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.4.0
