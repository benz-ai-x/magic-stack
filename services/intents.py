"""UserIntents — 用户意图单一归宿（架构评审 R5 候选 1）。

菜单回调（rumps，主线程）与设置窗桥接（``_bridge_action``，字符串
action）此前是同一批用户意图的两套手写 adapter，线程纪律各走各的
（菜单主线程同步 / 桥接 daemon Thread）、guard 分派与通知文案双份。
本模块独占意图的**执行纪律**：

- guard 分派——「未连接绝不拉起」的守卫入口选择（谓词本体归
  ConnectionCoordinator）；
- 线程纪律——慢操作（重连子进程 join ~10s、host-key 首连）一律
  daemon 线程后台跑，菜单点击即返回；快操作同步；
- 通知文案与 dirty 标记——副作用经注入的 ``notify`` / ``mark_dirty``
  回调，不含 rumps/AppKit 依赖。

依赖全部注入（协调器 / 事务写径 / UI 回调）——纯 Python 可构造，
测试直打公开意图面。两个 adapter 只做翻译：菜单从菜单状态推导意图，
桥接从显式 action 字符串映射意图。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading

from mpconf import config as _mpconf
from shared import i18n
from shared.server_shape import forward_enabled

logger = logging.getLogger("magic-proxy.intents")


def _spawn_daemon(target, name):
    """默认执行器：daemon 线程（菜单/桥接共用——慢操作不卡主线程）。"""
    threading.Thread(target=target, name=name, daemon=True).start()


class UserIntents:
    """菜单与设置窗共用的用户意图面（构造期全注入，无 UI 依赖）。"""

    def __init__(self, *, conn, mounts, notify, mark_dirty, update_mp,
                 reload_config, spawn=None,
                 capture_ctrl=None, get_capture_dir=None, alert=None,
                 hold_copy_latch=None, get_agent_instructions=None,
                 vpn_connect=None, vpn_disconnect=None, vpn_active=None,
                 on_access_stopped=None):
        self._conn = conn
        self._mounts = mounts
        self._notify = notify
        self._mark_dirty = mark_dirty
        self._update_mp = update_mp
        self._reload_config = reload_config
        self._spawn = spawn or _spawn_daemon
        # 可选能力面（菜单/桥接各自用到才注入；缺席即 AttributeError
        # 早失败，不静默降级）
        self._capture_ctrl = capture_ctrl
        self._get_capture_dir = get_capture_dir
        self._alert = alert
        self._hold_copy_latch = hold_copy_latch
        self._get_agent_instructions = get_agent_instructions
        # VPN（R7-C1 起）：连接核心归 vpn/coordinator（序列/互斥/错误
        # 表单一归宿），intents 只独占线程纪律与 dirty——同 capture_ctrl 模式
        self._vpn_connect_impl = vpn_connect
        self._vpn_disconnect_impl = vpn_disconnect
        self._vpn_active = vpn_active or (lambda: False)
        # 同一个执行闸覆盖两种接入与唤醒；派线程不代表停机已经完成。
        self._access_lock = threading.RLock()
        # R8-C6：桥接停接入的后置收敛（系统代理/防睡眠聚合重算）——
        # 与菜单 stop_proxy_tunnel 同一不变式，停完即跑（此前零后置、
        # 靠每秒 tick 治愈，tick 断过一次——#118 P0）
        self._on_access_stopped = on_access_stopped

    # ── 重连 ──────────────────────────────────────────────

    def reconnect_server(self, server_id=None):
        """设置窗显式重连：按最新已保存角色选择接入或独立转发会话。

        表单/旧 DOM 只传服务器 id；保存后的自动应用与转发菜单仍走
        reconnect_proxy_or_forward，不能按代理角色把 -L 改成 -D。
        """
        if server_id == self._conn.proxy_server_id:
            self.reconnect()
        else:
            self.reconnect_proxy_or_forward(server_id)

    def reconnect_proxy_or_forward(self, tunnel_id=None, *, guarded=False):
        """重连分派：tunnel_id 指定转发会话时按该会话守卫重建（多活），
        否则代理隧道整体重连。guarded=True 是保存流自动应用的守卫
        （未连接绝不拉起）；显式重连（菜单/桥接直连）恒 guarded=False
        ——会话存在即重建（Spec-A 语义）。"""
        if tunnel_id:
            self._conn.restart_forward_async(
                tunnel_id, self._reload_config, guarded=guarded,
                thread_name="BridgeReconnectForward")
            self._mark_dirty()
            return
        if guarded and not self._conn.proxy_connected:
            logger.info(
                "端口转发自动重连跳过：隧道未连接（status=%s）",
                self._conn.ssh.status)
            return
        self.reconnect()

    def reconnect(self):
        """显式重连代理隧道（慢操作，后台跑；完成即 dirty）。"""
        self._spawn_access(self._do_reconnect, "ReconnectProxy")

    def _do_reconnect(self):
        if self._vpn_active():
            logger.info("SSH reconnect skipped: VPN access active")
            return
        self._conn.restart(self._reload_config)
        self._mark_dirty()

    def _spawn_access(self, action, name):
        """后台串行执行完整接入动作，锁不交给菜单/HTTP 调用方。"""
        def run():
            with self._access_lock:
                action()
        self._spawn(run, name)

    def switch_to_ssh(self):
        """用户确认切换后，等待 VPN 停机完成再启动 SSH 接入。"""
        def switch():
            if self._vpn_disconnect_impl is not None:
                self._vpn_disconnect_impl()
            if self._vpn_active():
                logger.warning("SSH switch skipped: VPN has not stopped")
                return
            self._conn.start()
            self._mark_dirty()
        self._spawn_access(switch, "SwitchToSsh")

    def wake(self):
        """唤醒只恢复当前接入及服务会话，不复活已停止的 SSH 接入。"""
        def reconnect():
            if self._vpn_active():
                self._conn.reconnect_forwards_now()
            else:
                self._conn.handle_reconnect_trigger()
            self._mounts.reconnect_now()
            self._mark_dirty()
        self._spawn_access(reconnect, "WakeReconnect")

    def stop_proxy(self):
        """关闭接入（设置窗显式按钮）：只停 -D 代理会话（ADR-011 修订
        2026-09-27：接入层/服务层分离——转发会话与 NFS 挂载不陪葬）。
        停止即终止不做退避拉起，重启走显式「重新连接」。慢操作后台跑。"""
        def _stop():
            self._conn.stop_access()
            if self._on_access_stopped is not None:
                try:
                    self._on_access_stopped()
                except Exception:  # noqa: BLE001
                    logger.exception("post-stop convergence failed")
        self._spawn_access(_stop, "StopProxy")
        self._mark_dirty()

    # ── 端口转发会话（多活）──────────────────────────────

    def forward_session(self, tunnel_id, action):
        """显式启停一条转发会话（桥接 action=start/stop）。start 走
        host-key 首连流程（内含 AppKit alert——host_key_flow 自带
        callAfter 回主线程，daemon 线程安全）故后台跑；stop 快操作同步。"""
        if action == "start":
            def _start():
                ok, reason = self._conn.start_forward(tunnel_id)
                if not ok:
                    self._notify(i18n.t("notify.forward.start_failed.title"), reason)
            self._spawn(_start, "ForwardStart")
        else:
            self._conn.stop_forward(tunnel_id)
        self._mark_dirty()

    def toggle_forward_session(self, tunnel_id):
        """菜单「启动/停止端口转发」：无会话则启、有则停（状态推导）。"""
        running = {s.tunnel_id for s in self._conn.forward_sessions()}
        self.forward_session(
            tunnel_id, "stop" if tunnel_id in running else "start")

    def toggle_forward_row(self, tunnel_id, index):
        """菜单「端口映射逐条启停」：翻转该行磁盘 enabled + 守卫重建。
        全服务器统一路径（ADR-011 修订：代理服务器自己的转发也是独立
        -L 会话——点它不再重启整个 -D 接入）。

        -L 集合只在会话启动时生效——守卫在 ConnectionCoordinator；
        mutate 构造归 mpconf.toggle_forward_row；写径经注入的事务
        update_mp（#46 事务写 + 磁盘真相推导目标态）。"""
        row = _mpconf.forward_row(_mpconf.load_config(), tunnel_id, index)
        if row is None:
            return
        enabled = forward_enabled(row)
        lp, rp = row.get("local_port"), row.get("remote_port")
        if not self._update_mp(
                lambda c: _mpconf.toggle_forward_row(
                    c, tunnel_id, index, not enabled)):
            return
        # 守卫重建；放行后按新配置推导如实文案（c-3：全停用后
        # restart 实为收敛停止）
        note = ""
        if self._conn.restart_forward_async(
                tunnel_id, self._reload_config,
                thread_name="ToggleForwardRebuild"):
            any_enabled = any(
                forward_enabled(f)
                for f in _mpconf.forward_rows(
                    _mpconf.load_config(), tunnel_id))
            note = i18n.t("notify.forward.note.stopped"
                          if not any_enabled
                          else "notify.forward.note.rebuild")
        self._notify(
            i18n.t("notify.forward.enabled.title" if not enabled
                   else "notify.forward.disabled.title"),
            f"{lp} → {rp}{note}")
        self._mark_dirty()

    # ── NFS 挂载（ADR-007）───────────────────────────────

    def mount(self, tunnel_id, name, action):
        """显式挂载/卸载（桥接 action=mount/unmount）。start_mount 起
        专用会话（host-key 首连走 AppHelper 回主线程弹信任框）+ 派发
        mount job 到 worker——都在协调器内，这里只分派意图。"""
        if action == "unmount":
            self._mounts.stop_mount(tunnel_id, name)
        else:
            self._mounts.start_mount(tunnel_id, name)
        self._mark_dirty()

    def toggle_mount(self, tunnel_id, name):
        """菜单「挂载/卸载」：在挂（mounted/mounting/unmounting）则卸，
        其余（unmounted/error）则挂（状态推导）。"""
        states = {(m.tunnel_id, m.name): m.status
                  for m in self._mounts.mount_states()}
        in_mount = states.get((tunnel_id, name)) in (
            "mounted", "mounting", "unmounting")
        self.mount(tunnel_id, name, "unmount" if in_mount else "mount")

    # ── 抓包 ──────────────────────────────────────────────

    def set_capture(self, enabled):
        """开/关抓包模式（菜单 toggle 的动作半边；端口占用对话与 CA
        信任引导等 UI 门控留在调用方 adapter）。开失败给用户可见告警。"""
        if not enabled:
            self._capture_ctrl.disable()
            self._mark_dirty()
            return
        if not self._capture_ctrl.enable():
            self._alert(i18n.t("alert.capture.no_mitmdump"))
        self._mark_dirty()

    def open_capture_dir(self):
        """打开抓包目录（经 prepare 带标记建目录——裸 makedirs 会打败
        prepare 的「本应用创建」安全契约，#70 W13）。"""
        from capture import capture_store
        try:
            d = capture_store.prepare(self._get_capture_dir())
            subprocess.Popen(["open", d])
        except OSError:
            logger.exception("Failed to open capture dir")

    # ── VPN（M2 接线，spec §3.3）────────────────────────

    def vpn_connect(self, server):
        """连接 VPN（慢操作：可能弹管理员授权 + 子进程起停——后台跑，
        菜单/HTTP 点击即返回）。"""
        self._spawn_access(lambda: self._vpn_connect_impl(server), "VpnConnect")

    def vpn_disconnect(self):
        """断开 VPN（管理口 SIGTERM 优雅退出 + DNS down 脚本——后台跑）。"""
        self._spawn_access(self._vpn_disconnect_impl, "VpnDisconnect")

    # ── AI 助手指令 ───────────────────────────────────────

    def copy_agent_instructions(self):
        """复制 AI 助手指令上剪贴板。ADR-009：指令里的 curl 要能被
        agent 立即使用——复制即闩锁持有配置服务（本次会话保持监听）。"""
        result = {"ok": False, "error": i18n.t("ui.agent.native_copy_failed")}
        try:
            if self._hold_copy_latch() is False:
                result["error"] = i18n.t("ui.agent.api_unavailable")
            else:
                text = self._get_agent_instructions()
                # Finder 启动的 .app 没有 locale；pbcopy 按 C 编码处理中文
                # 会写空剪贴板，且仍返回 0。显式覆盖 LC_ALL，不能只查退出码。
                subprocess.run(
                    ["/usr/bin/pbcopy"], input=text.encode("utf-8"),
                    env={**os.environ, "LC_ALL": "en_US.UTF-8"},
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    check=True, timeout=5,
                )
                result = {"ok": True}
        except Exception as exc:
            # 指令含 token，不记录子进程输入或异常载荷。
            logger.warning("Copy agent instructions failed (%s)", type(exc).__name__)
        try:
            if result["ok"]:
                self._notify(i18n.t("notify.copy_instructions.title"),
                             i18n.t("notify.copy_instructions.body"))
            else:
                self._notify(i18n.t("notify.copy_instructions.failed"), result["error"])
        except Exception:
            logger.warning("Copy instructions notification unavailable")
        return result
