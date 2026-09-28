"""MountCoordinator —— NFS 挂载生命周期协调（tick reconcile，同
ConnectionCoordinator.check_forwards 的「check 即收敛」风格）。

状态机（每秒 tick，主线程调用——快检查在 tick 内联，mount/umount 子
进程全部丢 worker 线程）：

  挂载：desired → 起专用 NFS 会话 → connected → 本地端口探测通 →
        （首次经 sudoers 引导）mount_nfs → mounted
  断线：会话非 connected 且已挂载 → 立即强制卸载（防 Finder 卡死）
        → 恢复且端口通 → 自动重挂
  停止：先卸载后断隧道（顺序反了 hard 挂载会挂死 Finder）

desired 源：用户启停（start_mount/stop_mount）+ auto_mount（apply_
autostarts，应用启动与配置保存后收敛）。desired 是运行时意图，不落盘
——配置里的 auto_mount 才是持久意图。

resolve_mount_dir（local_dir 默认 /Volumes/<name> 的单一归宿）经构造
注入；servers[] 形状访问器自 shared.server_shape 叶子层消费（不再手抄
_nfs_of/_tunnels_by_id——mpconf 仍是同层禁区，形状知识的家在叶子层）。
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

from mount import mount_control
from mount.nfs_session import NfsSession
from shared.server_shape import server_nfs, servers_by_id, ssh_node

logger = logging.getLogger("magic-proxy.nfs-coordinator")

# 挂载失败后的重试间隔（避免每秒重复派发注定失败的 mount）
MOUNT_RETRY_BACKOFF = 30

# mount job 内等隧道就绪的上限（worker 线程内轮询，不占主线程 tick）
MOUNT_WAIT_FOR_TUNNEL = 15

STATUS_UNMOUNTED = "unmounted"
STATUS_MOUNTING = "mounting"
STATUS_MOUNTED = "mounted"
STATUS_UNMOUNTING = "unmounting"
STATUS_ERROR = "error"


class MountState(NamedTuple):
    """单个挂载项的运行态快照（菜单/UI/配置服务共用投影）。

    NamedTuple 保位置兼容；消费面用字段访问——形状契约从位置元组升为
    命名字段。fixable 非空 = 存在结构化修复路径（"exports" = 远端未
    导出，重跑一键安装可修），设置窗据此渲染「修复导出并重挂」按钮；
    非 error 态恒空——挂载成功/reconcile 确认/卸载完成即清。
    """
    tunnel_id: str
    tunnel_name: str
    name: str
    status: str
    error: str
    fixable: str = ""


class _MountState:
    """单个挂载项的运行时状态（worker 线程写，tick 读）。"""

    __slots__ = ("status", "error", "fixable", "busy", "next_retry")

    def __init__(self):
        self.status = STATUS_UNMOUNTED
        self.error = ""
        self.fixable = ""
        self.busy = False
        self.next_retry = 0.0


class MountCoordinator:
    def __init__(self, get_config, get_tunnel_password, ssh_log_sink,
                 resolve_mount_dir, clock=time.monotonic):
        self._get_config = get_config
        self._get_tunnel_password = get_tunnel_password
        self._ssh_log_sink = ssh_log_sink
        self._resolve_dir = resolve_mount_dir
        self._clock = clock
        # 状态机所有者的锁（RLock：teardown 内联调用含锁方法）
        self._lock = threading.RLock()
        self._sessions = {}   # tunnel_id -> NfsSession
        self._desired = set()  # {(tunnel_id, mount_name)}
        self._states = {}      # (tunnel_id, mount_name) -> _MountState
        self._workers = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="NfsMount")

    # ── 配置投影 ─────────────────────────────────────────

    def _tunnels_by_id(self):
        return servers_by_id(self._get_config())

    def _find_row(self, tunnel, name):
        for row in server_nfs(tunnel).get("mounts") or []:
            if isinstance(row, dict) and row.get("name") == name:
                return row
        return None

    # ── 公开状态投影 ─────────────────────────────────────

    def mount_states(self):
        """[MountState]——菜单/UI 快照。列出所有 nfs 配置了挂载的项
        （desired 与否都列）。"""
        with self._lock:
            result = []
            for tid, tunnel in self._tunnels_by_id().items():
                nfs = server_nfs(tunnel)
                if not (nfs.get("enabled") or nfs.get("mounts")):
                    continue
                tname = tunnel.get("name") or ssh_node(tunnel).get("host") or tid
                for row in nfs.get("mounts") or []:
                    if not isinstance(row, dict) or not row.get("name"):
                        continue
                    key = (tid, row["name"])
                    state = self._states.get(key)
                    result.append(MountState(
                        tid, tname, row["name"],
                        state.status if state else STATUS_UNMOUNTED,
                        state.error if state else "",
                        state.fixable if state else ""))
            return result

    def any_mounted(self):
        with self._lock:
            return any(s.status in (STATUS_MOUNTED, STATUS_MOUNTING,
                                    STATUS_UNMOUNTING)
                       for s in self._states.values())

    def any_session_connected(self):
        with self._lock:
            return any(s.monitor.status == "connected"
                       for s in self._sessions.values())

    # ── 生命周期入口 ─────────────────────────────────────

    def apply_autostarts(self):
        """按 auto_mount 收敛 desired（应用启动与配置保存后调用）。"""
        with self._lock:
            for tid, tunnel in self._tunnels_by_id().items():
                nfs = server_nfs(tunnel)
                if not nfs.get("enabled"):
                    continue
                for row in nfs.get("mounts") or []:
                    if isinstance(row, dict) and row.get("name") \
                            and row.get("auto_mount"):
                        self._desired.add((tid, row["name"]))

    def start_mount(self, tunnel_id, name):
        """用户挂载意图：加入 desired 并立即尝试（幂等）。"""
        with self._lock:
            self._desired.add((tunnel_id, name))
            state = self._states.setdefault((tunnel_id, name), _MountState())
            state.next_retry = 0.0
            self._ensure_session_locked(tunnel_id)
            row = self._row_locked(tunnel_id, name)
            if row is not None:
                self._dispatch_mount_locked(tunnel_id, name, row)

    def stop_mount(self, tunnel_id, name):
        """用户卸载意图：移出 desired；已挂载则派发卸载（阻塞等待不超菜单）。"""
        with self._lock:
            self._desired.discard((tunnel_id, name))
            self._dispatch_unmount_locked(tunnel_id, name)
            if not any(k[0] == tunnel_id for k in self._desired):
                self._teardown_tunnel_locked(tunnel_id)

    def reconnect_now(self):
        """唤醒事件：NFS 会话僵尸重建（与 ConnectionCoordinator 同拍）。"""
        with self._lock:
            for session in list(self._sessions.values()):
                session.reconnect_now()

    def unmount_all(self, timeout=8):
        """退出路径：先卸载（阻塞等待上限 timeout 秒）后断会话。"""
        import concurrent.futures as _futures
        futures = []
        with self._lock:
            for key, state in self._states.items():
                if state.status in (STATUS_MOUNTED, STATUS_MOUNTING):
                    futures.append(
                        self._workers.submit(self._unmount_job, key))
            for session in list(self._sessions.values()):
                session.stop(blocking=False)
            self._sessions.clear()
            self._desired.clear()
        if futures:
            _futures.wait(futures, timeout=timeout)
    # ── tick reconcile ──────────────────────────────────

    def tick(self):
        """每秒收敛：会话健康/重试 + 挂载收敛。主线程调用，绝不阻塞。"""
        with self._lock:
            tunnels = self._tunnels_by_id()
            self._reconcile_sessions(tunnels)
            self._reconcile_mounts(tunnels)

    def _reconcile_sessions(self, tunnels):
        # desired 的隧道补建会话（apply_autostarts 只改 desired）；多余
        # 的会话 teardown（隧道删/nfs 关/desired 清空）
        tids = {k[0] for k in self._desired} | set(self._sessions)
        for tid in sorted(tids):
            tunnel = tunnels.get(tid)
            nfs = server_nfs(tunnel)
            session_wanted = (
                tunnel is not None and bool(nfs.get("enabled"))
                and any(k[0] == tid for k in self._desired))
            if not session_wanted:
                self._teardown_tunnel_locked(tid)
                continue
            session = self._sessions.get(tid)
            if session is None:
                self._sessions[tid] = self._new_session_locked(
                    tid, nfs.get("local_port"), tunnels)
                continue
            # 端口改动 → 重建会话（tick 收敛，无需显式重连入口）
            if session.local_port != nfs.get("local_port"):
                session.stop()
                self._sessions[tid] = self._new_session_locked(
                    tid, nfs.get("local_port"), tunnels)
                continue
            # 每秒健康泵（到期重连 + 健康检查）单一归宿在 SshSession.tick
            session.tick()

    def _reconcile_mounts(self, tunnels):
        table = mount_control.nfs_mounts()   # 每拍一次，本拍内共享
        now = self._clock()
        for key in sorted(self._desired):
            tid, name = key
            tunnel = tunnels.get(tid)
            if tunnel is None:
                continue  # 隧道已删：会话收敛段已 teardown
            row = self._find_row(tunnel, name)
            if row is None:
                continue  # 改名/删行：desired 留待用户重新挂载
            state = self._states.setdefault(key, _MountState())
            mount_dir = self._resolve_dir(row)
            mounted_now = mount_dir in table
            session = self._sessions.get(tid)
            connected = bool(session
                             and session.monitor.status == "connected")
            if state.busy:
                continue
            if mounted_now and connected:
                state.status = STATUS_MOUNTED
                state.error = ""
                state.fixable = ""
                continue
            if mounted_now and not connected:
                # 隧道断开：hard 挂载立即强制卸载（防 Finder 卡死）
                state.busy = True
                state.status = STATUS_UNMOUNTING
                self._submit(self._unmount_job, key)
                continue
            if connected and not mounted_now:
                # monitor connected ⇒ 本拍 -L 本地端口探测已过（SSHMonitor
                # ._probe_ready），无需二次探测
                if now >= state.next_retry:
                    self._dispatch_mount_locked(tid, name, row)
                continue
            if not connected:
                state.status = STATUS_UNMOUNTED
                state.error = ""
                state.fixable = ""
        self._sweep_stale_mounts(tunnels, table)

    def _sweep_stale_mounts(self, tunnels, table):
        """陈旧挂载清扫（崩溃/泄漏恢复）：配置里已不在 desired 的挂载点
        仍挂在 mount 表上——上个实例异常退出留下的 hard 挂载（断链路的
        死挂载会让 Finder 卡死）→ 卸载收敛。只碰本应用配置声明的目录。"""
        for tid, tunnel in tunnels.items():
            for row in server_nfs(tunnel).get("mounts") or []:
                if not isinstance(row, dict) or not row.get("name"):
                    continue
                key = (tid, row["name"])
                if key in self._desired:
                    continue
                state = self._states.setdefault(key, _MountState())
                if state.busy:
                    continue
                if self._resolve_dir(row) in table:
                    state.busy = True
                    state.status = STATUS_UNMOUNTING
                    self._submit(self._unmount_job, key)
                    logger.info("清扫陈旧挂载：%s（%s）", row["name"],
                                self._resolve_dir(row))

    # ── 内部（调用方持锁）────────────────────────────────

    def _row_locked(self, tunnel_id, name):
        tunnel = self._tunnels_by_id().get(tunnel_id)
        return self._find_row(tunnel, name) if tunnel else None

    def _new_session_locked(self, tunnel_id, local_port, tunnels):
        session = NfsSession(
            tunnel_id, local_port, self._ssh_log_sink,
            tunnel_fn=lambda tid=tunnel_id: self._tunnels_by_id().get(tid),
            password_fn=self._get_tunnel_password)
        session.connect()
        logger.info("NFS 会话启动：%s (本地端口 %s)", tunnel_id, local_port)
        return session

    def _ensure_session_locked(self, tunnel_id):
        tunnels = self._tunnels_by_id()
        tunnel = tunnels.get(tunnel_id)
        nfs = server_nfs(tunnel)
        if tunnel is None or not nfs.get("enabled"):
            return
        session = self._sessions.get(tunnel_id)
        if session is None:
            self._sessions[tunnel_id] = self._new_session_locked(
                tunnel_id, nfs.get("local_port"), tunnels)
        elif session.monitor.status in ("stopped", "error"):
            session.connect()  # 显式意图绕过退避，立即重连

    def _teardown_tunnel_locked(self, tunnel_id):
        session = self._sessions.pop(tunnel_id, None)
        if session is not None:
            session.stop()
            logger.info("NFS 会话停止：%s", tunnel_id)

    def _dispatch_mount_locked(self, tunnel_id, name, row):
        state = self._states.setdefault((tunnel_id, name), _MountState())
        if state.busy:
            return
        state.busy = True
        state.status = STATUS_MOUNTING
        state.error = ""
        self._submit(self._mount_job, tunnel_id, name, dict(row))

    def _dispatch_unmount_locked(self, tunnel_id, name):
        state = self._states.setdefault((tunnel_id, name), _MountState())
        mount_dir = self._current_dir(tunnel_id, name)
        if mount_dir is None or not mount_control.is_mounted(mount_dir):
            return
        if state.busy:
            return
        state.busy = True
        state.status = STATUS_UNMOUNTING
        self._submit(self._unmount_job, (tunnel_id, name))

    def _submit(self, fn, *args):
        """worker 提交：退出期 executor 已关闭时静默吞（tick 不因竞态崩）。"""
        try:
            self._workers.submit(fn, *args)
        except RuntimeError:
            logger.debug("NFS worker 提交被拒（应用退出中）")

    def _current_dir(self, tunnel_id, name):
        row = self._row_locked(tunnel_id, name)
        return self._resolve_dir(row) if row else None

    # ── worker jobs（锁外执行；回锁只做状态写）───────────

    def _mount_job(self, tunnel_id, name, row):
        key = (tunnel_id, name)
        nfs = server_nfs(self._tunnels_by_id().get(tunnel_id))
        local_port = nfs.get("local_port")
        mount_dir = self._resolve_dir(row)
        error = ""
        # 引导在前：macOS 26 起 /Volumes 是 root:wheel 0755——挂载点必须
        # 经管理员脚本创建（install_sudoers 顺带 mkdir，家目录路径则免弹窗）
        ok, err = mount_control.install_sudoers([mount_dir])
        if not ok:
            error = f"管理员授权未完成：{err}"
        if not error:
            ok, err = mount_control.ensure_mountpoint(mount_dir)
            if not ok:
                error = err
        if not error and not self._wait_tunnel(local_port):
            error = "等待 NFS 隧道就绪超时（连接未建立）"
        fixable = ""
        if not error:
            r = mount_control.mount_nfs(local_port, row.get("remote_path"),
                                        mount_dir)
            if not r["ok"]:
                error = r["error"]
                fixable = r.get("fixable", "")
        with self._lock:
            state = self._states.setdefault(key, _MountState())
            state.busy = False
            if error:
                state.status = STATUS_ERROR
                state.error = error
                state.fixable = fixable
                state.next_retry = self._clock() + MOUNT_RETRY_BACKOFF
                logger.warning("NFS 挂载失败 %s/%s：%s", tunnel_id, name,
                               error)
            else:
                state.status = STATUS_MOUNTED
                state.error = ""
                state.fixable = ""
                logger.info("NFS 挂载成功：%s → %s", row.get("remote_path"),
                            mount_dir)

    def _wait_tunnel(self, local_port, timeout=MOUNT_WAIT_FOR_TUNNEL):
        """worker 线程内等 -L 本地端口就绪（start_mount 时隧道可能还在连）。"""
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if mount_control.probe_local_port(local_port, timeout=0.5):
                return True
            time.sleep(0.5)
        return False

    def _unmount_job(self, key):
        tunnel_id, name = key
        with self._lock:
            row = self._row_locked(tunnel_id, name)
            mount_dir = self._resolve_dir(row) if row else None
        if mount_dir:
            r = mount_control.force_unmount(mount_dir)
            if not r["ok"]:
                logger.warning("NFS 卸载失败 %s：%s", mount_dir, r["error"])
        with self._lock:
            state = self._states.setdefault(key, _MountState())
            state.busy = False
            state.status = STATUS_UNMOUNTED
            state.error = ""
            state.fixable = ""
