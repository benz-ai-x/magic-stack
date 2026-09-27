"""RuntimeProjection —— 运行态投影的叶子层容器（架构评审 R3 候选 2）。

零域知识（叶子层纪律）：字段注解不引用任何域模块——ForwardState /
MountState 等命名投影由各生产者域定义，消费侧按字段形状使用。

app 一处组装（capture/conn/mounts 三个生产者）；LifecycleRuntime →
ConfigServer → _ThreadingHTTPServer 单参数透传——曾经的
capture_state + tunnel_states_fn + mount_states_fn 三参穿三层，收敛
为一个 seam：下一个运行态投影 = 生产者加一个字段。
"""
from typing import NamedTuple


class RuntimeProjection(NamedTuple):
    """跨域运行态的一次快照（按需新鲜组装，不缓存）。"""
    capture_active: bool = False
    forwards: tuple = ()    # [ForwardState]（tunnel 域命名投影）
    mounts: tuple = ()      # [MountState]（mount 域命名投影）
    # VPN 隧道层快照 dict（vpn 域 VpnClient.snapshot——status/tun_ip/错误/
    # 速率；无连接 = None）。全局单条（spec §5.3 不变量），非 per-server。
    vpn: dict | None = None
