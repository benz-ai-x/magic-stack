"""专用 NFS 转发会话：一条纯 -L（local_port → 127.0.0.1:2049）承载本
隧道全部 NFS 挂载（NFSv4 单端口，多个导出共享同一条隧道）。

薄子类（ADR-007 收敛）：生命周期编排（三件套/连接序列/僵尸重建/健康
泵）单一归宿在 tunnel/ssh_session.SshSession；本类只持有 NFS 的两件
特定物——本地端口与 _injected_tunnel 投影（用户隧道副本的 forwards
**替换**为仅含 NFS 的一条 -L：用户自己的转发行由他们自己的会话持有，
同端口双进程绑定会因 ExitOnForwardFailure 互顶死循环，prepare 的端口
冲突校验拦同端口配置）。
"""
from tunnel.ssh_session import SshSession
from shared.server_shape import with_service_patch

NFS_REMOTE_PORT = 2049


class NfsSession(SshSession):
    """一条隧道的 NFS 转发会话（纯 -L，无 -D）。"""

    def __init__(self, tunnel_id, local_port, log_sink, tunnel_fn,
                 password_fn):
        self.tunnel_id = tunnel_id
        self.local_port = local_port
        super().__init__(
            log_sink,
            identity_fn=tunnel_fn,
            password_fn=password_fn,
            spawn_fn=self._injected_tunnel,
            probe_port_fn=lambda: self.local_port,
        )

    def _injected_tunnel(self):
        """用户隧道副本 + 仅含 NFS 的 forwards（见模块头：绝不双进程绑同口）。

        v2 回归修复（2026-09-25）：命令构建读 ``services.ssh.forwards``
        （ssh_launch._forwards），替换必须落在 v2 键上——此前写顶层
        ``forwards`` 是 v1 残留，v2 形状下覆盖落空，NFS 会话原样复制
        转发会话的全部 -L，双会话抢绑同口互顶死循环；v1 顶层键一并
        清空，防读时兼容路径吃到旧行。"""
        tunnel = self._identity_fn()
        if tunnel is None:
            return None
        row = {"local_port": self.local_port, "remote_host": "127.0.0.1",
               "remote_port": NFS_REMOTE_PORT}
        # 写侧 copy-on-write 原语（shared/server_shape）——与
        # toggle_forward_row / vpn profile_set 同款惯用法
        return {
            **with_service_patch(tunnel, "ssh", {"forwards": [row]}),
            "forwards": [],
        }
