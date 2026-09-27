"""跨域默认值唯一归宿（P1 叶子层）。

抓包默认端口/目录是多方共同需要的配置面知识（mpconf 的 DEFAULT_CONFIG
schema 默认、sysctl 的系统代理指向、capture 的目录准备、app 的菜单回
调）。网关默认端口同样跨 suanpan schema / sp_config 兜底 / lifecycle
端口探测三方出现。此前这些字面量散布各域（抓包侧曾在 capture_store，
迫使 mpconf 与 sysctl 横向 import 整个 capture 域）——现在常量归叶子
层，域间边严格向下（见 tests/test_arch_imports.py）。

capture_store 仍持有抓包命名知识的其余部分（文件名格式、目录管理），
并从这里消费默认值。
"""
import os
from datetime import timedelta, timezone

DEFAULT_CAPTURE_DIR = os.path.expanduser("~/.magic-proxy-captures")
DEFAULT_CAPTURE_PORT = 8080
DEFAULT_GATEWAY_PORT = 9527

# OpenVPN 管理口固定端口（docs/openvpn-client-spec.md §5.2）：sudoers 条目
# 全量钉死 argv 是端口必须固定的前提；固定端口 + Keychain 稳定管理密码
# 换来 app 崩溃重启后对残留 root openvpn 的收养能力（§5.4）
VPN_MANAGEMENT_PORT = 17511

# 端口上界（mp 四端口 / sp listen / 端口转发 / NFS 本地端口共用——
# 通用约束，归叶子层供 mpconf 与 suanpan 的校验器同一取值）
PORT_MAX = 65535

# 本地时区口径（CST，UTC+8）：供应商重置时间显示（balance_usage）与
# 用量日历聚合（usage_stats）共用同一时区约定——分属两个 services
# 模块的知识归叶子层，杜绝各自定义漂移
CST = timezone(timedelta(hours=8))
