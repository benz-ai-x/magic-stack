"""SSH 调用策略单一归宿：argv 构建、一次性探针、stderr 失败分类。

「按我们的策略调用 ssh」只在本模块存在一份：

- host-key 三件套（StrictHostKeyChecking=yes + 应用专用 known_hosts +
  GlobalKnownHostsFile=/dev/null）——调用方行为恒等；
- 认证注入：sshpass-via-fd（密码永不出现在 argv / ps）或 key 认证 -i 传参；
- stderr → 中文短语的失败分类表（有序，变更先于未信任）。

调用方：
- tunnel/proxy.py::SSHMonitor.start —— 长驻隧道，消费 build_tunnel_command；
- services/config_server.py::test_tunnel / test_forward —— 一次性探针，
  走 probe() / probe_forward() 全包；
- mount/remote_setup.py —— 远程一键安装，走 run_remote()（一次性命令执行）。
"""
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from tunnel import host_key
from shared.server_shape import enabled_forwards, ssh_node

# 探针的硬上限：ssh 自己的 ConnectTimeout 只管 TCP，这个管其余一切
# （sshpass 提示等待、密钥交换卡住），HTTP 请求绝不无限挂起。
PROBE_TIMEOUT = 15

# run_remote 的默认上限：覆盖发行版探测/exports 应用这类秒级命令；
# 安装类调用（apt/dnf）由调用方显式传更长的 timeout。
REMOTE_TIMEOUT = 60

# sudo -S 失败特征（远程 stderr；-p '' 抑制提示行后只剩这两类）
_SUDO_FAILURE_PHRASES = (
    ("incorrect password attempt", "远程 sudo 认证失败：密码错误"),
    ("a password is required", "远程 sudo 需要密码（NOPASSWD 未配置）"),
    ("not in the sudoers file", "该 SSH 用户没有 sudo 权限"),
)

_FAILURE_PHRASES = (
    # 顺序敏感：密钥已变更的 stderr 同时含 "Host key verification failed"，
    # 更具体的模式必须先命中。
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", "主机密钥已变更，请先从菜单栏处理告警"),
    ("Host key verification failed", "主机密钥未信任，请先从菜单栏连接一次完成信任"),
    ("Permission denied", "认证失败：密钥或密码被拒绝"),
    ("Connection refused", "连接被服务器拒绝"),
    ("Could not resolve hostname", "无法解析服务器地址"),
    ("Connection timed out", "连接超时"),
    ("No route to host", "无法路由到服务器"),
    ("Network is unreachable", "网络不可达"),
)

_HOST_KEY_CHANGED_PHRASE = _FAILURE_PHRASES[0][0]



def describe_failure(stderr):
    """Map raw ssh stderr to a short Chinese phrase for the config UI."""
    text = (stderr or "").strip()
    for needle, phrase in _FAILURE_PHRASES:
        if needle in text:
            return phrase
    first_line = text.splitlines()[0] if text else "未知错误"
    return f"连接失败：{first_line[:120]}"


def host_key_changed(stderr):
    """True when ssh exited because the server's host key changed.

    与 describe_failure 共用同一张有序分类表——调用方不再自行字符串匹配。
    """
    return _HOST_KEY_CHANGED_PHRASE in (stderr or "")


@dataclass
class SshCommand:
    """一次 ssh 调用的完整描述。

    cmd 是真实 exec argv；display_cmd 仅供日志展示（密码 fd 打码为 ***）；
    password_fd 由调用方在 spawn 完成后以 close_password_fd() 释放。
    """

    cmd: list
    display_cmd: str
    destination: str
    pass_fds: tuple = ()
    password_fd: Optional[int] = None

    def close_password_fd(self):
        """释放密码管道读端；幂等，重复调用与 OS 错误都不抛。"""
        if self.password_fd is None:
            return
        try:
            os.close(self.password_fd)
        except OSError:
            pass
        self.password_fd = None


def _destination(tunnel):
    user = ssh_node(tunnel).get("user", "")
    host = ssh_node(tunnel).get("host", "")
    return f"{user}@{host}" if user else host


def _host_key_args():
    return [
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
        "-o", "GlobalKnownHostsFile=/dev/null",
    ]


def _with_auth(tunnel, ssh_args, password, extra_auth_args=()):
    """把认证注入策略套到 ssh_args 上，返回完整的 SshCommand。

    password 认证走 sshpass -d fd（密码经管道传递，永不出现在 argv/ps）；
    key 认证前置 -i（显式 null 兜底为空串，argv 绝不出现 None）。
    extra_auth_args 是模式专属认证参数（探针的 BatchMode /
    NumberOfPasswordPrompts）。
    """
    destination = _destination(tunnel)
    if ssh_node(tunnel).get("auth_type") == "password":
        r_fd, w_fd = os.pipe()
        try:
            os.write(w_fd, (password + "\n").encode())
        except OSError:
            # 写端已废：读端一并关闭再抛出，fd 不泄漏
            try:
                os.close(r_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(w_fd)
        cmd = (["sshpass", "-d", str(r_fd), "ssh"] + list(extra_auth_args)
               + ssh_args)
        display_cmd = " ".join(
            ["sshpass", "-d", "***", "ssh"] + list(extra_auth_args) + ssh_args)
        return SshCommand(cmd=cmd, display_cmd=display_cmd,
                          destination=destination, pass_fds=(r_fd,),
                          password_fd=r_fd)
    key = str(ssh_node(tunnel).get("ssh_key") or "")
    cmd = ["ssh"] + list(extra_auth_args) + ["-i", key] + ssh_args
    return SshCommand(cmd=cmd, display_cmd=" ".join(cmd),
                      destination=destination)


def _port_ok(v):
    """转发端口的防御性判定：真 int（bool 除外）且 1..65535。"""
    return (isinstance(v, int) and not isinstance(v, bool)
            and 1 <= v <= 65535)


def _forward_args(tunnel):
    """本地端口转发（-L）argv 段：绑定地址恒 127.0.0.1（本机回环面）。

    停用行（enabled=False，逐条启停）不进 -L 集合。非法行（端口越界/
    缺字段/非对象）防御性跳过——prepare 校验与 merge 归一双保险下，
    正常流转的配置永不触达跳过分支。
    """
    args = []
    for f in enabled_forwards(tunnel):
        lp, rp = f.get("local_port"), f.get("remote_port")
        if not _port_ok(lp) or not _port_ok(rp):
            continue
        rh = str(f.get("remote_host") or "").strip() or "127.0.0.1"
        args += ["-L", f"127.0.0.1:{lp}:{rh}:{rp}"]
    return args


def build_tunnel_command(tunnel, socks5_port, password=""):
    """长驻隧道的完整调用描述；spawn 由 SSHMonitor 负责。

    socks5_port=None → 纯转发模式（多活模型里的「转发会话」）：跳过 -D，
    只携带 -L。socks5_port 非 None → 代理模式：**恒纯 -D**（ADR-011 修订，
    2026-09-27——代理服务器自己的 forwards 走独立转发会话，-D 会话不再
    搭载 -L 便车），结构上忽略 forwards，不依赖调用方剥隧道副本。
    socks5_port 全局唯一，只有代理隧道（current_tunnel）以代理模式启动，
    其余隧道并行时必须是转发模式——两条 -D 同端口会因
    ExitOnForwardFailure 直接退出。
    """
    port = str(ssh_node(tunnel).get("port", 22))
    dyn_args = [] if socks5_port is None else ["-D", str(socks5_port)]
    fw_args = _forward_args(tunnel) if socks5_port is None else []
    ssh_args = (
        dyn_args + ["-N", "-o", "ExitOnForwardFailure=yes"]
        + fw_args
        + _host_key_args()
        + ["-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
           # #87：跨国链路——更快判死（60s）、不标 DSCP（防中间设备针对性丢包）、
           # 建连自带 3 次重试（缓解瞬时 connect 超时）
           "-o", "IPQoS=none", "-o", "ConnectionAttempts=3"])
    if ssh_node(tunnel).get("compression", True):
        ssh_args.append("-C")
    ssh_args.extend(["-p", port, _destination(tunnel)])
    return _with_auth(tunnel, ssh_args, password)


def probe(tunnel, password=""):
    """一次性连通性探针：与真实隧道同策略地连一次并立即退出。

    绿结果意味着隧道本身也会连上；未信任的主机快速失败，绝不自动信任。
    key 认证跑 BatchMode，passphrase 提示永远挂不住；password 认证复用
    sshpass-via-fd，密码不进 argv。调用方须先做完输入校验与密码取用
    （本函数假设 tunnel 字段已合法）。

    返回 {"ok": True} 或 {"ok": False, "error": "<中文短语>"}——绝不抛异常。
    """
    port = str(ssh_node(tunnel).get("port", 22))
    ssh_args = (["-o", "ConnectTimeout=5"] + _host_key_args()
                + ["-p", port, _destination(tunnel), "true"])
    if ssh_node(tunnel).get("auth_type") == "password":
        extra = ("-o", "NumberOfPasswordPrompts=1")
    else:
        extra = ("-o", "BatchMode=yes")
    sc = None
    try:
        # argv 构建（含 os.pipe/write）也在 try 内：fd 耗尽等 OSError
        # 同样归「无法启动 ssh」，契约「绝不抛异常」无条件成立。
        sc = _with_auth(tunnel, ssh_args, password, extra)
        proc = subprocess.run(
            sc.cmd, capture_output=True, timeout=PROBE_TIMEOUT,
            pass_fds=sc.pass_fds)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "连接超时"}
    except OSError:
        hint = "（密码认证需要 sshpass）" if password else ""
        return {"ok": False, "error": f"无法启动 ssh{hint}"}
    finally:
        if sc is not None:
            sc.close_password_fd()
    if proc.returncode == 0:
        return {"ok": True}
    # bytes + replace decode (not text=True)：SSH stderr 可能携带原始字节，
    # 严格 locale 解码绝不能把探针打崩。
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    return {"ok": False, "error": describe_failure(stderr)}


def probe_forward(tunnel, remote_host, remote_port, password=""):
    """一次性端口转发探针：ssh -W 把 stdio 桥到远端 remote_host:remote_port。

    与 probe()/真实隧道同一套 host-key 三件套与认证策略。stdin=DEVNULL
    立即 EOF：远程端可达则 ssh 干净退出（0），远程拒绝/SSH 层失败则非 0
    且 stderr 可分类。测的是「表单里的意图」——不依赖隧道当前状态，
    未保存的转发行同样可测。返回 {"ok": True, "latency_ms": int} 或
    {"ok": False, "error": "<中文短语>"}——绝不抛异常。
    """
    rh = str(remote_host or "").strip() or "127.0.0.1"
    try:
        rp = int(remote_port)
    except (TypeError, ValueError):
        return {"ok": False, "error": "远程端口无效（须 1..65535）"}
    if not 1 <= rp <= 65535:
        return {"ok": False, "error": "远程端口无效（须 1..65535）"}
    if any(c.isspace() for c in rh) or ":" in rh:
        return {"ok": False, "error": "远程地址无效（暂不支持 IPv6）"}
    port = str(ssh_node(tunnel).get("port", 22))
    ssh_args = (["-o", "ConnectTimeout=5"] + _host_key_args()
                + ["-p", port, "-W", f"{rh}:{rp}", _destination(tunnel)])
    if ssh_node(tunnel).get("auth_type") == "password":
        extra = ("-o", "NumberOfPasswordPrompts=1")
    else:
        extra = ("-o", "BatchMode=yes")
    sc = None
    try:
        sc = _with_auth(tunnel, ssh_args, password, extra)
        started = time.monotonic()
        proc = subprocess.run(
            sc.cmd, capture_output=True, timeout=PROBE_TIMEOUT,
            stdin=subprocess.DEVNULL, pass_fds=sc.pass_fds)
        elapsed_ms = int((time.monotonic() - started) * 1000)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "连接超时"}
    except OSError:
        hint = "（密码认证需要 sshpass）" if password else ""
        return {"ok": False, "error": f"无法启动 ssh{hint}"}
    finally:
        if sc is not None:
            sc.close_password_fd()
    if proc.returncode == 0:
        return {"ok": True, "latency_ms": elapsed_ms}
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    # -W 专属分类先行：远程端口拒绝在 stderr 呈现为 channel open failed，
    # 其中的 "Connection refused" 若落回通用表会被误报成 SSH 层被拒
    if "open failed" in stderr:
        if "refused" in stderr:
            return {"ok": False, "error": f"远程 {rh}:{rp} 拒绝连接（服务未监听？）"}
        if "timed out" in stderr:
            return {"ok": False, "error": f"远程 {rh}:{rp} 连接超时"}
        return {"ok": False, "error": f"无法连到远程 {rh}:{rp}"}
    return {"ok": False, "error": describe_failure(stderr)}


def run_remote(tunnel, command, password="", sudo_password="",
               timeout=REMOTE_TIMEOUT):
    """一次性远程命令执行：与真实隧道同策略地连一次并执行 command。

    与 probe() 同一套 host-key 三件套与认证策略；command 是完整远程
    shell 命令串（调用方负责 shlex.quote 用户输入片段）。sudo_password
    非空时经 ssh stdin 管道传给远程 `sudo -S`——密码只走管道，本地与
    远程 argv 均不出现（sshpass 的 pty 回显由返回前 scrub 兜底，密码
    绝不随 stdout/stderr 回流调用方）。无 sudo_password 时 stdin 接
    DEVNULL，远程任何读取 stdin 的行为立即 EOF。

    返回 {"ok": True, "stdout": str, "stderr": str} 或
    {"ok": False, "error": "<中文短语>", "stdout": str, "stderr": str}
    ——绝不抛异常。
    """
    port = str(ssh_node(tunnel).get("port", 22))
    ssh_args = (["-o", "ConnectTimeout=10"] + _host_key_args()
                + ["-p", port, _destination(tunnel), command])
    if ssh_node(tunnel).get("auth_type") == "password":
        extra = ("-o", "NumberOfPasswordPrompts=1")
    else:
        extra = ("-o", "BatchMode=yes")
    sc = None
    try:
        sc = _with_auth(tunnel, ssh_args, password, extra)
        if sudo_password:
            proc = subprocess.run(
                sc.cmd, capture_output=True, timeout=timeout,
                input=(sudo_password + "\n").encode("utf-8"),
                pass_fds=sc.pass_fds)
        else:
            proc = subprocess.run(
                sc.cmd, capture_output=True, timeout=timeout,
                stdin=subprocess.DEVNULL, pass_fds=sc.pass_fds)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"远程命令超时（>{timeout}s）",
                "stdout": "", "stderr": ""}
    except OSError:
        hint = "（密码认证需要 sshpass）" if password else ""
        return {"ok": False, "error": f"无法启动 ssh{hint}",
                "stdout": "", "stderr": ""}
    finally:
        if sc is not None:
            sc.close_password_fd()
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    if sudo_password:
        stdout = stdout.replace(sudo_password, "***")
        stderr = stderr.replace(sudo_password, "***")
    if proc.returncode == 0:
        return {"ok": True, "stdout": stdout, "stderr": stderr}
    lowered = stderr.lower()
    for needle, phrase in _SUDO_FAILURE_PHRASES:
        if needle in lowered:
            return {"ok": False, "error": phrase,
                    "stdout": stdout, "stderr": stderr}
    return {"ok": False, "error": describe_failure(stderr),
            "stdout": stdout, "stderr": stderr}
