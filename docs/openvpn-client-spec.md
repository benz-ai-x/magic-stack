# OpenVPN 客户端 Spec（调研 + 技术方案）

> 状态：**调研定稿，未立项实施**。本文是 ADR-011「OpenVPN 备忘（占位）」的正式展开：
> 前期可行性共识（openvpn 子进程 + management interface + 模式互斥 choke point）在此
> 落成完整方案。实施立项时另写精简决策记录（ADR-013+），本文作为其依据材料。
> 调研日期：2026-09-27；调研手段：OpenVPN 源码树（management-notes.txt / manage.c）、
> openvpn-gui 与 Tunnelblick 源码、Homebrew formula、GPL 合规先例（见文末来源）。

---

## 1. 背景与目标

### 1.1 产品定位

Magic Stack 的服务器中心模型（ADR-011）里，用户的核心资产是「我的服务器」。当前接入
远端网络的唯一模式是 **SSH 隧道**（本地 SOCKS/HTTP 代理 + 端口转发 + NFS 挂载）——
它解决「指定流量走远端」，但对不支持代理的应用、以及「把整台 Mac 网络层接进远端网段」
的场景无能为力。

OpenVPN 客户端是第二种接入模式：**网络层接入**（utun 接口 + 路由），与 SSH 隧道模式
二选一互斥（见 §7）。服务器侧运行 OpenVPN server（用户自建，`services/server_check.py`
的 `probe_openvpn` 已能探测安装态）。

### 1.2 目标

- M1：单服务器 VPN 连接的完整生命周期——导入 .ovpn、连接/断开、状态与错误可见。
- M2：与 SSH 的模式互斥（接入层切换；ADR-011 修订后为接入层互斥/服务层自治），设置窗 OpenVPN tab 转正。
- M3：DNS 应用与恢复、崩溃清理、流量统计、错误分类中文提示。

### 1.3 非目标（本期不做）

- 不捆绑 openvpn 二进制进 .app（GPL 与体积考量，走「用户自装 + 引导」——tomlkit 先例）。
- 不做 NetworkExtension / openvpn3 库嵌入（构建复杂度数量级上升，见 §2.1）。
- 不做多 VPN 并发（全局至多一条活跃 VPN 连接，是本 spec 的结构不变量，见 §5.3）。
- 不做远端 OpenVPN server 一键安装（远期可镜像 `mount/remote_setup.py` 模式）。
- Docker 形态不含 VPN 域（utun 是宿主概念）；该形态下 UI 相关入口静默隐藏。

---

## 2. 调研结论摘要（决策依据）

### 2.1 技术路线选型

| 路线 | 结论 |
|---|---|
| **A. 包 openvpn 2.x 社区版二进制 + management interface** | **采用**。Tunnelblick、openvpn-gui、Viscosity（闭源商业）全部走这条路：零编译、协议是几页行文本规约、特权与 UI 天然分离（root 子进程 + 非特权 GUI 经本地 socket 控制） |
| B. 嵌入 openvpn3 C++ 库 | 否决。无官方维护的 Python binding（Debian 的 python3-openvpn3 是 Linux-only D-Bus 客户端）；自己桥接 + 自实现 utun/路由层，构建复杂度数量级上升 |
| C. NetworkExtension（NEPacketTunnelProvider） | 否决。需 Swift/ObjC system extension + Apple entitlement；连官方 OpenVPN Connect for macOS 都不用 NE（用 root helper + utun） |
| D. PyPI 现成封装库 | 否决。`openvpn-api`（2020 停维）、`pyopenvpn`（玩具级）均弃维多年；协议极简，自砧行协议客户端 ~200 行是业界常态 |

基线版本：**Homebrew openvpn 2.7.7**（management 协议版本 6；2.5=3、2.6=5）。2.6+ 的
`data-ciphers` 默认值已含 ChaCha20-Poly1305，`tls-version-min` 默认 1.2。

### 2.2 management interface 关键事实

（完整命令/事件表见 §6.2 附录；此处只列影响架构的）

- 启用：`--management 127.0.0.1 <port> <pw-file>`（TCP 形态，Tunnelblick/openvpn-gui 均
  此配置）或 `--management <path> unix`。TCP 形态官方标注 INSECURE，必须 127.0.0.1 +
  密码文件；密码比较常量时间。
- **`--management-hold` + `hold release`**：openvpn 启动后先冬眠，等 app 连上管理口、
  UI 就绪再放行——彻底消除「子进程先跑、GUI 后 attach 丢事件」竞态。注意 hold 旗标跨
  重启持久：收到 `>HOLD` 时先 `hold off` 再 `hold release`（openvpn-gui 做法）。
- **凭证注入**：`--management-query-passwords` + `--auth-retry interact`（默认 none 会让
  认证失败直接致命退出）。凭证经 `username "Auth" <u>` / `password "Auth" <p>` 下发，
  **永不落盘**；参数遵循 openvpn 配置文件词法（`\\`、`\"`、`\空格` 转义）。
- **状态机**：`CONNECTING → RESOLVE → TCP_CONNECT → WAIT → AUTH → [AUTH_PENDING] →
  GET_CONFIG → ASSIGN_IP → ADD_ROUTES → CONNECTED`；异常 `RECONNECTING`（原因在描述
  字段）、`EXITING`。行尾允许空段拖尾逗号——解析按 `split(',')` 容忍空段，禁止按字段数判断。
- **流量**：`bytecount n`（整秒粒度）→ `>BYTECOUNT:<in>,<out>`，自连接起累计值，速率
  自行差分（Tunnelblick：环形缓冲滑动平均 + 跨重连基数归零）。
- **断开语义**：`signal SIGTERM` → `>STATE:EXITING` → **等进程真正退出才算断开完成**
  （EXITING 只是「退出进行中」）；退出码非 0 且伴随 `>FATAL:` 行即致命错误。
- **单客户端模型**：管理口同时只服务一个客户端，新连接顶掉旧连接——app 内部保证唯一
  连接，重建前先关旧 socket。
- 管理通道本身明文无认证（除 pw-file）——任何能连上的本地进程可完全控制 openvpn。

### 2.3 macOS 特权与清理事实

- **root 是硬需求**：开 utun（kernel control socket）、`ifconfig` 配地址、`route add`
  都要 root；非 root 报 `Permission denied (errno=13)`。Linux 式 `--user` 降权在
  macOS 不实用（重连/改路由仍需特权）。openvpn 2.4+ 原生 utun（`dev tun` 自动尝试）。
- **方案对比**：sudo + sudoers.d 限定条目（本仓库 NFS 先例，v1 采用）＜ SMAppService
  privileged helper（macOS 13+，v2 加固方向）＜ root LaunchDaemon（Tunnelblick
  tunnelblickd 现役架构）。setuid wrapper 业界证伪（Tunnelblick 2012 本地 root 漏洞）。
- **崩溃清理**：SIGTERM 优雅退出会跑 down 脚本、删路由、关 tun；kill -9 后 utun 与
  路由由内核兜底回收；**真正会残留的是 DNS 等系统设置**（本来就不是 openvpn 二进制
  改的，是 up/down 脚本改的）——需要「快照 + 恢复 + 启动 reconcile」。
- **DNS**：openvpn 在 macOS 不应用 push 的 `dhcp-option`——push 选项以 `foreign_option_N`
  环境变量交给 up 脚本，必须 `script-security 2` + root up/down 脚本经
  `networksetup`/`scutil` 应用与还原，否则 DNS 泄漏。

### 2.4 GPL 合规

- 仓库 MIT 协议。**调用用户自装的 openvpn 二进制（独立子进程 + socket 通信）= mere
  aggregation，零 GPL 义务**（FSF 立场：独立进程是独立程序）。
- 若将来捆绑二进制分发：义务为随附 GPLv2 文本 + 源码 written offer（Viscosity 模式）；
  openvpn 的 openssl-exception 已扫清链接障碍。本期不捆绑。

### 2.5 业界先例印证

- **Tunnelblick**（macOS 最成熟开源实现）：root 守护拉起 openvpn（`--management
  127.0.0.1 <随机端口>` + 随机口令），GUI 全部交互走管理口；日志双通道（文件全量 +
  管理口事件）；up/down 脚本管 DNS；「up 建标志文件、EXITING 时还在 = 崩溃，GUI 补跑
  down 清理」；连后外网 IP 复核。
- **openvpn-gui**（Windows 官方）：命令行固定注入 `--auth-retry interact
  --management-query-passwords --management-hold`；attach 后 `version 4` → `hold off` +
  `hold release` → `state on all`（原子：历史回放 + 实时订阅）→ `log on all` →
  `bytecount 5` → `state`；管理口令经 `stdin` 不落盘。
- 版本闸门：`version 4` 起管理命令才有响应（≤3 静默）；宣告 4 顺带解锁 2.7 的用户名
  单问等能力。

---

## 3. 总体架构

### 3.1 进程与线程模型

```
Magic Stack（用户态）
 ├─ VpnClient（SubprocessMonitor 子类，第三类子进程消费者）
 │   ├─ sudo -n <openvpn argv>          ← 前台子进程，退出码经 sudo 透传
 │   └─ mgmt reader daemon 线程          ← 管理口 socket 行读取 + 事件分发
 ├─ 状态机投影 → RuntimeProjection.vpn   → /api/state 装饰 + 菜单
 └─ （既有）ConnectionCoordinator / MountCoordinator 不动

root 态（经 sudoers 免密执行，argv 全量钉死）
 └─ openvpn --config <固定路径>.conf --management 127.0.0.1 <固定端口> <固定 pw-file>
     └─ up/down 脚本（app 预装，root 执行）：DNS 快照/应用/恢复
```

线程纪律沿用现状：主线程 rumps NSRunLoop；VpnClient 的 mgmt reader 是 daemon 线程；
慢操作（起停 join、profile 授权写入）经 `services/intents.py` 的 daemon 线程纪律后台跑。

### 3.2 模块落位（单一归属 + 分层 DAG）

```
vpn/ ── 新域：OpenVPN 客户端
  profile.py ── .ovpn 解析/校验/净化：inline 引用核对、remote/auth 提取、
    script 类指令剥除（安全模型 §5.2 的单一归宿）；「需要凭证」布尔推导
  profile_store.py ── profile 落盘存取（profile_set 布尔的写者）：PATHS 注册目录 +
    0600 原子写（config_store 管线）；解析/净化归 profile.py，布尔翻转归保存流
  mgmt_client.py ── 管理口行协议客户端（纯 Python 零依赖可单测）：连接/密码握手/
    version 4 宣告/命令发送-响应匹配/实时事件行解析（>STATE/>LOG/>BYTECOUNT/
    >PASSWORD/>HOLD/>FATAL）/转义规则单一归宿
  openvpn_client.py ── VpnClient（SubprocessMonitor 子类）：argv 构建（固定模板）、
    hold/release 时序、STATE→UI 状态映射、凭证事件回调（Keychain 取用经注入）、
    bytecount 差分速率、退出码收尾
  dns_scripts.py ── up/down sh 脚本的生成与安装（内容固定模板 + 快照文件路径约定）；
    崩溃 reconcile 判据（标志文件存在 = down 未跑）单一归宿

shared/
  defaults.py ── +VPN_MANAGEMENT_PORT（跨域默认值既有归宿；固定端口登记）

services/
  server_check.py ── probe_openvpn 已有（远端安装态），不动
  intents.py ── +vpn_connect / vpn_disconnect 意图（互斥检查 = 接入层谓词
    _access_active；线程纪律、通知、dirty 沿用独占）
  （原规划的 mode_gate.py 状态机不落地——ADR-011 修订后互斥退化为接入层
    谓词 + stop_access 单点，无独立状态机必要）

mpconf/
  config.py ── services.openvpn 读路径归一 normalize_openvpn + 访问器
    server_openvpn（镜像 normalize_nfs/server_nfs 模式）
  validate.py ── +openvpn 分域校验（profile_set 引用一致、布尔形状）

shellui/
  menu_builder.py ── +「VPN 网络」组（A 类运行物语法：动词标题 + 状态点四值 +
    行尾状态词）
  config_ui.html ── OpenVPN tab 转正（profile 导入/查看、凭证、连接操作、状态展示）
```

依赖方向合规：`vpn/` 只向下依赖 `shared/`；`services/mode_gate.py` 与 `intents.py`
在 `vpn/` 与 `tunnel/` 之上协调；菜单/UI 只做翻译。`vpn/` 不 import `tunnel/`（互斥
经 services 层，不搞跨域白名单）。

### 3.3 关键时序

**连接**（intents.vpn_connect → app._vpn_do_connect → VpnClient.start）：

```
0. 接入层切换（ADR-011 修订）：ConnectionCoordinator.stop_access()——只停 -D
   会话 + 系统代理收敛；转发/NFS 会话不碰（§6）
1. 写 runtime conf（净化后 profile + 注入指令）→ root 目录落盘（首次/变更需管理员授权）
2. 随机生成管理密码 → pw-file（固定路径，0600，内容每次可变）
3. sudo -n openvpn --config … --management 127.0.0.1 <固定端口> <pw-file>
   --management-query-passwords --management-hold --auth-retry interact
   --script-security 2 --up <固定up.sh> --down <固定down.sh> --verb 3
4. app 连管理口 → 密码握手 → version 4 → hold off + hold release
   → state on all → log on all → bytecount 1
5. >PASSWORD:Need 'Auth' … → Keychain 取密码 → username/password 下发（转义）
6. >STATE:…,CONNECTED,SUCCESS,<tun_ip>,… → 状态机置「已连接」
```

**断开**：mgmt `signal SIGTERM` → 等 `>STATE:EXITING` → **等子进程退出**（退出码收尾；
openvpn 自跑 down 脚本还原 DNS）→ 超时（5s）fallback `process.terminate()`（sudo 转发
SIGTERM）→ 仍不退 SIGKILL（接受孤儿风险，靠 §5.4 收养清理兜底）。

**崩溃**（app 被杀 / openvpn 被杀）：
- openvpn 死、app 活：SubprocessMonitor.check() 现有崩溃检测路径收尾 + dns_scripts
  标志文件判据触发补清理（恢复 DNS 快照）。
- app 死、openvpn 活（root 孤儿）：下次启动 reconcile——连固定管理口（密码是 Keychain
  里的稳定值，见 §5.3）发 `signal SIGTERM` 收尸；socket 不通且标志文件在 → 只补 DNS 恢复。

---

## 4. 配置模型（mpconf schema 扩展）

`~/.magic-proxy.json` 的 `servers[].services.openvpn`（与 `services.nfs` 同形——单实例
服务，enabled + 参数，非 1:n 实例）：

```jsonc
"openvpn": {
  "enabled": false,        // 服务开关（配置态）
  "profile_set": false,    // 掩码布尔契约（ADR-002）：profile 文件存在且非空
  "auth": "none",          // none | userpass（profile.py 从 .ovpn 推导，UI 可改）
  "username": "",          // userpass 时明文存 config（同 ssh user 先例）
  "password_set": false,   // 掩码布尔；密码在 Keychain 槽 vpn:{server_id}
  "pull_dns": true,        // false = 注入 pull-filter ignore "dhcp-option"（DNS 与
                           //   域名搜索域一并拒收——与开关名语义一致）
  "autostart": false       // 启动即连（经 mode gate 互斥检查）
}
```

- **profile 正文不进 config JSON**：inline `.ovpn` 内嵌用户私钥，属机密。存独立文件
  `~/.magic-stack/openvpn/{server_id}.ovpn`（0600，config_store 原子写管线 + PATHS
  注册表新增）。config 只存 `profile_set` 布尔 + 提炼元数据（remote host 等运行时从
  profile 现读）。
- 稳定 id 沿用 `shared/identity.stable_id("p…")` 体系外的服务器 id（`s…`/服务器形
  访问器既有约定），Keychain 槽 `vpn:{server_id}` 镜像 `tunnel:{id}` / `nfs-sudo:`
  先例。
- 迁移：schema v2 无 openvpn 键 → `normalize_openvpn` 归一默认值（读路径归一，同 nfs）。

---

## 5. 特权与安全模型

### 5.1 sudoers 条目（v1 提权方式，复用 mount_control 骨架）

新规则文件 `/etc/sudoers.d/magic-stack-openvpn`（独立于 NFS 的规则文件）。条目形态
（实施裁决）：**全局单条目 + 固定 `client.conf`**——与 §5.3「全局至多一条活跃 VPN 连接」
不变量自洽，切换服务器 = 重装 conf（重走一次管理员授权，低频可接受）；argv 全量精确
匹配、零通配：

```
<user> ALL=(root) NOPASSWD: /opt/homebrew/opt/openvpn/sbin/openvpn --config /Library/MagicStack/openvpn/client.conf --management 127.0.0.1 <VPN_MANAGEMENT_PORT> /Library/MagicStack/openvpn/mgmt.pw --management-query-passwords --management-hold --management-forget-disconnect --auth-retry interact --script-security 2 --up /Library/MagicStack/openvpn/dns-up.sh --down /Library/MagicStack/openvpn/dns-down.sh --verb 3, /bin/sh /Library/MagicStack/openvpn/dns-down.sh
```

> 目录用 `/Library/MagicStack/openvpn`（原案 "Application Support"）——sudoers 命令
> 匹配按空白分词，路径含空格要引号转义整类脆断；无空格根上消除。末尾逗号清单第二项
> 是崩溃 reconcile 补跑 dns-down 的授权（§5.4，实施裁决取并入形态）。

与 NFS 条目的**本质差异必须遵守**：`up` 指令使「config 内容可控 = root 任意执行」，
所以——

1. **参数零通配**（sudoers glob 可匹配空格 = 注入面；本条目连端口号都固定——
   端口是 `shared/defaults.VPN_MANAGEMENT_PORT` 安全不变量，不参数化）。
2. **runtime conf 目录 root-owned、用户不可写**（`/Library/MagicStack/openvpn/`，
   安装 sudoers 的同一次管理员授权里创建并 chown root）。profile
   变更重新生成 conf 时再次走 osascript 授权（低频操作，UX 同 NFS「一键安装」）。
3. **profile 净化是第二道闸**：`vpn/profile.py` 剥除一切脚本/执行类指令（`up`、`down`、
   `script-security`、`iproute`、`route-up`、`down-pre`、`tls-verify` 等），导入时对被
   剥除项给用户告警确认。即使 conf 被篡改，可执行面也被锁死在 app 自有的两个固定脚本。
4. 可选加固（实施时评估）：sudoers digest 锁定 openvpn 二进制 SHA-2。

### 5.2 管理口安全

- TCP `127.0.0.1:<VPN_MANAGEMENT_PORT>`（固定，登记 `shared/defaults.py`；端口占用经
  `sysctl/port_check.py` 启动期报告）。固定端口是 sudoers 钉死 argv 的直接推论，也换来
  §5.4 的崩溃收养能力。
- **管理密码稳定存 Keychain**（槽 `vpn-mgmt:`，安装时随机生成）而非每次随机：app 崩溃
  重启后仍能重新接管残留的 root openvpn。pw-file 固定路径（root 可读），内容 = Keychain
  值，0600。
- 备选形态 `--management <path> unix`（官方无警告）因 root 进程建 socket 的连接权限
  问题 + 业界先例（Tunnelblick/openvpn-gui 均 TCP）而排后；若实施期验证可行可切换，
  协议面（mgmt_client.py）不变。

### 5.3 全局不变量：至多一条活跃 VPN

互斥模型（§7）本身只允许一种接入模式；进一步把「同时至多一条 VPN 连接」定为结构不变
量：固定单一管理端口、单一 runtime conf 目录、菜单单条目。多服务器 = 各自可配置，
连接时切换。

### 5.4 崩溃收养与清理

- openvpn 自身改的路由/tun：SIGTERM 优雅退出自清；kill -9 后内核回收（残留 utun 无害）。
- DNS（up 脚本改的）：`dns-up.sh` 快照落 `/Library/MagicStack/openvpn/dns-backup.txt`
  （三行平文本：服务名 / DNS / 搜索域——sh 侧免 JSON 解析）+ 建标志文件；`dns-down.sh`
  恢复 + 删标志。app 每次启动与 VPN 断开后 reconcile：标志文件在 = down 没跑过 →
  经 sudoers 补跑。**实施裁决**：补跑授权并入 openvpn 条目的逗号清单
  （`/bin/sh <dns-down.sh>`，脚本本体 root-owned 用户不可写），不另开独立行。
- root 孤儿 openvpn：固定端口 + Keychain 稳定密码 → 启动时连管理口 `signal SIGTERM`
  收尸（Tunnelblick 用「端口编码在日志文件名里」解决同一问题，我们的固定端口方案更简）。

### 5.5 凭证

- VPN 用户名密码：Keychain `vpn:{server_id}`，经管理口 `username/password` 下发，
  `--management-forget-disconnect` 防残留；永不落盘（不走 `auth-user-pass` 文件）。
- 私钥口令（加密私钥的 .ovpn）：同一槽或独立槽（实施时定），`password "Private Key"`。
- `auth-retry interact` 使凭据错误变成「重问」而非致命退出；动态挑战（CRV1）/静态挑战
  （SC）协议面已核实（附录 §6.2），**v1 只做密码错误重问**，挑战类给出「服务器要求
  附加验证，暂不支持」的可读提示。

---

## 6. 模式互斥（ADR-011 修订版：接入层互斥 / 服务层自治，2026-09-27 定稿）

### 6.1 模型

> 原始模型（M2 实施）：两模式 SSH（-D + 转发 + NFS 全家桶为整体）vs VPN，粒度必须是
> 全部 SSH 会话。真机使用推翻——A 服务器做 VPN、B 服务器做转发/挂载的拓扑下，连 VPN
> 不该陪葬 B 的会话。ADR-011 修订记录见 `docs/adr/011-server-centric-config.md`。

- **接入层全局唯一**：任一台的 -D 代理或 OpenVPN，活跃接入数恒为 1；**服务层按服务器
  自治**：各服务器的 -L 转发会话与 NFS 会话独立运行，不随接入切换陪葬。
- 互斥屏障收窄到接入层（内联在 `_vpn_do_connect_locked`，规划中的 `mode_gate.py`
  状态机随屏障收窄不再必要——互斥判定退化为「-D 接入活跃」谓词）：

```
switch_to_vpn():
  1. 接入层切换：ConnectionCoordinator.stop_access()——只停 -D 会话
     （含重试/host-key/本地代理运行时），转发会话与 NFS 会话不碰；
     系统代理收敛（别指着死掉的 :8888）
  2. → VpnClient.start（§3.3 时序）
  3. VPN connected → 服务层僵尸重建（转发 + NFS 会话 reconnect_now，
     与唤醒事件同语义；绝不拉起 -D）——路由翻转断掉的存量 TCP 自愈
switch_to_ssh(): 对称（VPN 断开 → conn.start()；服务会话自管，无需恢复面）
vpn_stopped(): VPN 断开后不自动回切接入（ADR-011 语义保留）
```

- 只能经 VPN 到达的服务器，在 VPN 未连时其转发行如实亮红（无限退避重试照常）。
- 「未连接绝不拉起」守卫按会话粒度原样保留；-D 便车退役：代理服务器自己的转发改
  独立纯 -L 会话（`build_tunnel_command` 代理模式结构上忽略 forwards）。
- intents 层互斥判定：`_access_active`（仅 -D 会话 connecting/connected）——转发/
  挂载在跑不拦 VPN；确认文案口径「断开 SSH 代理接入，端口映射与挂载不受影响」。

### 6.2 附录：管理口命令/事件速查（实施时 mgmt_client.py 的对照面）

初始化（attach 后依序）：`version 4` → `hold off` + `hold release` → `state on all`
→ `log on all` → `bytecount 1`。

| 命令（app→openvpn） | 用途 |
|---|---|
| `signal SIGTERM` / `SIGHUP` / `SIGUSR1` | 优雅退出 / 硬重启（重读配置）/ 软重启（不重读） |
| `username "Auth" <u>` / `password "Auth" <p>` / `password "Private Key" <p>` | 凭证下发（词法转义） |
| `state on all` / `log on all` / `bytecount n` | 状态/日志（历史+实时原子订阅）/ 流量 |
| `hold on/off/release` | 挂起控制（release 不改旗标，旗标跨重启持久） |
| `forget-passwords` / `auth-retry interact` | 丢凭证 / 运行时改重试策略 |

| 事件（openvpn→app） | 处理 |
|---|---|
| `>STATE:<ts>,<name>,<desc>,<tun_ip>,…` | 状态机主输入；9 列容忍空段 |
| `>PASSWORD:Need 'Auth' …` / `Verification Failed: 'Auth'` | 凭证索取 / 凭据错（重问上限后报错） |
| `>BYTECOUNT:<in>,<out>` | 差分速率（环形缓冲滑动平均，跨重连基数归零） |
| `>LOG:<ts>,<flags>,<text>` | 错误分类输入（F/N/W 标志 + VERIFY ERROR / AUTH_FAILED 文本模式） |
| `>FATAL:<text>` | 致命错误（进程将退） |
| `>HOLD:Waiting for hold release:<n>` | hold 态（先 off 再 release） |

错误分类（>LOG/>FATAL 文本 → 中文提示，`vpn/openvpn_client.py` 分类表，镜像
`_classify_mount_failure` / ssh_launch 的失败分类先例）：凭据错 / 服务器证书过期
（`VERIFY ERROR.*certificate has expired`）/ 加密协商失败（`AUTH_FAILED,Data channel
cipher negotiation failed`——注意它披着 AUTH_FAILED 皮，不是密码错）/ TLS 握手失败
（多为网络/防火墙）/ 服务器不可达（connect 超时退避）/ 服务器主动断开（`>NOTIFY` …
`remote-exit`）。

---

## 7. UX 设计

### 7.1 菜单（A 类运行物语法）

新增组「VPN 网络」（位置：远程挂载之后、AI 路由之前；命名实施时随 i18n 键定稿）：

- 标题动词：`连接 <服务器名>` / `断开 <服务器名>`；状态点四值：绿 = CONNECTED /
  黄 = connecting·reconnecting / 黑 = 未启动 / 红 = error（EXITING 非零退出码、致命
  日志）。行尾状态词走 `shared/locales` 状态词表（键名引用，非字面量）。
- 组标题异常 rollup 沿用（组内 error 才挂 ⚠ n）。
- SSH 活跃时点「连接」→ 互斥确认对话框（经 intents 确认回调）。

### 7.2 设置窗

OpenVPN tab 转正（去 `soon` 徽标）：profile 导入（文件选择 + 净化告警确认）、
用户名/密码（掩码布尔 + Keychain）、pull_dns 开关、autostart 开关、「检测服务」
（已有 probe 复用）+ 运行态展示（5s 定向轮询 `mergeRuntimeDecorations` 模式，同
NFS 错误可见性链路）。保存流走 config_server 既有 PUT 管线；runtime conf 生成与
sudoers 安装是保存后的显式「安装到系统」动作（管理员授权弹窗），不混进静默保存。

### 7.3 运行态投影

`RuntimeProjection` 新增 `vpn` 字段（`VpnState`：status / tun_ip / 错误 / 速率），
`/api/state` 装饰 `vpn_state` 进 `RUNTIME_DECORATED_FIELDS`——按既有「生产者加字段 +
装饰处加键」纪律，不加构造参数。

---

## 8. 依赖探测与安装引导

- 二进制探测链（镜像 `capture/resources.py`）：env `MAGIC_STACK_OPENVPN` → Homebrew
  双 prefix 常量 `/opt/homebrew`（ARM）/ `/usr/local`（Intel）下的
  `opt/openvpn/sbin/openvpn`（**brew 装在 sbin，默认 PATH 不含**，`which` 探不到——
  已核实的坑）→ MacPorts `/opt/local/sbin` → PATH 兜底。
- 未安装：连接动作降级为提示 `brew install openvpn`（tomlkit 缺席提示安装的既有
  模式，ADR-010 M4）；设置窗 OpenVPN 卡给安装引导文案。
- 最低版本：2.6（tls-version-min/data-ciphers 默认现代值；管理协议 ≥5）。2.7.7 为
  当前 brew stable，`version 4` 宣告兼容 2.5–2.7。

---

## 9. 里程碑与验收

### M1 — 客户端核心（不含互斥）

交付：`vpn/` 域四模块、配置模型、sudoers 引导 + runtime conf 安装流、连接/断开/状态/
日志窗接入、凭证 Keychain + 管理口注入。
验收：
- 导入含 inline 证书的 .ovpn → 净化告警正确列出剥除项 → 连接成功（绿点 + tun IP）。
- 断开走 SIGTERM 优雅路径，down 脚本跑完，DNS 恢复。
- 凭据错误触发重问；三次失败给出中文分类提示。
- app 强杀后重启：收养/清理 root 孤儿（固定端口 + Keychain 密码路径）。
- `python3 -m pytest` 新增测试全绿（§10）；菜单/设置窗双语无汉字字面量残留。

### M2 — 模式互斥 + UI 转正

> M2 按「全量屏障」实施并真机验收通过；下列验收项中的屏障粒度已被 ADR-011 修订
> （2026-09-27）取代为接入层互斥/服务层自治——历史记录保留，现行语义见 §6。

交付：intents 两意图、菜单组、设置窗 tab、RuntimeProjection。
验收：
- SSH 全活跃（代理 + 2 转发 + NFS 挂载）时连接 VPN：确认 → ~~屏障逐项停净（含卸载）~~
  只停 -D 接入（转发/挂载原地不动，ADR-011 修订）→ VPN 起；反向对称。
- VPN 断开后不自动回切接入；autostart 的 guarded 语义测试钉死。
- 互斥期间菜单动词/状态词随接入态正确推导（真值表测试）。

### M3 — 加固

交付：DNS 崩溃 reconcile 全链路、错误分类全表、bytecount 速率展示、`status 3` 对账。
验收：
- kill -9 openvpn → DNS 恢复（标志文件路径）。
- 错误分类表对真实日志语料（VERIFY ERROR / cipher 协商 / TLS 握手）逐条命中。
- 菜单/设置窗显示上下行速率，跨重连累计单调。

### v2（远期，实施时另立 ADR）

- SMAppService privileged helper 取代 sudoers（消除 NOPASSWD 面；helper 接管 conf
  写入 + DNS 脚本 + 快照重放 = tunnelblickd 等价物）。
- DNS 升级 scutil State 键（per-service、接口消失自动失效）+ DNS 泄漏自检。
- 捆绑 openvpn 二进制（GPLv2 文本 + written offer，Viscosity 模式）。
- 远端 OpenVPN server 一键安装（镜像 `mount/remote_setup.py`：easy-rsa + systemd）。

---

## 10. 测试策略

- `vpn/mgmt_client.py`：行协议纯 Python 单测（fake socket 喂语料：STATE 尾逗号空段、
  转义、密码握手、单客户端顶替、EXITING 后 EOF）。
- `vpn/profile.py`：净化真值表（脚本指令剥除、inline 引用核对、auth 推导）。
- 状态机映射：`>STATE`/`>PASSWORD`/`>LOG` 语料 → VpnState 真值表（镜像
  `tests/test_intents.py` 打公开意图面的模式）。
- sudoers 生成与净化输出：argv 全量钉死断言（零通配）、conf 内容无脚本指令断言。
- 校验镜像：openvpn 表单校验双侧同错同净；单侧规则去 `tests/test_validation_mirror.py`
  白名单挂号。
- i18n：新文案键位奇偶/en 全译/字面量五道闸自动覆盖。
- SIT（实施时手跑清单）：真机 brew openvpn 2.7.7 + 测试 server 的连接/断开/崩溃矩阵。

---

## 11. 风险清单

| 风险 | 缓解 |
|---|---|
| sudoers NOPASSWD 面比 NFS 大（conf = 潜在 root 执行） | argv 零通配钉死 + conf root-owned + profile 净化双闸（§5.1）；v2 换 helper |
| 管理口明文 | 127.0.0.1 固定端口 + Keychain 稳定密码 + 0600 pw-file（§5.2） |
| DNS 残留（崩溃后 down 未跑） | 快照文件 + 标志文件判据 + 启动 reconcile（§5.4） |
| SIGKILL 孤儿 root openvpn | 固定端口 + 稳定密码收养路径（§5.4）；SIGKILL 只作最后手段 |
| openvpn 内层无限重试 × 外层重启 = 风暴 | **不做外层自动重启**（用户停止/崩溃绝不拉起纪律）；不可达交给内层退避，UI 显示重连中 |
| hold 语义踩坑（忘 release / 旗标持久） | 时序钉死在 VpnClient（hold off + release），语料单测 |
| 凭据错默认致命退出 | `--auth-retry interact` 写进 argv 模板 |
| EXITING 误当已断开 | 状态机显式等进程退出 + 退出码（§3.3） |
| 版本碎片（2.4–2.7） | `version 4` 宣告；STATE 解析容忍空段；2.6 为最低支持线 |
| brew sbin 不在 PATH | 探测链双 prefix（ARM/Intel）+ PATH 兜底（§8） |

---

## 12. 开放问题（实施立项时裁决）

1. 菜单组名与图标（「VPN 网络」vs 并入「代理」组做模式切换器）——随 M2 的 UX 打磨定。
2. 私钥口令的 Keychain 槽位归属（独立槽 vs 并入 `vpn:{id}`）。
3. `pull_dns=false` 之外是否暴露更细的 split-tune（`route-nopull` + 手动 route 列表）。
4. 是否在「检测服务」卡里加本地 openvpn 安装态（当前卡只测远端 server）。

---

## 13. 调研来源

- 管理接口权威规约（随源码分发）：openvpn 源码树 `doc/management-notes.txt`（master/2.7；
  2.5/2.6 分支 diff 核对）· man 分节 `doc/man-sections/{management-options,client-options,
  signals,cipher-negotiation,tls-options,protocol-options,vpn-network-options,generic-options,
  inline-files}.rst` · 源码 `src/openvpn/manage.c`（manage.h MANAGEMENT_VERSION：2.5=3 /
  2.6=5 / 2.7=6）
- 先例实现：Tunnelblick（GitHub Tunnelblick/Tunnelblick：VPNConnection.m 管理口时序、
  client.up.tunnelblick.sh DNS 管线、tunnelblickd 恢复文件机制、issue #640 命令行实证）·
  Windows openvpn-gui（GitHub openvpn/openvpn-gui：openvpn.c/manage.c 初始化序列与
  凭证/断开语义）
- macOS 特权：openvpn 手册（utun 自动选择、--user 降权限制）· Openwall 2012 Tunnelblick
  setuid 漏洞公告 · SMAppService 文档 · sudoers 手册（参数 glob/digest）
- 分发与合规：Homebrew formula openvpn（2.7.7，GPL-2.0-only WITH openvpn-openssl-exception，
  依赖 openssl@3/lzo/lz4/pkcs11-helper，装于 sbin）· GNU GPL FAQ（mere aggregation）·
  Viscosity GPL written offer 模式（SparkLabs）
- openvpn3：GitHub OpenVPN/openvpn3（无官方 Python binding；openvpn3-linux D-Bus 为
  Linux-only）· PyPI openvpn-api / pyopenvpn（弃维）
