"""服务卡一键检测：服务类型探测知识的单一归宿（SERVICE_CARDS 注册表）。

v0.13.0 服务器中心模型的检测面（ADR-011 M2）：每类远程服务一张「卡」
（label + probe 函数），设置窗服务卡按钮、REST /api/server-check、agent
调用方都经同一张卡路由——新增服务类型 = 注册一张卡，检测分发不散落
if 链（对齐 shared/provider_auth.PROVIDER_REGISTRY 的卡式注册表模式）。

探测原语全部复用既有单一归宿（不自建 ssh argv）：
- SSH 可达：tunnel/ssh_launch.probe（与真实隧道同一调用策略）；
- NFS 状态：mount/remote_setup.check_remote（只读，无副作用）；
- OpenVPN 安装态：tunnel/ssh_launch.run_remote 一次性远程命令（无 sudo）。

探针输入归一（host/user/port 守卫 + Keychain 取用）原居 config_server.
_probe_inputs——随服务检测归入本模块而迁来（config_server 反向复用，
保持路由薄层定位）。keychain 以参数注入（探测签名 (server, keychain_)），
调用方与测试各自决定凭据来源。
"""
import logging
import time

from mount import remote_setup
from tunnel import ssh_launch
from shared.server_shape import ssh_node

logger = logging.getLogger("magic-proxy.server-check")

# OpenVPN 安装态探测命令（只读，无 sudo）：__ABSENT__ 显式区分「命令跑通
# 但 openvpn 缺席」与「连接失败」（后者由 run_remote 归 ok=False + error）。
# command -v 的路径回显静默掉——stdout 首个 OpenVPN 行即版本行。
_OPENVPN_CMD = ("command -v openvpn >/dev/null 2>&1 && "
                "openvpn --version 2>&1 | head -1 || echo __ABSENT__")


def probe_inputs(tunnel, keychain_):
    """探针前置：输入守卫 + Keychain 取用（服务卡检测 / test_tunnel /
    test_forward / NFS 远程操作共用）。

    返回 (normalized, password, error)：守卫不过 → (None, "", "<中文短语>")；
    过关 → 探针消费校验归一后的值（strip/int），与历史行为一致——手改
    配置的空白 host 或 "022" 端口不进 ssh argv。
    """
    ssh = ssh_node(tunnel)
    host = str(ssh.get("host") or "").strip()
    user = str(ssh.get("user") or "").strip()
    try:
        port = int(ssh.get("port", 22))
    except (TypeError, ValueError):
        port = 0
    destination = f"{user}@{host}" if user else host
    if not host or not 1 <= port <= 65535 or destination.startswith("-"):
        return None, "", "服务器地址或端口无效"

    password = ""
    if ssh.get("auth_type") == "password":
        password = keychain_.get_password(tunnel)
        if not password:
            return None, "", "钥匙串中没有该服务器的密码，请先保存"

    normalized = {**tunnel, "ssh": {**ssh, "host": host, "user": user,
                                    "port": port}}
    return normalized, password, ""


def probe_ssh(server, keychain_):
    """SSH 可达探测：probe() 全包 + 外层计时归一。

    返回 {"ok": bool, "error": str, "latency_ms": int|None}（失败时
    latency_ms=None）——绝不抛异常。
    """
    normalized, password, error = probe_inputs(server, keychain_)
    if error:
        return {"ok": False, "error": error, "latency_ms": None}
    started = time.monotonic()
    result = ssh_launch.probe(normalized, password=password)
    latency = (int((time.monotonic() - started) * 1000)
               if result.get("ok") else None)
    return {"ok": bool(result.get("ok")),
            "error": str(result.get("error") or ""),
            "latency_ms": latency}


def probe_nfs(server, keychain_):
    """NFS 状态探测（只读）：check_remote 归一到服务卡形状。

    返回 {"ok", "error", "family", "installed", "listening_2049",
    "exports_configured"}——ok=False（连接失败/不支持发行版）时其余字段
    为安全默认值，消费方不必判形。
    """
    failed = {"ok": False, "error": "", "family": "", "installed": False,
              "listening_2049": False, "exports_configured": False}
    normalized, password, error = probe_inputs(server, keychain_)
    if error:
        return {**failed, "error": error}
    result = remote_setup.check_remote(normalized, password=password)
    if not result.get("ok"):
        return {**failed, "error": str(result.get("error") or "")}
    return {"ok": True, "error": "",
            "family": str(result.get("family") or ""),
            "installed": bool(result.get("installed")),
            "listening_2049": bool(result.get("listening")),
            "exports_configured": bool((result.get("exports") or "").strip())}


def _parse_openvpn(stdout):
    """解析安装态探测输出：OpenVPN 版本行（存在即已装，整行为版本串）。"""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("OpenVPN"):
            return True, stripped
    return False, ""


def probe_openvpn(server, keychain_):
    """OpenVPN 安装态探测：run_remote 一次性命令（无 sudo）。

    返回 {"ok", "error", "installed", "version"}——ok=True 表示探测本身
    跑通（installed 报告有无装）；连接失败 → ok=False + error。
    """
    normalized, password, error = probe_inputs(server, keychain_)
    if error:
        return {"ok": False, "error": error, "installed": False,
                "version": ""}
    result = ssh_launch.run_remote(normalized, _OPENVPN_CMD,
                                   password=password)
    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("error") or ""),
                "installed": False, "version": ""}
    installed, version = _parse_openvpn(result.get("stdout") or "")
    return {"ok": True, "error": "", "installed": installed,
            "version": version}


# 服务卡注册表：label 供 UI/agent 展示，probe 统一签名 (server, keychain_)。
SERVICE_CARDS = {
    "ssh": {"label": "SSH 隧道服务", "probe": probe_ssh},
    "nfs": {"label": "NFS 服务", "probe": probe_nfs},
    "openvpn": {"label": "OpenVPN 服务", "probe": probe_openvpn},
}


def check_server(server, keychain_, only=None):
    """编排器：跑请求的服务卡探针，返回 {服务类型: 探测结果}。

    only = SERVICE_CARDS 键之一（单卡）或 None（全部）。每张卡独立包裹
    ——单卡抛异常不连坐其它卡（归一为 ok=False 的中文错误）。
    """
    keys = list(SERVICE_CARDS) if only is None else [only]
    results = {}
    for key in keys:
        try:
            results[key] = SERVICE_CARDS[key]["probe"](server, keychain_)
        except Exception:
            logger.exception("service probe %s crashed", key)
            results[key] = {"ok": False, "error": "检测内部错误"}
    return results
