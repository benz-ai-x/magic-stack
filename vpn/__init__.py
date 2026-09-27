"""OpenVPN 客户端域（docs/openvpn-client-spec.md）。

包 openvpn 2.x 社区版二进制 + management interface 路线；域内四模块：
profile（.ovpn 解析/净化）、mgmt_client（管理口行协议）、openvpn_client
（子进程生命周期 + 状态机）、privilege/dns_scripts（sudoers 引导与 root 侧
DNS 脚本）。分层 DAG：只向下依赖 shared/（tests/test_arch_imports.py 钉死）。
"""
