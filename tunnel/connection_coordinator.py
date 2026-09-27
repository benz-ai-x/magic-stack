"""Connection lifecycle coordinator: SSH tunnel + proxy runtime + retry + host-key.

Owns the connection state machine that was previously scattered across MagicProxyApp.
The App delegates start/stop/reconnect/pause to this module.

多活模型（v0.9 起）：代理服务器（current_server，携带 -D 的唯一会话，本类
全部既有状态机照旧）+ 任意多条并行「转发会话」（纯 -L 无 -D，SshSession
实例，各自持有 monitor/retry/host-key 三件套）。会话生命周期的编排单一
归宿在 tunnel/ssh_session.SshSession（ADR-007 收敛：转发会话与 NFS 会话
原是两份逐行镜像）。

Interface:
  start()           — start proxy background + SSH connection sequence
  handle_retry()    — check retry scheduler, connect if due (call before icon)
  check_ssh()       — SSH status check + host-key handling (call after icon)
  check_forwards()  — 转发会话的 tick 半边（SshSession.tick：到期重连 +
                      健康检查；check 即 reconcile）
  start_forward(id) / stop_forward(id) — 转发会话启停
  restart_forward(id, cfg_fn)          — 重载配置后重建指定转发会话
  apply_autostarts()                   — 按 forward_autostart 收敛补启
  restart(cfg_fn)   — full stop + config reload + restart（旧代理降级续跑）
  cancel()          — cancel in-flight connection
  toggle_pause()    — pause/resume; returns new paused state（仅代理会话）
  stop_all()        — stop everything for quit

Properties: ssh, paused, proxy_running, current_server, socks5_port,
            any_connected, forward_sessions
"""
from __future__ import annotations

import logging
import threading
from typing import NamedTuple

from tunnel.proxy import ProxyRuntime, SSHMonitor
from tunnel.retry_scheduler import RetryScheduler
from tunnel.host_key_flow import HostKeyFlow
from tunnel.ssh_session import SshSession, check_and_recover
from shared.server_shape import (
    enabled_forwards, first_forward_port, proxy_server, server_by_id,
    ssh_service,
)
from shared.stats import Stats

logger = logging.getLogger("magic-proxy.connection")

class ForwardState(NamedTuple):
    """一条转发会话的运行态快照（菜单/UI/配置服务共用投影）。

    NamedTuple 保位置兼容（既有解包不破）；消费面用字段访问——
    形状契约从「位置元组猜形状」升为命名字段。
    """
    tunnel_id: str
    name: str
    status: str


class ConnectionCoordinator:
    """Own SSH tunnel + proxy runtime + retry + host-key lifecycle."""

    def __init__(
        self,
        stats: Stats,
        ssh_log_sink,
        get_config,
        get_tunnel_password,
    ):
        self._ssh = SSHMonitor(line_sink=ssh_log_sink)
        self._proxy_runtime = ProxyRuntime(stats)
        self._retry = RetryScheduler()
        # 状态机所有者的锁（#68）：桥接重连 daemon 线程与主线程 tick 并发
        # 打进 stop/check/restart——锁归模块，不归调用方纪律（app.py 的
        # 「owns its locking」注释曾证伪）。RLock：restart 内联调
        # start_ssh / _start_background，同线程重入合法。
        self._lifecycle_lock = threading.RLock()
        self._proxy_running = False
        self._paused = False
        self._get_config = get_config
        self._get_tunnel_password = get_tunnel_password
        self._host_key = HostKeyFlow(
            ssh_monitor=self._ssh,
            get_tunnel=lambda: self.current_server,
            get_socks5_port=lambda: self.socks5_port,
            get_password=lambda: (
                self._get_tunnel_password(self.current_server)
                if self.current_server else ""
            ),
            on_connect=self._start_proxy_ssh,
            on_reconnect=self.start_ssh,
        )
        # 多活转发会话注册表：tunnel_id → SshSession
        self._forward_sessions = {}
        self._ssh_log_sink = ssh_log_sink

    # ── config-derived properties ───────────────────────

    @property
    def _config(self):
        return self._get_config()

    @property
    def current_server(self):
        """代理服务器（v2 schema：proxy_server_id 唯一真相，首条回退）——
        形状访问器单一归宿在 shared.server_shape（对未经 merge 的裸
        配置也稳）。"""
        return proxy_server(self._config)

    @property
    def socks5_port(self):
        return self._config.get("socks5_port", 1080)

    # ── public properties ───────────────────────────────

    @property
    def ssh(self):
        return self._ssh

    @property
    def paused(self):
        return self._paused

    @property
    def proxy_running(self):
        return self._proxy_running

    @property
    def any_connected(self):
        """任一会话已连接（防睡眠等聚合判定的真相源）。"""
        if self._ssh.status == "connected":
            return True
        return any(s.monitor.status == "connected"
                   for s in self._forward_sessions.values())

    @property
    def any_forward_session_connected(self):
        """是否有转发会话在服务（防睡眠的暂停豁免判定）。"""
        return any(s.monitor.status == "connected"
                   for s in self._forward_sessions.values())

    @property
    def proxy_server_id(self):
        t = self.current_server
        return t.get("id") if t else None

    def forward_sessions(self):
        """转发会话快照 [ForwardState]——菜单/UI 投影用。"""
        return [ForwardState(tid, s.monitor.current_name or tid,
                             s.monitor.status)
                for tid, s in self._forward_sessions.items()]

    # ── lifecycle ───────────────────────────────────────

    def start(self):
        """Start proxy background + initiate SSH connection sequence."""
        self._start_background()
        self.start_ssh()

    def start_ssh(self):
        with self._lifecycle_lock:
            self._retry.cancel()
            self._paused = False
            self._host_key.start_check()

    def handle_retry(self):
        """Check retry scheduler; connect if due. Call before setting status icon."""
        if self._retry.consume_due():
            self._retry_connect()

    def check_ssh(self):
        """Check SSH status and handle errors. Call after setting status icon.

        非阻塞（#68 复核）：restart 持锁跨有界 join（≤~11s），1s tick
        若阻塞等锁会把菜单卡死——try-lock 拿不到就跳过本拍（状态下拍
        再收敛；单实例 tick 是主线程唯一调用方，跳拍安全）。
        """
        if not self._lifecycle_lock.acquire(blocking=False):
            return
        try:
            if self._paused:
                return
            check_and_recover(
                self._ssh, self._retry, self._host_key, self.socks5_port)
        finally:
            self._lifecycle_lock.release()

    def check_forwards(self):
        """转发会话的 tick 半边（tick 调用，后于图标）。

        每会话走 SshSession.tick（到期重连 + 健康检查合一——原
        handle_retry_forwards / check_forwards 两拍收敛为一拍；转发会话
        不喂主图标，两拍拆分本就只服务代理会话语义）。隧道被删 /
        forwards 被清空的会话在此收敛停掉（check 即 reconcile）。
        """
        if not self._lifecycle_lock.acquire(blocking=False):
            return
        try:
            for tunnel_id in list(self._forward_sessions):
                session = self._forward_sessions[tunnel_id]
                tunnel = self._server_by_id(tunnel_id)
                if tunnel is None or not enabled_forwards(tunnel):
                    del self._forward_sessions[tunnel_id]
                    session.stop()
                    logger.info("转发会话收敛停止：%s（无隧道或无启用中的转发规则）",
                                tunnel_id)
                    continue
                session.tick()
        finally:
            self._lifecycle_lock.release()

    def _server_by_id(self, tunnel_id):
        return server_by_id(self._config, tunnel_id)

    # ── 转发会话生命周期（多活） ─────────────────────────

    def start_forward(self, tunnel_id):
        """启动一条纯 -L 转发会话（服务层按服务器自治，ADR-011 修订：
        代理服务器同样走独立会话——-D 会话恒纯 -D，不再搭载便车）。
        返回 (ok, reason)。拒绝条件：服务器不存在 / 无启用中的转发规则。
        """
        with self._lifecycle_lock:
            tunnel = self._server_by_id(tunnel_id)
            if tunnel is None:
                return False, "服务器不存在"
            if not enabled_forwards(tunnel):
                return False, "该服务器没有启用中的端口转发规则"
            if tunnel_id in self._forward_sessions:
                session = self._forward_sessions[tunnel_id]
                if session.monitor.status in ("stopped", "error"):
                    session.connect()
                return True, ""
            session = SshSession(
                self._ssh_log_sink,
                identity_fn=lambda tid=tunnel_id: self._server_by_id(tid),
                password_fn=self._get_tunnel_password,
                probe_port_fn=lambda tid=tunnel_id:
                    first_forward_port(self._server_by_id(tid)))
            self._forward_sessions[tunnel_id] = session
            session.connect()
            logger.info("转发会话启动：%s", tunnel.get("name", tunnel_id))
            return True, ""

    def stop_forward(self, tunnel_id, blocking=True):
        """停掉一条转发会话（幂等）。"""
        with self._lifecycle_lock:
            session = self._forward_sessions.pop(tunnel_id, None)
            if session is None:
                return
            session.stop(blocking=blocking)
            logger.info("转发会话停止：%s", tunnel_id)

    def restart_forward(self, tunnel_id, reload_config_fn):
        """重载配置后重建指定转发会话（单会话重连的唯一归宿）。

        显式重连语义：会话存在即重建——error/stopped/退避态同（评审
        Spec-A：曾按 was_alive==connected 门控，error 态点「重新连接」
        只停不连成死按钮）。守卫语义（未连接绝不拉起）在
        restart_forward_async 单一归宿，不会把未运行的会话送进来。
        """
        with self._lifecycle_lock:
            session = self._forward_sessions.get(tunnel_id)
            if session is None:
                return False
            session.stop()
            reload_config_fn()
            tunnel = self._server_by_id(tunnel_id)
            if tunnel is None or not enabled_forwards(tunnel):
                del self._forward_sessions[tunnel_id]
                return True
            session.connect()
            return True

    @property
    def proxy_connected(self) -> bool:
        """代理会话守卫谓词：仅 connected（不含 connecting——与保存流
        同判，review c-1）。菜单翻转/桥接自动应用共用。"""
        return self.ssh.status == "connected"

    def forward_connected(self, tunnel_id) -> bool:
        """转发会话守卫谓词：仅该会话 status=="connected" 算在跑——
        error/退避态的滞留会话（stop_forward 才 pop）不算，绝不因配置
        翻转/保存后自动应用被拉起（架构评审 C1：三处手写守卫的单一
        归宿）。"""
        return any(s.tunnel_id == tunnel_id and s.status == "connected"
                   for s in self.forward_sessions())

    def restart_forward_async(self, tunnel_id, reload_config_fn, *,
                              guarded=True,
                              thread_name="ForwardRebuild") -> bool:
        """守卫重建的单一入口：daemon 线程跑 restart_forward（阻塞最多
        ~10s 的子进程 join 不挂菜单/桥接线程）。

        guarded=True（配置翻转/保存后自动应用）：仅该会话已连接才重建
        ——否则跳过并记日志；guarded=False 为显式重连（会话存在即重建，
        Spec-A 语义）。返回是否派发了重建。
        """
        if guarded and not self.forward_connected(tunnel_id):
            logger.info("转发会话守卫跳过重建：%s 未连接", tunnel_id)
            return False
        threading.Thread(
            target=self.restart_forward,
            args=(tunnel_id, reload_config_fn),
            name=thread_name, daemon=True).start()
        return True

    def apply_autostarts(self):
        """按 services.ssh.autostart 收敛补启（app 启动与配置重载后调用）。
        ADR-011 修订：代理服务器不再跳过——它的转发与 -D 接入各自独立。"""
        for t in self._config.get("servers", []):
            if not (isinstance(t, dict) and ssh_service(t).get("autostart")):
                continue
            tid = t.get("id")
            if tid and tid not in self._forward_sessions \
                    and enabled_forwards(t):
                self.start_forward(tid)

    def handle_reconnect_trigger(self):
        """#86：唤醒等外部事件 → 立即重连（跳过退避）。

        只做提前触发，不改状态机语义：connected 视为僵尸链路主动重建
        （不等 ServerAlive 判死）；connecting 让现有流程收敛；其余直接
        走 start_ssh（内部 cancel 重试计数 + host-key 检查）。
        多活：唤醒断了所有隧道的 TCP——转发会话同样僵尸重建。
        """
        with self._lifecycle_lock:
            if not self._paused:
                status = self._ssh.status
                if status != "connecting":
                    if status == "connected":
                        self._ssh.stop()
                    self.start_ssh()
            for session in list(self._forward_sessions.values()):
                session.reconnect_now()

    def reconnect_forwards_now(self):
        """接入层事件（VPN established 等）→ 服务层僵尸重建：全部转发
        会话 reconnect_now（与唤醒同语义——路由翻转后健康会话也可能是
        半死 TCP，主动重建比等 keepalive 判死稳）。**只碰转发会话**，
        绝不拉起 -D（接入互斥——这是它与 handle_reconnect_trigger 的
        根本差异，后者会把代理半边一起重建）。"""
        with self._lifecycle_lock:
            for session in list(self._forward_sessions.values()):
                session.reconnect_now()

    def restart(self, reload_config_fn):
        """Full stop + config reload + restart（多活版）。

        只重启 -D 接入会话（ADR-011 修订：接入层/服务层分离）；转发会话
        与 NFS 会话是服务层，各自存活——被删/无 forwards 的由
        check_forwards 收敛，autostart 的由 apply_autostarts 补启，
        「旧代理降级为转发会话」的便车补偿随 -L 便车退役一并消失。
        """
        with self._lifecycle_lock:
            self._retry.cancel()
            self._host_key.cancel()
            self._ssh.stop()
            self._proxy_running = False
            self._proxy_runtime.stop()
            reload_config_fn()
            self._start_background()
            self.start_ssh()
            self.apply_autostarts()

    def stop_access(self, blocking=True):
        """接入层切换（ADR-011 修订，2026-09-27）：只停 -D 代理会话
        （含重试调度 / host-key 流程 / 本地代理运行时）——转发会话与
        NFS 会话属服务层，不在此面（此前无此入口，cancel/stop_all 均
        为全停）。"""
        with self._lifecycle_lock:
            self._retry.cancel()
            self._host_key.cancel()
            self._ssh.stop(blocking=blocking)
            self._proxy_running = False
            self._proxy_runtime.stop()
            logger.info("access (-D) stopped; service sessions untouched")

    def toggle_pause(self):
        """Pause/resume proxy. Returns new paused state."""
        with self._lifecycle_lock:
            self._paused = not self._paused
            if self._paused:
                # timeout=0: signal the worker and return immediately —
                # joining here blocks the menu callback for up to 5 s (#40).
                self._proxy_runtime.stop(timeout=0)
                self._proxy_running = False
            else:
                self._start_background()
                if self._ssh.status in ("stopped", "error"):
                    self.start_ssh()
            return self._paused

    def stop_all(self):
        """Stop SSH + proxy for quit (non-blocking)."""
        with self._lifecycle_lock:
            self._retry.cancel()
            self._host_key.cancel()
            self._ssh.stop(blocking=False)
            for session in list(self._forward_sessions.values()):
                session.stop(blocking=False)
            self._forward_sessions.clear()
            self._proxy_runtime.stop()
            self._proxy_running = False

    # ── internals ───────────────────────────────────────

    def _start_background(self):
        if self._proxy_runtime.running:
            return
        proxy_config = {
            "socks5_port": self.socks5_port,
            "http_listen_port": self._config["http_listen_port"],
        }
        self._proxy_running = self._proxy_runtime.start(proxy_config)

    def _retry_connect(self):
        if self._paused:  # #85：暂停即停止重连（menu 语义）
            return
        if self._ssh.status in ("connected", "connecting"):
            return
        self._start_proxy_ssh()

    def _start_proxy_ssh(self):
        """启动代理会话的 ssh（host-key 首连与重试共用）——恒纯 -D
        （ADR-011 修订：代理服务器自己的 forwards 走独立转发会话）。"""
        tunnel = self.current_server
        if tunnel:
            self._ssh.start(tunnel, self.socks5_port,
                            self._get_tunnel_password(tunnel))
