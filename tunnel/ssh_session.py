"""SshSession —— SSH 会话 deep module（ADR-007 收敛落地）。

「一条 SSH 会话怎么活」的单一归宿：三件套（SSHMonitor + RetryScheduler
+ HostKeyFlow）组装、连接序列（host-key 首连信任 → spawn）、僵尸重建、
每秒健康泵（tick = 到期重连 + check_and_recover）。

消费者（各持薄 interface）：
- ConnectionCoordinator 的转发会话（多活，ADR-005）；
- MountCoordinator 的 NFS 会话（ADR-007，经 spawn_fn 注入单条 -L 副本）。
代理会话（-D，携带暂停/降级策略）仍内联在 ConnectionCoordinator——它
不是逐行镜像，强迁会扩风险面。

两个 fns 的分工：identity_fn 供 host-key 检查与凭据解析（原始隧道），
spawn_fn 供 monitor.start（默认即 identity_fn；NFS 会话覆写为注入副本）。
本类恒为纯 -L 形态（无 -D）；就绪探测口经 probe_port_fn 显式注入
（推导助手 shared.server_shape.first_forward_port）——不留隐式默认，
探测语义一处可寻。
"""

import logging

from tunnel.proxy import SSHMonitor
from tunnel.retry_scheduler import RetryScheduler
from tunnel.host_key_flow import HostKeyFlow

logger = logging.getLogger("magic-proxy.ssh-session")


def check_and_recover(monitor, retry, host_key, probe_port):
    """单会话健康检查（原 ConnectionCoordinator._check_monitor 与
    MountCoordinator._check_monitor 的逐行重复，收敛为一份）。

    #85：error 也放行——每拍继续 handle_error（timer 存活时自去重），
    耗尽退避表后按封顶节奏无限重试，不再永久躺平等手动。
    """
    if monitor.status not in ("connecting", "connected", "stopped", "error"):
        return
    monitor.check(probe_port)
    if monitor.status == "connected":
        retry.reset()
    elif monitor.status == "error":
        if monitor.is_host_key_changed and not host_key.change_prompted:
            host_key.begin_replacement()
        else:
            retry.handle_error()


class SshSession:
    """一条 SSH 会话（纯 -L 转发模式的通用形态）。

    Args:
        log_sink: ssh stderr 行转发（SSHMonitor line_sink）。
        identity_fn: () -> 原始 tunnel dict or None（host-key 检查 + 凭据）。
        password_fn: (tunnel) -> str。
        spawn_fn: () -> 交给 monitor.start 的 tunnel dict or None（默认同
            identity_fn；NFS 会话注入只含 NFS -L 的副本）。
        probe_port_fn: () -> int|None。健康检查的就绪探测口，**显式注入**
            （转发会话用 first_forward_port，NFS 会话用本地端口）——
            曾经的隐式默认推导生产零消费且与在用推导双份，已删。
    """

    def __init__(self, log_sink, identity_fn, password_fn,
                 spawn_fn=None, probe_port_fn=None):
        self.monitor = SSHMonitor(line_sink=log_sink)
        self.retry = RetryScheduler()
        self._identity_fn = identity_fn
        self._spawn_fn = spawn_fn or identity_fn
        self._password_fn = password_fn
        self._probe_port_fn = probe_port_fn
        self.host_key = HostKeyFlow(
            ssh_monitor=self.monitor,
            get_tunnel=identity_fn,
            get_socks5_port=lambda: None,  # 纯 -L 模式无 -D
            get_password=lambda: (
                password_fn(t) if (t := identity_fn()) else ""),
            on_connect=self._start_now,
            on_reconnect=self.connect,
        )

    @property
    def probe_port(self):
        return self._probe_port_fn()

    def connect(self):
        """发起连接序列：重试计数清零 + host-key 信任检查（首连信任流）。"""
        self.retry.cancel()
        self.host_key.start_check()

    def _start_now(self):
        identity = self._identity_fn()
        tunnel = self._spawn_fn()
        if tunnel is None:
            return
        password = self._password_fn(identity) if identity is not None else ""
        self.monitor.start(tunnel, None, password)

    def reconnect_now(self):
        """#86 僵尸重建：connected 主动拆（不等 ServerAlive 判死）再连。"""
        if self.monitor.status == "connecting":
            return
        if self.monitor.status == "connected":
            self.monitor.stop()
        self.connect()

    def stop(self, blocking=True):
        self.retry.cancel()
        self.host_key.cancel()
        self.monitor.stop(blocking=blocking)

    def tick(self):
        """每秒健康泵：到期重连 + 状态检查（check 即收敛的会话半边）。"""
        if self.retry.consume_due() \
                and self.monitor.status in ("stopped", "error"):
            self.connect()
        check_and_recover(self.monitor, self.retry, self.host_key,
                          self.probe_port)
