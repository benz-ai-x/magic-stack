#!/usr/bin/env python3
"""Magic Stack — macOS menu bar app for HTTP→SOCKS5 over SSH tunnel."""
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time

from AppKit import NSApplication, NSMenu, NSMenuItem, NSApplicationWillTerminateNotification
from Foundation import NSObject, NSNotificationCenter
import rumps

from capture import ca_trust
from capture import chromium_proxy
from shared import i18n, keychain
from sysctl import login_item
from shared import netloc
from shared.identity import IdentityMigrationError
from sysctl import port_check
from shellui.bridge_protocol import (ACTION_COPY_AGENT_INSTRUCTIONS,
    ACTION_FORWARD_SESSION, ACTION_NFS_MOUNT_TOGGLE, ACTION_OPEN_PATH,
    ACTION_RECONNECT_PROXY, ACTION_STOP_PROXY)
from shared.defaults import (DEFAULT_CAPTURE_DIR, DEFAULT_CAPTURE_PORT,
                             VPN_MANAGEMENT_PORT)
from mpconf.config import (  # noqa: F401 — DEFAULT_CONFIG 是模块导出符号
    DEFAULT_CONFIG, load_config, merge_config, resolve_mount_dir,
    server_openvpn, servers)
from mount.coordinator import MountCoordinator
from vpn import privilege as vpn_privilege
from vpn import profile_store as vpn_profile_store
from vpn import dns_scripts as vpn_dns_scripts
from vpn.openvpn_client import VpnClient
from shared.runtime_state import RuntimeProjection
from shellui.log_window import LogBuffer, show_log_window
from shellui.webview_window import show_config_window
from shellui.menu_builder import MenuBuilder, MenuState, _menubar_color
from mpconf.config_state import ConfigStateStore
from shared.stats import Stats
from tunnel.connection_coordinator import ConnectionCoordinator
from tunnel.reconnect_trigger import ReconnectTrigger, WakeEventSource
from services.intents import UserIntents
from services.lifecycle_runtime import LifecycleRuntime, config_server_wanted
from util import build_stamp, version_display, resource_path

LOG_DIR = os.path.expanduser("~/Library/Logs")
LOG_PATH = os.path.join(LOG_DIR, "MagicProxy.log")
VERSION = "0.15.0"
VERSION_DISPLAY = version_display(VERSION, build_stamp())

log_buffer = LogBuffer()

# VPN 结构化错误码 → 文案键（字面键名表——取词守卫纪律：表存键名，
# t() 调用点查表；键全集在 shared/locales 双侧登记）
_VPN_ERR_KEYS = {
    "auth_failed": "vpn.err.auth_failed",
    "auth_required": "vpn.err.auth_required",
    "auth_challenge_unsupported": "vpn.err.challenge",
    "cipher_mismatch": "vpn.err.cipher",
    "cert_expired": "vpn.err.cert_expired",
    "cert_invalid": "vpn.err.cert_invalid",
    "tls_error": "vpn.err.tls",
    "unreachable": "vpn.err.unreachable",
    "server_exit": "vpn.err.server_exit",
    "crashed": "vpn.err.crashed",
    "mgmt_lost": "vpn.err.mgmt_lost",
    "mgmt_attach_failed": "vpn.err.mgmt_attach",
    "start_failed": "vpn.err.start_failed",
    "fatal": "vpn.err.generic",
}
_VPN_INSTALL_ERR_KEYS = {
    "cancelled": "vpn.err.install_cancelled",
    "sudoers_verify_failed": "vpn.err.install_verify",
    "openvpn_missing": "vpn.err.no_binary_code",
    "osascript_failed": "vpn.err.install_generic",
}


def _thread_excepthook(args):
    """threading.excepthook（模块级可直测）：线程未捕获异常进日志。"""
    logger.error(
        "unhandled exception in thread %s",
        getattr(args.thread, "name", "?"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _sys_excepthook(t, v, tb):
    """sys.excepthook：主解释器未捕获异常进日志。"""
    logger.error("unhandled exception", exc_info=(t, v, tb))


def _install_excepthooks():
    """windowed 应用的 stderr 丢失黑洞（真机教训：HTTP handler 线程异常
    打到 stderr 后凭空消失 = 「操作失败」零线索）。三路兜底全进日志：
    threading.excepthook（线程未捕获异常）/ sys.excepthook（主解释器）/
    sys.stderr 重定向（ThreadingMixIn.handle_error 等直接 print 的存量
    路径）。"""
    threading.excepthook = _thread_excepthook
    sys.excepthook = _sys_excepthook

    class _StderrToLog:
        def write(self, text):
            if text and text.strip():
                logger.error("stderr| %s", text.rstrip())

        def flush(self):
            pass

    sys.stderr = _StderrToLog()


def _setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if log_buffer not in root.handlers:
        root.addHandler(log_buffer)
    # 测试进程绝不写用户真实日志：root handler 共享曾把 MagicMock/假
    # 服务器输出写进 MagicProxy.log，两轮真机诊断被带偏（2026-09-27）
    if "pytest" in sys.modules:
        return
    if any(isinstance(h, logging.handlers.RotatingFileHandler)
           for h in root.handlers):
        _install_excepthooks()
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=512 * 1024, backupCount=2,
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        root.addHandler(handler)
    except OSError:
        pass
    _install_excepthooks()


_setup_logging()
logger = logging.getLogger("magic-proxy.app")
actions_log = logging.getLogger("magic-proxy.actions")
ssh_log = logging.getLogger("magic-proxy.ssh")




class _TerminateObserver(NSObject):
    """NSApplicationWillTerminate → app._shutdown()。

    菜单退出（quit_app）与 AppleEvent 退出（osascript quit / 注销 /
    重启）都必经 NSApp.terminate_——本观察者是两条路径的公共咽喉。
    曾有的缺口：清理只挂在菜单回调上，AppleEvent 退出直接跳过——NFS
    会话的 ssh 泄漏成孤儿（PPID=1）占住本地端口，下个实例的 NFS 会话
    在 ExitOnForwardFailure 下永久失败（实测：12049 被孤儿占用致挂载
    死循环）。"""

    def onTerminate_(self, _note):
        app = self._app_ref()
        if app is not None:
            app._shutdown()


class MagicProxyApp(rumps.App):
    def __init__(self):
        try:
            cfg = load_config()
        except IdentityMigrationError as exc:
            # 迁移可行动错误（显式重复 id）：绝不带病运行——弹窗给出
            # 处置指引后退出，原配置文件未被动过
            rumps.alert(
                "Magic Stack",
                i18n.t("alert.dup_id.startup", exc=exc))
            raise SystemExit(1)
        self._config = merge_config(cfg)
        # ADR-012：语言随配置收敛（首个 build 前生效）；auto 在此解析
        self._apply_language()
        self._stats = Stats()
        # 菜单开关的唯一写径持有者（#46）：与 UI 保存同一事务管线
        self._config_store = ConfigStateStore(keychain=keychain)
        self.VERSION = VERSION
        self.VERSION_DISPLAY = VERSION_DISPLAY
        self._log_path = LOG_PATH
        self._log_buffer = log_buffer

        # Connection lifecycle
        self._conn = ConnectionCoordinator(
            stats=self._stats,
            ssh_log_sink=lambda line: ssh_log.info("ssh| %s", line),
            get_config=lambda: self._config,
            get_tunnel_password=self._tunnel_password,
        )
        # NFS 挂载协调（ADR-007）：专用 NFS 会话 + 挂载生命周期，与端口
        # 转发会话并行互不干扰；resolve_mount_dir 注入——mount 域不横向
        # import mpconf
        self._mounts = MountCoordinator(
            get_config=lambda: self._config,
            get_tunnel_password=self._tunnel_password,
            ssh_log_sink=lambda line: ssh_log.info("nfs| %s", line),
            resolve_mount_dir=resolve_mount_dir,
        )
        # #86：唤醒事件 → 立即重连（跳过退避）。事件源装不上则静默
        # 降级——网络中断场景由 #85 的无限退避兜底。多活：NFS 会话同拍
        # 僵尸重建（唤醒断了所有隧道的 TCP）。
        self._reconnect_trigger = ReconnectTrigger(self._on_wake_event)
        WakeEventSource(self._reconnect_trigger.notify).start()

        # Non-blocking quit→relaunch state machine for proxied app launches
        self._relaunch_waiter = None

        # VPN 客户端（M2 接线）：全局至多一条（spec §5.3）——懒构造，
        # 每次连接按当次解析的二进制/凭证重建；投影 lambda 惰性读
        self._vpn_client = None
        # 连接并发闸：连点/菜单+设置窗双入口同时触发时只跑一个
        # （屏障与 VpnClient 重建都不重入）
        self._vpn_connect_lock = threading.Lock()

        # Services (AI router + capture + system proxy + sleep + config
        # server): LifecycleRuntime 持有全部构造/启动/退出顺序（架构候选
        # 2+3）——app 只经合法属性面取子模块引用，不再两阶段构造、不再
        # 私有属性掏取，「抓包正在运行」在 lifecycle 内单一投影。
        # 运行态投影（架构评审 R3）：app 一处组装，capture/forwards/mounts
        # 三参穿层塌缩为一个 RuntimeProjection seam（懒求值——构造期
        # _capture_ctrl 尚未由 lifecycle 创建，请求时才调用）
        self._lifecycle = LifecycleRuntime(
            config_fn=lambda: self._config,
            ssh_monitor=self._conn.ssh,
            paused_fn=lambda: self._conn.paused,
            on_menu_dirty=lambda: setattr(self._menu_builder, "last_struct_key", None),
            initial_sys_proxy_on=self._config.get("system_proxy_default", False),
            runtime_state_fn=lambda: RuntimeProjection(
                capture_active=self._capture_ctrl.actively_running,
                forwards=tuple(self._conn.forward_sessions()),
                mounts=tuple(self._mounts.mount_states()),
                vpn=(self._vpn_client.snapshot()
                     if self._vpn_client is not None else None)),
            on_mp_saved=self._on_mp_saved,
        )
        self._suanpan = self._lifecycle.suanpan
        self._capture_ctrl = self._lifecycle.capture_ctrl
        self._sys_proxy = self._lifecycle.sys_proxy
        self._capture = self._lifecycle.capture
        self._config_server = self._lifecycle.config_server
        # VPN 动作 seam（M2）：connect/disconnect 是 app 侧动作（VpnClient
        # 持有者 + 互斥屏障），设置窗 HTTP 端点经此回调——与 runtime_state_fn
        # 同款注入，浏览器直开场景同路径可用（不经原生 bridge）
        self._config_server.vpn_connect_fn = self._vpn_connect_intent
        self._config_server.vpn_disconnect_fn = self._vpn_disconnect_intent
        # 用户意图单一归宿（架构评审 R5 候选 1）：菜单回调与设置窗桥接
        # 两套 adapter 共用——guard 分派/线程纪律/通知/dirty 在 intents
        # 独占，app 只做翻译（菜单从状态推导、桥接从 action 字符串映射）
        self._intents = UserIntents(
            conn=self._conn,
            mounts=self._mounts,
            notify=self._notify,
            mark_dirty=self._dirty,
            update_mp=self._update_mp_config,
            reload_config=self._reload_config_or_alert,
            capture_ctrl=self._capture_ctrl,
            get_capture_dir=lambda: self._config.get(
                "capture_dir", DEFAULT_CAPTURE_DIR),
            alert=lambda message: rumps.alert(
                title="Magic Stack", message=message),
            hold_copy_latch=lambda: self._set_config_holders(
                copy_latch=True),
            get_agent_instructions=self._config_server.agent_instructions,
            vpn_connect=self._vpn_do_connect,
            vpn_disconnect=self._vpn_do_disconnect,
        )
        # ADR-009 配置服务持有者：设置窗开着 / 复制指令会话闩锁。
        # config_api_enabled 是第三持有者（磁盘真相，经 self._config 读）。
        self._config_window_open = False
        self._copy_api_latch = False
        if not self._lifecycle.start_all():
            # 单实例守卫失败（issue #3）：用户可见的清晰错误，绝不以
            # 僵尸实例形态继续起菜单。
            rumps.alert("Magic Stack", i18n.t("alert.instance_running"))
            raise SystemExit(0)
        # VPN 启动期 reconcile（spec §5.4）：上次异常退出可能留下占着
        # 管理口的 root openvpn 孤儿与未恢复的 DNS——收养 SIGTERM + DNS
        # 标志补跑。缺 sudoers/二进制（首次使用）静默跳过。
        self._vpn_startup_reconcile()

        # Menu
        self._menu_builder = MenuBuilder(
            self, self._make_menu_state)

        super().__init__(
            name="Magic Stack",
            title="⚫",
            quit_button=None,
        )
        self._install_edit_menu()
        self._menu_builder.build()

        if cfg is None or not self._config.get("servers"):
            self.show_preferences(None)
        else:
            self.check_both_ports()
            self._conn.start()
            # 多活：forward_autostart 的转发会话随应用启动恢复
            self._conn.apply_autostarts()
        # NFS：auto_mount 的挂载项随应用启动恢复（tick 负责补会话）
        self._mounts.apply_autostarts()

        # 退出咽喉：AppleEvent 退出不走菜单回调——统一经
        # NSApplicationWillTerminate 进 _shutdown（见 _TerminateObserver）
        self._shutdown_done = False
        import weakref
        self._terminate_observer = _TerminateObserver.alloc().init()
        self._terminate_observer._app_ref = weakref.ref(self)
        NSNotificationCenter.defaultCenter(
        ).addObserver_selector_name_object_(
            self._terminate_observer, "onTerminate:",
            NSApplicationWillTerminateNotification, None)

        rumps.Timer(self._on_tick, 1).start()

    # ── helpers ──────────────────────────────────────────

    @staticmethod
    def _install_edit_menu():
        """Install standard Edit submenu for text field responder chain."""
        app = NSApplication.sharedApplication()
        main = app.mainMenu()
        if main is None:
            main = NSMenu.alloc().init()
            app.setMainMenu_(main)
        if main.itemWithTitle_("Edit") is not None:
            return
        edit_menu = NSMenu.alloc().initWithTitle_("Edit")
        for title, action, key in (
            ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
            ("Cut", "cut:", "x"), ("Select All", "selectAll:", "a"),
        ):
            edit_menu.addItem_(
                NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key))
        top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
        top.setSubmenu_(edit_menu)
        main.addItem_(top)

    def _tunnel_password(self, tunnel):
        _auth = (tunnel.get("ssh") or {}).get("auth_type") if tunnel else None
        if _auth == "password":
            return keychain.get_password(tunnel)
        return ""

    def _apply_language(self):
        """语言随配置收敛（ADR-012）：resolve（auto→AppleLanguages）后
        切换 i18n 全局；MenuState.language 进 struct_key，下一次 tick
        整树重建换文案。写径三条（启动/菜单 update_mp/UI 保存回调）都
        汇聚到本方法。"""
        i18n.set_language(i18n.resolve(self._config.get("language")))

    # ── menu state ───────────────────────────────────────

    def _make_menu_state(self):
        """Build a frozen snapshot of all data MenuBuilder reads."""
        s = self._conn.ssh
        sp = self._suanpan
        cap = self._capture_ctrl
        sysp = self._sys_proxy
        return MenuState(
            ssh_status=s.status,
            ssh_cmd_str=s.cmd_str,
            ssh_log=s.log if s.status == "connecting" else "",
            ssh_error_msg=s.error_msg,
            paused=self._conn.paused,
            stats_snapshot=self._stats.snapshot(),
            config=self._config,
            sys_proxy_on=sysp.on,
            sys_proxy_error=sysp.error,
            capture_enabled=cap.enabled,
            capture_state=cap.menu_state(),
            capture_hint=cap.hint(),
            suanpan_running=sp.running,
            suanpan_error=sp.error,
            suanpan_listen_address=sp.listen_address() if sp.running else "",
            current_server=self._conn.current_server,
            forward_states=tuple(self._conn.forward_sessions()),
            mount_states=tuple(self._mounts.mount_states()),
            language=i18n.language(),
            vpn_status=(getattr(self, "_vpn_client").vpn.status
                        if getattr(self, "_vpn_client", None) is not None
                        else "idle"),
            vpn_server=((self._vpn_configured_server() or {}).get("name")
                        or ""),
            vpn_error=(getattr(self, "_vpn_client").vpn.error_kind
                       if getattr(self, "_vpn_client", None) is not None
                       else ""),
            vpn_tun_ip=(getattr(self, "_vpn_client").vpn.tun_ip
                        if getattr(self, "_vpn_client", None) is not None
                        else ""),
        )

    # ── VPN（M2 接线，spec §3.3/§7.2）────────────────────

    def _vpn_configured_server(self):
        """「已配置 VPN 的服务器」单一解析：profile_set 的第一台。"""
        for s in servers(self._config):
            if isinstance(s, dict) and server_openvpn(s).get("profile_set"):
                return s
        return None

    def _vpn_connect_intent(self, index, force=False):
        """设置窗 HTTP 入口：同步校验（可拒）→ 意图层后台执行。
        返回 dict 直接作为端点响应（错误用结构化码，文案在 UI 侧映射）。"""
        logger.info("vpn connect intent: index=%r force=%r", index, force)
        rows = servers(self._config)
        if (isinstance(index, bool) or not isinstance(index, int)
                or not 0 <= index < len(rows)):
            return {"ok": False, "error": "bad_index"}
        server = rows[index]
        # profile 判定以落盘文件为准（导入端点即时写文件）——不依赖保存
        # 流的 profile_set 布尔：真机案例（2026-09-27）导入后未保存，连
        # 接被 no_profile 弹回而用户只见「没反应」
        if not (server_openvpn(server).get("profile_set")
                or vpn_profile_store.profile_exists(server.get("id") or "")):
            return {"ok": False, "error": "no_profile"}
        # 接入互斥（ADR-011 修订）：仅 -D 接入活跃时拒连，UI 确认后
        # force 重发——转发/NFS 属服务层，不再拦 VPN
        if not force and self._access_active():
            return {"ok": False, "error": "ssh_active"}
        self._intents.vpn_connect(server)
        return {"ok": True}

    def _vpn_disconnect_intent(self):
        self._intents.vpn_disconnect()
        return {"ok": True}

    def _access_active(self):
        """接入层活跃（ADR-011 修订，2026-09-27）：仅 -D 代理会话
        connecting/connected。转发会话与 NFS 挂载属服务层，不拦
        VPN——接入互斥只发生在接入面。"""
        return self._conn.ssh.status in ("connecting", "connected")

    def _vpn_do_connect(self, server):
        """连接核心（intents 线程纪律：daemon 后台跑）。接入层切换 =
        只停 -D 会话（ADR-011 修订：服务层转发/NFS 不陪葬）；断开不
        自动回切接入。并发闸：重入即跳过（连点保护——多线程同时做接入
        切换/重建客户端会互相踩，真机连点场景）。"""
        if not self._vpn_connect_lock.acquire(blocking=False):
            logger.info("vpn connect skipped: already in flight")
            return
        try:
            self._vpn_do_connect_locked(server)
        finally:
            self._vpn_connect_lock.release()

    def _vpn_do_connect_locked(self, server):
        logger.info("vpn connect requested: server=%s", server.get("id"))
        binary = vpn_privilege.resolve_openvpn_bin()
        if not binary:
            logger.warning("vpn connect aborted: openvpn binary missing")
            self._notify(i18n.t("notify.vpn.no_binary"),
                         i18n.t("notify.vpn.no_binary_body"))
            return
        profile_text = vpn_profile_store.load_profile(server.get("id") or "")
        if not profile_text.strip():
            logger.warning("vpn connect aborted: profile empty (id=%s)",
                           server.get("id"))
            self._notify(i18n.t("notify.vpn.no_profile"), "")
            return
        svc = server_openvpn(server)
        mgmt_pw = keychain.ensure_vpn_mgmt_password()
        # 重装条件：sudoers 缺失，或磁盘 dns 脚本不是当前版本（脚本
        # 0755 可读可比对；conf 0600 不可读——其重装挂脚本版本：dns/
        # conf 组成变更即 bump SCRIPTS_VERSION，杜绝「conf 用到天荒地老」）
        if not vpn_privilege.check_sudoers(binary) \
                or not vpn_dns_scripts.assets_current():
            logger.info("vpn connect: sudoers/assets outdated, installing")
            ok, code = vpn_privilege.install(
                conf_text=vpn_privilege.runtime_conf(
                    profile_text, pull_dns=svc.get("pull_dns", True)),
                mgmt_password=mgmt_pw)
            if not ok:
                logger.warning("vpn connect aborted: install failed (%s)", code)
                self._notify(
                    i18n.t("notify.vpn.install_failed"),
                    i18n.t(_VPN_INSTALL_ERR_KEYS.get(
                        code, "vpn.err.install_generic")))
                return
        # 接入层切换（ADR-011 修订）：只停 -D 会话 + 系统代理收敛
        # （别指着死掉的 :8888）+ 防睡眠重算——转发会话与 NFS 挂载是
        # 服务层，原地不动（路由翻转断掉的由 VPN connected 事件重建）
        logger.info("vpn connect: switching access layer (-D only)")
        self._conn.stop_access()
        self._sys_proxy.sync()
        self._lifecycle.sync_sleep(self._conn.ssh.status, self._conn.paused,
                             self._config.get("prevent_sleep", False))

        def _creds():
            return (svc.get("username", ""),
                    keychain.get_vpn_password(server))

        if self._vpn_client is not None:
            self._vpn_client.stop()
        self._vpn_client = VpnClient(
            full_cmd=vpn_privilege.build_full_command(binary),
            mgmt_port=VPN_MANAGEMENT_PORT,
            mgmt_password=mgmt_pw or None,
            credentials=_creds,
            on_state_change=self._on_vpn_state_change,
            on_error=self._vpn_error_notify,
        )
        self._vpn_client.start()
        self._dirty()

    def _on_vpn_state_change(self, snap):
        """VPN 状态回调：dirty 刷菜单；established 时路由翻转已断存量
        TCP——服务层僵尸重建（转发 + NFS 会话，与唤醒同语义；**绝不
        拉起 -D**：接入互斥，这是与 handle_reconnect_trigger 的根本
        差异）。"""
        self._dirty()
        if isinstance(snap, dict) and snap.get("status") == "connected":
            logger.info("vpn established: rebuilding service sessions")
            self._conn.reconnect_forwards_now()
            self._mounts.reconnect_now()

    def _vpn_do_disconnect(self):
        client = self._vpn_client
        if client is not None:
            client.stop()
        self._dirty()

    def _vpn_error_notify(self, kind, text):
        self._notify(i18n.t(_VPN_ERR_KEYS.get(kind, "vpn.err.generic")),
                     text or "")

    def toggle_vpn(self, _item):
        """菜单「连接/断开 VPN」：状态推导动作（A 类动词语法）。
        SSH 活跃时不再踢去设置窗（真机反馈的死胡同）——原生确认框
        一步到位：确认即拆屏障连接。"""
        client = self._vpn_client
        if client is not None and client.vpn.status in (
                "connecting", "connected", "reconnecting"):
            self._intents.vpn_disconnect()
            return
        server = self._vpn_configured_server()
        if server is None:
            self.show_preferences(None)
            return
        if self._access_active():
            logger.info("vpn menu connect: access active, confirming")
            # rumps.alert 第 3 个位置参数即 ok——标题/正文各占一个位置
            # 参数，按钮文案只能走关键字（3 位置 + ok= 会 TypeError）
            ok = rumps.alert(
                i18n.t("notify.vpn.ssh_active"),
                i18n.t("notify.vpn.ssh_active_body_force"),
                ok=i18n.t("vpn.confirm_ok"))
            if not ok:
                return
        self._intents.vpn_connect(server)

    def toggle_ssh(self, _item):
        """接入段 SSH 行（行即开关，2026-09-27 定稿）：VPN 活跃 → 原生
        确认切回 SSH 接入（断 VPN + 起 -D；服务层会话自管，无需恢复面
        ——ADR-011 修订）；连接中 → 取消；已连接 → 停止接入（转发/
        挂载不受影响）；空闲/失败 → 发起连接。"""
        client = getattr(self, "_vpn_client", None)
        vpn_active = client is not None and client.vpn.status in (
            "connecting", "connected", "reconnecting")
        if vpn_active:
            logger.info("ssh access row: vpn active, confirming switch")
            # rumps.alert 第 3 个位置参数即 ok——标题/正文各占一个位置
            # 参数，按钮文案只能走关键字（3 位置 + ok= 会 TypeError）
            ok = rumps.alert(
                i18n.t("mode.ssh_confirm_title"),
                i18n.t("mode.ssh_confirm_body"),
                ok=i18n.t("mode.switch_ok"))
            if not ok:
                return
            self._intents.vpn_disconnect()
            self._conn.start()
            return
        s = self._conn.ssh.status
        if s == "connecting":
            self.cancel_connection(None)
        elif s == "connected":
            self.stop_proxy_tunnel(None)
        else:
            self._intents.reconnect_proxy_or_forward()

    def _vpn_startup_reconcile(self):
        """启动期收养（spec §5.4 落地到生命周期）：残留 root openvpn
        （上次崩溃/强杀后永久占管理口者）经管理口 SIGTERM + DNS 标志
        补跑。任何失败只记日志，绝不阻断启动。"""
        try:
            from vpn.openvpn_client import adopt_stale_openvpn
            from vpn import dns_scripts
            pw = keychain.get_vpn_mgmt_password()
            binp = vpn_privilege.resolve_openvpn_bin()
            if pw and binp and vpn_privilege.check_sudoers(binp):
                adopt_stale_openvpn(pw, VPN_MANAGEMENT_PORT, timeout=1.0)
                dns_scripts.run_reconcile()
        except Exception:
            logger.exception("vpn startup reconcile failed")

    # ── tick ─────────────────────────────────────────────

    def _on_wake_event(self):
        """#86 唤醒 → 代理/转发会话重连 + NFS 会话僵尸重建（同拍）。"""
        self._conn.handle_reconnect_trigger()
        self._mounts.reconnect_now()

    def _on_tick(self, _):
        self._stats.tick()
        self._conn.handle_retry()
        # getattr 兜底：半构造的测试替身无此属性（_shutdown_done 同款纪律）
        if getattr(self, "_vpn_client", None) is not None:
            self._vpn_client.check()

        # 主图标色（用户拍板语义）：灰=无连接 / 蓝=SSH / 绿=VPN / 黄=连接中
        vpn_client = getattr(self, "_vpn_client", None)
        vpn_st = vpn_client.vpn.status if vpn_client is not None else "idle"
        self._menu_builder.set_status_icon(
            _menubar_color(self._conn.ssh.status, self._conn.paused, vpn_st))

        key = self._menu_builder.struct_key()
        if key != self._menu_builder.last_struct_key:
            self._menu_builder.build()
            self._menu_builder.last_struct_key = key
        else:
            self._menu_builder.refresh_titles()

        # SSH check AFTER icon (matches original)
        self._conn.check_ssh()
        self._conn.check_forwards()
        # NFS 挂载收敛（会话健康 + 挂载/卸载 reconcile，不阻塞主线程）
        self._mounts.tick()

        # Services —— 防睡眠按聚合状态：任一会话在跑就不睡（暂停是代理
        # 会话语义，转发会话仍在服务时不因代理暂停而允许睡眠）；挂载在
        # 途/已挂载同理（hard 挂载睡着 = Finder 卡死）
        self._lifecycle.tick(self._config.get("capture_port", DEFAULT_CAPTURE_PORT))
        mounts_active = (self._mounts.any_mounted()
                         or self._mounts.any_session_connected())
        sleep_status = ("connected"
                        if (self._conn.any_connected or mounts_active)
                        else s)
        sleep_paused = (self._conn.paused
                        and not self._conn.any_forward_session_connected
                        and not mounts_active)
        self._lifecycle.sync_sleep(sleep_status, sleep_paused,
                             self._config.get("prevent_sleep", False))

        # Pending proxied-app relaunch (quit → wait → launch)
        self._tick_relaunch()

    def _tick_relaunch(self):
        """Advance the quit→relaunch state machine (never blocks the menu)."""
        w = self._relaunch_waiter
        if w is None:
            return
        action, payload = w.step()
        if action is None:
            return
        self._relaunch_waiter = None
        if action == "timeout":
            rumps.alert(title="Magic Stack",
                        message=i18n.t("alert.relaunch.timeout", name=w.name))
            return
        ok, err = chromium_proxy.launch(w.path, payload)
        if not ok:
            rumps.alert(title="Magic Stack",
                        message=i18n.t("alert.launch.failed", err=err))
            return
        rumps.alert(
            title="Magic Stack",
            message=i18n.t("alert.launched_proxied", name=w.name,
                           addr=payload),
        )

    # ── menu callbacks ───────────────────────────────────

    def _dirty(self):
        self._menu_builder.last_struct_key = None

    def _update_mp_config(self, mutate):
        """菜单开关唯一写径（#46）：写前读新 + 事务写，成功后刷新内存副本。

        旧径 save_config(self._config) 用启动时的内存副本整文件覆写——
        UI 保存后不重连就点开关，磁盘上 UI 的改动被静默抹掉。现全部经
        ConfigStateStore.update_mp（与 UI 保存同一校验 + journal + 0600
        原子写管线），成功后重读磁盘刷新副本。
        """
        try:
            result = self._config_store.update_mp(mutate)
        except IdentityMigrationError as exc:
            rumps.alert(
                "Magic Stack",
                i18n.t("alert.dup_id.save", exc=exc))
            return False
        if not result.ok:
            logger.warning("menu config update rejected: %s", result.errors)
            self._notify(i18n.t("notify.mp_save_failed.title"),
                         "; ".join(result.errors)[:160])
            return False
        cfg = load_config()
        if cfg:
            self._config = merge_config(cfg)
        self._apply_language()
        self._dirty()
        return True

    def _notify(self, subtitle, message=""):
        # 用户可见通知全部落日志（i18n 文案不进日志的纪律只约束开发
        # 期打印——通知是诊断「用户看到了什么」的关键事实）
        logger.info("notify: %s | %s", subtitle, message)
        rumps.notification("Magic Stack", subtitle, message)

    def _on_mp_saved(self):
        """UI 保存 MP 段后的内存副本收敛（配置服务线程调用）。

        旧缺口：PUT 只落盘 + reload 网关，app 内存副本直到下一次重连才
        重读——防睡眠/抓包设置/代理角色在窗口期全按旧值行动。此处重读
        替换引用后，tick 与各使用点自然收敛（与 reconnect 的 reload_cfg
        同款跨线程纪律，#68）。
        """
        try:
            cfg = load_config()
        except IdentityMigrationError:
            return  # prepare 已拦病态写入，此为防御；旧副本继续服务
        if not cfg:
            return
        new_config = merge_config(cfg)
        old_login = bool(self._config.get("launch_at_login", False))
        new_login = bool(new_config.get("launch_at_login", False))
        self._config = new_config
        # 语言键可能随本次保存翻转（菜单/设置窗两写径在此汇合）
        self._apply_language()
        self._dirty()
        # 登录启动是唯一的配置外副作用：UI 保存路径此前只写文件不注册
        # LaunchAgent（只有菜单路径注册）——两条写径在此对齐
        if old_login != new_login:
            ok, err = login_item.set_launch_at_login(new_login)
            if not ok:
                logger.warning("UI 保存后同步登录启动失败：%s", err)
        # NFS：新配置的 auto_mount 挂载项收敛补挂（tick 负责补会话）
        self._mounts.apply_autostarts()
        # ADR-009：UI 系统页保存可能翻转 config_api_enabled——按新
        # 持有态收敛 :9528（设置窗此刻开着，服务不会被误停）
        self._sync_config_server()

    # ── connection ───────────────────────────────────────

    def cancel_connection(self, _):
        """取消连接中的接入（菜单 SSH 行点击/连接中态）：只取消 -D
        会话的建连尝试——转发会话是服务层，不陪葬（ADR-011 修订）。"""
        self._conn.stop_access()

    def stop_proxy_tunnel(self, _):
        """停止接入（菜单 SSH 行点击）：只停 -D 会话（含重试调度/本地
        代理运行时）——转发会话与 NFS 挂载是服务层，不受影响（ADR-011
        修订）。停止即终止，恢复走接入行再点。后置同步面对齐
        toggle_pause（系统代理收敛与防睡眠状态重算）。"""
        self._conn.stop_access()
        self._sys_proxy.sync()
        self._lifecycle.sync_sleep(self._conn.ssh.status, self._conn.paused,
                             self._config.get("prevent_sleep", False))

    def _reload_config_or_alert(self):
        """重读磁盘配置刷新内存副本（重连 / 单会话重建共用）。

        迁移可行动错误（重复 id）不得在回调里裸抛——保持现有连接并给
        出指引；调用方可能在 daemon 线程（#68），NSAlert 经
        AppHelper.callAfter 回主线程（host_key_flow 同款）。曾有两份
        reload_cfg 闭包一处弹窗一处静默的分叉，处置统一到本方法。
        """
        try:
            cfg = load_config()
        except IdentityMigrationError as exc:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(
                rumps.alert,
                "Magic Stack",
                i18n.t("alert.dup_id.reload", exc=exc))
            return
        if cfg:
            self._config = merge_config(cfg)
            self._apply_language()

    def reconnect(self, _):
        self._intents.reconnect_proxy_or_forward()

    def toggle_pause(self, _):
        self._conn.toggle_pause()
        self._sys_proxy.sync()
        self._lifecycle.sync_sleep(self._conn.ssh.status, self._conn.paused,
                             self._config.get("prevent_sleep", False))

    def toggle_system_proxy(self, _):
        self._sys_proxy.toggle()

    def make_switch_server(self, sid):
        """切换代理服务器（v2：proxy_server_id 单一真相）。"""
        def switch(_):
            target = next((t for t in self._config.get("servers", [])
                           if isinstance(t, dict) and t.get("id") == sid), None)
            if target is None:
                return
            if target is self._conn.current_server \
                    and self._conn.ssh.status == "connected":
                return
            if not self._update_mp_config(
                    lambda c: {**c, "proxy_server_id": sid}):
                return
            self.reconnect(None)
        return switch

    # ── 多活转发会话（v0.9） ──────────────────────────────

    def toggle_forward_session(self, tunnel_id):
        """菜单「启动/停止端口转发」：无会话则启，有则停。"""
        def act(_):
            self._intents.toggle_forward_session(tunnel_id)
        return act

    def make_reconnect_tunnel(self, tunnel_id):
        """重连指定隧道：代理隧道走整体 restart（含降级逻辑），转发会话
        单会话重建（显式意图——会话存在即重建，Spec-A 语义）。"""
        def act(_):
            self._intents.reconnect_proxy_or_forward(tunnel_id)
        return act

    def make_toggle_forward(self, tunnel_id, index):
        """菜单「端口映射逐条启停」：意图体在 UserIntents.toggle_forward_row
        （写径 + 守卫重建 + 如实文案——R5 候选 1 收敛）。"""
        def act(_):
            self._intents.toggle_forward_row(tunnel_id, index)
        return act

    # ── NFS 挂载（ADR-007）───────────────────────────────

    def make_toggle_mount(self, tunnel_id, name):
        """菜单「挂载/卸载」：在挂（mounted/mounting/unmounting）则卸，
        其余（unmounted/error）则挂。"""
        def act(_):
            self._intents.toggle_mount(tunnel_id, name)
        return act

    def make_open_mount_dir(self, tunnel_id, name):
        """菜单「打开挂载目录」：Finder 中打开（不存在则先建目录）。"""
        def act(_):
            for t in self._config.get("servers", []):
                if not (isinstance(t, dict) and t.get("id") == tunnel_id):
                    continue
                for row in ((((t.get("services") or {}).get("nfs") or {})
                             .get("mounts")) or []):
                    if isinstance(row, dict) and row.get("name") == name:
                        d = resolve_mount_dir(row)
                        try:
                            os.makedirs(d, exist_ok=True)
                            subprocess.Popen(["open", d])
                        except OSError:
                            actions_log.exception(
                                "Failed to open mount dir %s", d)
                        return
        return act

    # ── suanpan ──────────────────────────────────────────
    # SuanpanRuntime 的公开方法组装直接写在 App 的菜单回调里，不再多一层
    # 间接（LifecycleRuntime 升格落地）。

    def toggle_suanpan(self, _):
        sp = self._suanpan
        if sp.running:
            sp.stop()
            self._notify(i18n.t("notify.router.stopped"))
        elif sp.start():
            self._notify(i18n.t("notify.router.started"),
                         f"http://{sp.listen_address()}")
        else:
            self._notify(i18n.t("notify.router.start_failed"),
                         sp.error[:120])
        self._menu_builder.build()

    def copy_suanpan_url(self, _):
        url = f"http://{self._suanpan.listen_address()}"
        proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        proc.communicate(url.encode())
        self._notify(i18n.t("notify.router.url_copied"), url)

    def copy_suanpan_example(self, _):
        path = resource_path("suanpan.example.yaml")
        if not os.path.exists(path):
            self._notify(i18n.t("notify.router.example.title"),
                         i18n.t("notify.router.example.missing"))
            return
        with open(path) as f:
            content = f.read()
        proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        proc.communicate(content.encode())
        self._notify(i18n.t("notify.router.example_copied"),
                     i18n.t("notify.router.example_bytes",
                            n=len(content)))

    def reload_suanpan(self, _):
        sp = self._suanpan
        if sp.reload():
            self._notify(i18n.t("notify.router.reloaded"))
        else:
            self._notify(i18n.t("notify.router.reload_failed"),
                         sp.error[:120])
        self._menu_builder.build()

    def restart_suanpan(self, _):
        sp = self._suanpan
        if sp.running:
            sp.stop()
        if sp.start():
            self._notify(i18n.t("notify.router.restarted"),
                         f"http://{sp.listen_address()}")
        else:
            self._notify(i18n.t("notify.router.restart_failed"),
                         sp.error[:120])
        self._menu_builder.build()

    # ── sleep / login ────────────────────────────────────

    def toggle_prevent_sleep(self, _):
        if not self._update_mp_config(
                lambda c: {**c,
                           "prevent_sleep": not c.get("prevent_sleep", False)}):
            return
        self._lifecycle.sync_sleep(self._conn.ssh.status, self._conn.paused,
                             self._config.get("prevent_sleep", False))

    def toggle_launch_at_login(self, _):
        # 目标态从磁盘真相推导（#46 复核：内存副本可能滞后于 UI 保存，
        # 与 prevent_sleep 同一口径）
        cfg = load_config()
        enabled = not (cfg or {}).get("launch_at_login", False)
        ok, err = login_item.set_launch_at_login(enabled)
        if not ok:
            rumps.alert(title="Magic Stack",
                        message=i18n.t("alert.login.failed", err=err))
            self._dirty()
            return
        if not self._update_mp_config(
                lambda c: {**c, "launch_at_login": enabled}):
            return
        self._notify(
            i18n.t("notify.login.on_title" if enabled
                   else "notify.login.off_title"),
            i18n.t("notify.login.on_body" if enabled
                   else "notify.login.off_body"),
        )

    # ── capture ──────────────────────────────────────────

    def toggle_capture(self, _):
        """Toggle capture mode. Off is immediate; on gates through port + CA trust."""
        if self._capture_ctrl.enabled:
            self._intents.set_capture(False)
            return
        capture_port = self._config.get("capture_port", DEFAULT_CAPTURE_PORT)
        if not self._check_port(
                capture_port, i18n.t("common.label.capture")):
            return
        if ca_trust.is_trusted():
            self._intents.set_capture(True)
            return

        def on_result(trusted):
            if trusted:
                self._intents.set_capture(True)
            else:
                self._dirty()

        ca_trust.show_ca_trust_guide(on_result=on_result)

    def open_capture_dir(self, _):
        self._intents.open_capture_dir()

    def open_today_jsonl(self, _):
        from capture import capture_store
        try:
            d = capture_store.prepare(
                self._config.get("capture_dir", DEFAULT_CAPTURE_DIR))
            today = time.strftime("%Y-%m-%d")
            path = os.path.join(d, f"{today}.jsonl")
            if os.path.exists(path):
                subprocess.Popen(["open", "-t", path])
            else:
                subprocess.Popen(["open", d])
        except OSError:
            actions_log.exception("Failed to open today's JSONL")

    # ── misc ─────────────────────────────────────────────

    def open_log(self, _):
        try:
            subprocess.Popen(["open", self._log_path])
        except OSError:
            actions_log.exception("Failed to open log")

    def show_log_window(self, _):
        try:
            show_log_window(self._log_buffer)
        except Exception:
            actions_log.exception("show_log_window failed")

    def about(self, _):
        rumps.alert(
            title="Magic Stack",
            message=i18n.t("app.about.body", version=self.VERSION_DISPLAY))

    # ── proxied app launch ───────────────────────────────

    def make_launch_proxied(self, entry):
        def cb(_):
            self._launch_app_proxied(entry)
        return cb

    def _launch_app_proxied(self, entry):
        """Launch a Chromium app with --proxy-server."""
        name = entry["name"]
        path = entry.get("path") or chromium_proxy.app_path(entry)
        if not path:
            rumps.alert(title="Magic Stack",
                        message=i18n.t("alert.proxied.not_found",
                                        name=name))
            return
        http_listen = netloc.format_listen("127.0.0.1", int(self._config["http_listen_port"]))
        if chromium_proxy.is_running(path):
            resp = rumps.alert(
                title="Magic Stack",
                message=i18n.t("alert.proxied.running", name=name),
                ok=i18n.t("alert.proxied.quit_relaunch"),
                cancel=i18n.t("common.cancel_action"),
            )
            if not resp:
                return
            chromium_proxy.quit_app(path)
            # Waiting for the process to exit blocks the menu callback for up
            # to 5 s — hand off to the tick loop (see _tick_relaunch).
            self._relaunch_waiter = chromium_proxy.RelaunchWaiter(
                path, name, http_listen)
            return
        ok, err = chromium_proxy.launch(path, http_listen)
        if not ok:
            rumps.alert(title="Magic Stack",
                        message=i18n.t("alert.launch.failed", err=err))
            return
        rumps.alert(
            title="Magic Stack",
            message=i18n.t("alert.launched_proxied", name=name,
                           addr=http_listen),
        )

    # ── port check ───────────────────────────────────────

    def _check_port(self, port, label):
        """Detect port occupancy; prompt user; kill on confirm."""
        owner = port_check.who_owns(port)
        if owner is None:
            return True
        msg = i18n.t("alert.port.occupied", label=label, port=port,
                     owner_name=owner.name, pid=owner.pid,
                     cmd=owner.cmd[:120])
        if rumps.alert(title="Magic Stack", message=msg,
                       ok=i18n.t("alert.port.kill_confirm"),
                       cancel=i18n.t("common.cancel")) != 1:
            return False
        ok, err = port_check.kill(owner.pid)
        if not ok:
            rumps.alert(title="Magic Stack",
                        message=i18n.t("alert.port.kill_failed", err=err))
            return False
        return True

    def check_both_ports(self):
        """Run port check on SOCKS5 and HTTP ports from current config."""
        self._check_port(self._conn.socks5_port, "SOCKS5")
        try:
            http_port = int(self._config["http_listen_port"])
        except (KeyError, ValueError, TypeError):
            return
        self._check_port(http_port, "HTTP")

    # ── preferences / quit ───────────────────────────────

    def show_preferences(self, _):
        self._open_config_window("")

    def show_prefs_forwards(self, _):
        """端口映射空态深链：偏好设置 → 服务器（SSH 隧道服务卡转发表）。"""
        self._open_config_window("#servers")

    def show_prefs_mounts(self, _):
        """远程挂载空态深链：偏好设置 → 服务器（NFS 服务卡挂载表）。"""
        self._open_config_window("#servers")

    def show_agent_setup(self, _):
        """ADR-010 M5：菜单「配置 Agent…」深链——设置窗直达快速接入向导。"""
        self._open_config_window("#quickstart")

    def _open_config_window(self, fragment):
        try:
            # ADR-009：设置窗本身是配置服务持有者——先置位再开窗
            # （show_config_window 关旧窗的回调在调用内触发，晚置位会让
            # 旧窗关闭误判"无持有者"而停掉刚要用的服务）。
            # 启停全经 lifecycle 单一归宿（架构评审 C1：删直调
            # config_server.start() 的第二条启动路径）。
            if not self._set_config_holders(window_open=True):
                # 启动失败：服务未在听——刻意只清位不收敛（收敛无益，
                # 常驻开关持有者的收敛交给下一个自然事件）
                self._config_window_open = False
                rumps.alert(title="Magic Stack",
                            message=i18n.t("alert.prefs.port_busy"))
                return
            show_config_window(
                self._config_server.url + fragment, on_action=self._bridge_action,
                auth_headers={"Authorization":
                              f"Bearer {self._config_server.token}"},
                on_close=self._on_config_window_closed)
        except Exception as e:
            logger.exception("show_preferences failed")
            # 服务可能已在听——清位必须收敛（R2-4 修复点：曾漏收敛，
            # 零持有者时 :9528 常驻到下一个偶然事件）
            self._set_config_holders(window_open=False)
            rumps.alert(title="Magic Stack",
                        message=i18n.t("alert.prefs.failed", err=repr(e)))

    def _on_config_window_closed(self):
        """设置窗真关闭（webview_window windowWillClose）→ 释放持有者。"""
        self._set_config_holders(window_open=False)

    def _set_config_holders(self, *, window_open=None, copy_latch=None):
        """ADR-009 持有者唯一写口：置位/清位与 :9528 收敛是一个动作
        （架构评审 R2-4：变更位与收敛位曾靠各调用点配对记性——except
        路径漏收敛，零持有者时服务常驻到下一个偶然事件）。返回收敛
        结果。

        唯一刻意不收敛的路径是开窗启动失败分支（服务未在听，收敛无
        益——常驻开关持有者的收敛交给下一个自然事件）。"""
        if window_open is not None:
            self._config_window_open = window_open
        if copy_latch is not None:
            self._copy_api_latch = copy_latch
        return self._sync_config_server()

    def _sync_config_server(self):
        """ADR-009 持有状态机收敛：三持有者任一在场即监听 :9528，否则
        释放。返回收敛结果（False = 想起但端口被占用）——开窗路径据此
        提示；无变更的收敛（如 UI 保存翻转 config_api_enabled）直接调
        用本方法。"""
        return self._lifecycle.sync_config_server(config_server_wanted(
            self._config_window_open,
            bool(self._config.get("config_api_enabled")),
            self._copy_api_latch))

    def toggle_config_api(self, _):
        """系 统 ▸「配置 API 服务」开关（ADR-009）：目标态从磁盘真相推导
        （#46 口径，与 prevent_sleep/launch_at_login 同款）。"""
        cfg = load_config()
        enabled = not (cfg or {}).get("config_api_enabled", False)
        if not self._update_mp_config(
                lambda c: {**c, "config_api_enabled": enabled}):
            return
        self._sync_config_server()
        self._notify(
            i18n.t("notify.configapi.on_title" if enabled
                   else "notify.configapi.off_title"),
            i18n.t("notify.configapi.on_body" if enabled
                   else "notify.configapi.off_body"))

    def make_set_language(self, pref):
        """选项 ▸「语言」单选（ADR-012）：写 mp 配置（auto/zh-CN/en）；
        语言收敛与菜单重建经 _update_mp_config 既有机制（_apply_language
        + struct_key 整树重建），本回调只做翻译。"""
        def act(_):
            if pref not in i18n.PREFERENCES:
                return
            self._update_mp_config(lambda c: {**c, "language": pref})
        return act

    def copy_agent_instructions(self, _):
        """菜单栏页脚「复制 AI 助手指令」（v0.9.1）——免开设置窗直通。
        意图体在 UserIntents（ADR-009 闩锁 + pbcopy + 通知）。"""
        self._intents.copy_agent_instructions()

    def _bridge_action(self, action):
        """App-level bridge actions from the settings window.

        纯翻译层（架构评审 R5 候选 1）：action 字符串 → intents 意图
        调用；guard 分派/线程纪律/通知/dirty 全在 services/intents 的
        单一归宿（此前此处与菜单回调是两套手写 adapter）。消息经
        WKScriptMessageHandler 到主线程；慢操作由 intents 派 daemon
        线程，窗口与菜单保持响应（#68：跨线程调用安全——
        ConnectionCoordinator 的 _lifecycle_lock 已归状态机所有者）。
        """
        kind = action.get("type")
        if kind == ACTION_RECONNECT_PROXY:
            self._intents.reconnect_proxy_or_forward(
                action.get("tunnel_id"),
                guarded=bool(action.get("if_connected")))
        elif kind == ACTION_STOP_PROXY:
            self._intents.stop_proxy()
        elif kind == ACTION_FORWARD_SESSION:
            tid = action.get("tunnel_id")
            if not tid:
                return
            self._intents.forward_session(tid, action.get("action"))
        elif kind == ACTION_NFS_MOUNT_TOGGLE:
            tid = action.get("tunnel_id")
            name = action.get("name")
            if not tid or not name:
                return
            self._intents.mount(tid, name, action.get("action"))
        elif kind == ACTION_OPEN_PATH and action.get("kind") == "captureDir":
            self.open_capture_dir(None)
        elif kind == ACTION_COPY_AGENT_INSTRUCTIONS:
            self.copy_agent_instructions(None)

    def _shutdown(self):
        """退出清理唯一归宿（幂等）。

        顺序契约：NFS 先卸载（hard 挂载断隧道前必须卸，防 Finder 卡死）
        → 系统代理恢复 → SSH 停止 → 服务线 → 配置服务（后者由
        LifecycleRuntime.quit 持有）。"""
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        # VPN 先停（SIGTERM 优雅路径跑 DNS down 脚本——干净还原系统设置）
        if getattr(self, "_vpn_client", None) is not None:
            self._vpn_client.stop()
        self._mounts.unmount_all()
        self._lifecycle.quit(self._conn.stop_all)

    def quit_app(self, _):
        self._shutdown()
        rumps.quit_application()


if __name__ == "__main__":
    if os.environ.get("MAGIC_PROXY_SMOKE_TEST") == "1":
        logger.info("Magic Stack smoke import OK: v%s", VERSION)
        # mount 域（ADR-007）依赖分析守卫：app.py 顶层 import 会让
        # PyInstaller 把 mount 包编进 PYZ——这里真导入一次，漏收集在
        # 打包冒烟即红，而非装到 /Applications 后首挂载才炸
        from mount import (  # noqa: F401
            coordinator, mount_control, nfs_session, remote_setup)
        # frozen 冒烟（issue #2）：契约解析 + 实际 spawn bundled mitmdump
        # 加载 addon，判据单一归宿在 capture/resources；失败原因直达
        # stderr（windowed 包 logger 不落终端，print 才可见）。
        from capture.resources import (
            CaptureResourcesError, resolve_capture_resources, smoke_capture_boot)
        try:
            ok, detail = smoke_capture_boot(resolve_capture_resources({}))
        except CaptureResourcesError as exc:
            print(f"frozen resource contract FAILED: {exc.msg}", file=sys.stderr)
            raise SystemExit(1)
        if not ok:
            print(f"frozen capture smoke FAILED: {detail}", file=sys.stderr)
            raise SystemExit(1)
        logger.info("frozen capture smoke OK: %s", detail)
    else:
        MagicProxyApp().run()
