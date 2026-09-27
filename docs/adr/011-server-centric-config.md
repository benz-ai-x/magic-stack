# ADR-011: 服务器中心配置模型（Server → Service → Instance）

日期：2026-09-25
状态：已接受（v2 schema，随 v0.13.0 落地）

## 背景

v1 配置以 `tunnels[]` 为中心：每条"隧道"把 SSH 连接参数、端口转发行、NFS 节捆在一个扁平对象里。这带来三个问题：

1. 同一台远程服务器配两次 = 两条孤立"隧道"，连接参数重复维护；
2. 设置窗被模型塑形：「隧道」页编辑连接+转发、「远程挂载」页复用同一列表编辑 NFS——用户被迫按"配隧道"思考；
3. 新服务类型（OpenVPN 等）没有挂靠点——只能再开一个复用隧道列表的页面。

## 决策

配置 schema 换轴为**服务器中心**的三层模型：

```
服务器（servers[]：id + name + ssh{连接参数}）
 └─ 服务（services.{ssh, nfs, …}：这台机器能提供什么）
     └─ 实例（forwards[] / mounts[]：每个服务的具体用途行）
```

六项拍板（用户决策）：

1. **实例 = 用途行**：每条 -L 转发 = 转发实例、-D 代理 = 唯一代理实例、每个挂载 = NFS 实例。**运行时会话模型不变**（一服务器一 SSH 会话承载其全部转发）。
2. **自动迁移 + 保 id**：v1 `tunnels[]` → v2 `servers[]` 一次性迁移；稳定 id（`t-<sha1>@host:port`）逐字节保留——Keychain 槽位（`tunnel:{id}`）与运行时会话身份随之保值。新增 `schema_version` 显式版本化。
3. **代理角色单一真相**：`proxy_server_id` 取代 v1 的 `current_tunnel_id` + `current_tunnel`（下标投影）双表示。
4. **菜单保持功能分组**（操作面与配置面分离：菜单按用户想做的事分组，设置窗才是服务器中心的配置面）。
5. **设置窗「服务器」单视图吞并 隧道+远程挂载 两页**（后续批次）。
6. **服务卡 + 一键检测**：每服务一张卡（配置态徽标 + 手动「检测服务」探活——SSH 复用 probe、NFS 复用 check_remote、OpenVPN 新探针）；OpenVPN 本轮仅作枚举占位。

## 后果

- 服务器形状知识的单一归宿在 `mpconf.config`（归一化访问器 servers/server_by_id/proxy_server/server_forwards/server_nfs）；tunnel/mount 域按分层 DAG 不 import mpconf，用本地微访问器。
- `mpconf.validate.tunnel_rows_errors` → `server_rows_errors`（校验文案 隧道→服务器，镜像语料同步）。
- Keychain 账户字符串是兼容契约——`tunnel:{id}` 与 `user@host:port` 逐字节不变，只换字段读取路径（ssh 节）。
- 未保存的新服务器（无 id）不能被设为代理服务器（角色标记按钮隐藏）——id 是角色的寻址前提。

## OpenVPN 互斥模型（修订版，2026-09-27 定稿）

> 修订前共识（2026-09 评估）：VPN 与 SSH 隧道二选一互斥；**互斥粒度必须是全部 SSH 会话（-D/-L/NFS）**，否则路由坑复活。M2 实施即按此粒度落地（连接 VPN = `unmount_all + stop_all` 拆全部）。真机使用推翻该粒度假设，本节为正式决策记录。

**接入层/服务层分离（用户拍板的两条不变量）**：

1. **接入层全局唯一**：任一台服务器的 SSH 代理（-D）**或** OpenVPN，活跃接入数恒为 1；切换接入 = 停旧起新，只动接入面。
2. **服务层按服务器自治**：每台服务器 0..n 条端口映射（独立纯 -L 会话）、0..n 个 NFS 挂载（独立 NFS 会话），各自启停/重连/重挂，不随接入切换陪葬。多台服务器可同时有活跃服务会话。

**落地语义**：

- 互斥屏障收窄到接入层：连接 VPN 只停 -D 会话（`ConnectionCoordinator.stop_access`——新增面，此前 `cancel`/`stop_all` 均为全停）+ 系统代理收敛；**不**再 `unmount_all`（它会清空 NFS `_desired` 导致永不自动重挂）、**不**再停转发会话。
- -D 会话不再搭载代理服务器自己的 -L（`build_tunnel_command` 代理模式结构上忽略 forwards）；代理服务器的转发改独立纯 -L 会话，与非代理服务器同路径同生命周期。
- VPN established 引发的路由翻转会断存量 TCP：VPN connected 事件触发**服务层**僵尸重建（转发会话 + NFS 会话，与唤醒事件同语义），绝不拉起 -D（接入互斥）。会话重连后走 VPN 新路由自愈；只能经 VPN 到达的服务器在 VPN 未连时如实亮红。
- 「未连接绝不拉起」守卫按会话粒度原样保留；「断开不自动回切接入」语义保留。
- 早期备忘的技术路线（openvpn 子进程 + management interface）维持不变；规划中的 mode_gate 状态机随屏障收窄不再必要——互斥判定退化为「-D 接入活跃与否」谓词（`_access_active`）。
