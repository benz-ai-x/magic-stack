"""OpenVPN management interface 行协议客户端（纯 Python 零依赖，spec §6.2）。

协议形态（源码树 doc/management-notes.txt，2.5–2.7 逐版核对）：

- 行导向文本；命令以 ``\\n`` 结尾，响应 ``SUCCESS:``/``ERROR:``；
- 实时事件以 ``>`` 开头，可随时穿插在命令响应之间；
- 单客户端模型：管理口同时只服务一个客户端，重连前必须关旧 socket；
- 密码握手（TCP + pw-file 形态）：连接后服务端先发 ``ENTER PASSWORD:``
  → 客户端回一行密码 → ``SUCCESS: password is correct`` → ``>INFO:`` 欢迎。

握手成功后立即宣告 ``version 4``——管理命令 ≤3 静默无响应，4 起才有
SUCCESS（openvpn-gui 同款；顺带解锁 2.7 的用户名单问能力）。

转义（username/password 命令参数）：openvpn 配置文件词法——双引号包裹 +
内部 ``\\\\`` → ``\\\\\\\\``、``"`` → ``\\"``。含换行的凭证直接拒绝
（openvpn-gui 同款纪律）。
"""
from __future__ import annotations

import logging
import socket
import threading

logger = logging.getLogger("magic-proxy.vpn-mgmt")

PROTOCOL_VERSION = 4


class ManagementError(Exception):
    """管理口握手/通信失败（含连接被拒/超时/密码错）。"""


def quote_arg(value: str) -> str:
    """管理命令参数转义：openvpn 配置词法（双引号包裹 + 反斜杠/引号转义）。

    不含空白与引号/反斜杠的值原样返回；空串 → ``""``。含换行抛
    ValueError（命令是行协议，换行即碎包——上游必须在入参处拦）。
    """
    if "\n" in value or "\r" in value:
        raise ValueError("newline in management command argument")
    if value == "":
        return '""'
    if any(c in value for c in ' \t"\\'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def parse_state_line(payload: str) -> dict:
    """>STATE 载荷 → 字段 dict。9 列容忍空段（2.4+ 缺席字段保留空位，
    行尾常拖尾逗号——split 后按名取值，绝不按字段数判断）。"""
    fields = (payload or "").split(",")
    name = fields[1].strip() if len(fields) > 1 else ""
    return {
        "ts": fields[0].strip(),
        "name": name,
        "desc": fields[2].strip() if len(fields) > 2 else "",
        "tun_ip": fields[3].strip() if len(fields) > 3 else "",
        "remote_ip": fields[4].strip() if len(fields) > 4 else "",
        "remote_port": fields[5].strip() if len(fields) > 5 else "",
        "local_ip": fields[6].strip() if len(fields) > 6 else "",
        "local_port": fields[7].strip() if len(fields) > 7 else "",
        "tun_ipv6": fields[8].strip() if len(fields) > 8 else "",
    }


def parse_bytecount(payload: str):
    """>BYTECOUNT 载荷 → (in, out)；形状不对返回 None（绝不抛）。"""
    parts = (payload or "").split(",")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def parse_log_line(payload: str) -> dict:
    """>LOG 载荷 → {ts, flags, text}（flags：I/F/N/W/D）。"""
    parts = (payload or "").split(",", 2)
    if len(parts) < 3:
        return {"ts": "", "flags": "", "text": (payload or "")}
    return {"ts": parts[0], "flags": parts[1], "text": parts[2]}


def parse_password_need(payload: str) -> dict:
    """>PASSWORD:Need 载荷 → {kind, need_password, echo, static_challenge}。

    形态：``Need 'Auth' username/password`` / ``Need 'Auth' username``
    （2.7+）/ ``Need 'Auth' username/password SC:<flag>,<提示>`` /
    ``Need 'Private Key' password``。
    """
    text = (payload or "").strip()
    kind, need_password, echo, challenge = "", False, False, ""
    if text.startswith("Need '"):
        end = text.find("'", 6)
        if end > 0:
            kind = text[6:end]
            rest = text[end + 1:].strip()
            # 静态挑战后缀跟在 username/password 之后（management-notes）：
            # Need 'Auth' username/password SC:<flag>,<提示>
            if " SC:" in rest:
                rest, challenge = rest.split(" SC:", 1)
                flag_part = challenge.split(",", 1)[0]
                try:
                    echo = bool(int(flag_part) & 1)
                except ValueError:
                    echo = False
            need_password = "password" in rest
    return {"kind": kind, "need_password": need_password,
            "echo": echo, "challenge": challenge}


class ManagementClient:
    """一个管理口连接 + reader daemon 线程事件分发。

    handlers 是事件名 → 回调的可选映射（缺省全忽略）：
    ``state``(parsed dict)、``log``(parsed dict)、``bytecount``(in, out)、
    ``password_need``(parsed dict)、``password_failed``(str)、``fatal``(str)、
    ``hold``(str)、``disconnected``()。
    """

    def __init__(self, *, password=None, handlers=None,
                 handshake_timeout=5.0, command_timeout=3.0):
        self._password = password or None
        self._handlers = dict(handlers or {})
        self._handshake_timeout = handshake_timeout
        self._command_timeout = command_timeout
        self._sock = None
        self._rfile = None
        self._reader = None
        self._alive = False
        self._leftover = b""
        self._lock = threading.Lock()
        self._resp_event = threading.Event()
        self._resp = (False, "")
        self._disconnect_notified = False

    # ── 连接 / 握手 ──────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        return self._alive

    def connect(self, host, port):
        """连管理口：TCP 建连 → 密码握手 → >INFO 欢迎 → version 4 → 起 reader。

        openvpn --management-hold 下子进程先起管理口再冬眠，本方法可安全
        重试（调用方退避重连）。失败抛 ManagementError。
        """
        try:
            sock = socket.create_connection((host, int(port)),
                                            timeout=self._handshake_timeout)
        except OSError as exc:
            raise ManagementError(f"connect failed: {exc}") from exc
        try:
            leftover = self._handshake(sock)
        except ManagementError:
            try:
                sock.close()
            except OSError:
                pass
            raise
        self._sock = sock
        self._alive = True
        self._leftover = leftover
        self._reader = threading.Thread(
            target=self._read_loop, args=(sock,), daemon=True)
        self._reader.start()
        # version 4 宣告：≤3 的服务端静默、≥4 回 SUCCESS——统一走等待路径
        # 消费掉这条响应。send_and_wait 的应答按到达序配对，若把 version
        # 的 SUCCESS 留在途，它会串位配到下一条命令（真机竞态，评审抓出）；
        # 老服务端不回 → 超时无害。
        self.send_and_wait(f"version {PROTOCOL_VERSION}", timeout=1.0)
        logger.info("management interface attached (%s:%s)", host, port)

    def _handshake(self, sock) -> bytes:
        """同步握手：可选 ENTER PASSWORD → 密码 → SUCCESS → >INFO。

        返回握手期 recv 可能多读的剩余字节——问候之后的推送事件（>STATE
        等）与版本响应可能同批到达，绝不能丢：转交给 reader 线程作种子。
        """
        sock.settimeout(self._handshake_timeout)
        buf = b""
        sent_password = False
        deadline_ok = False
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout as exc:
                raise ManagementError("handshake timeout") from exc
            except OSError as exc:
                raise ManagementError(f"handshake recv failed: {exc}") from exc
            if not chunk:
                raise ManagementError("management closed during handshake")
            buf += chunk
            while b"\n" in buf and not deadline_ok:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").rstrip("\r")
                if text.startswith(">INFO:"):
                    deadline_ok = True
                elif text == "ENTER PASSWORD:" and not sent_password:
                    if not self._password:
                        raise ManagementError(
                            "management requires a password, none configured")
                    sent_password = True
                    sock.sendall(self._password.encode("utf-8") + b"\n")
                elif text.startswith("ERROR:"):
                    # 密码错等服务端断开/超时；这里只记录
                    logger.warning("management handshake error line: %s", text)
            if deadline_ok:
                break
        sock.settimeout(None)
        return buf

    # ── 命令面 ───────────────────────────────────────────────────

    def _send_line(self, line: str):
        with self._lock:
            if self._sock is None:
                raise ManagementError("not connected")
            self._sock.sendall(line.encode("utf-8") + b"\n")

    def send(self, line: str):
        """发送命令（fire-and-forget；事件经 handlers 回调）。"""
        self._send_line(line)

    def send_and_wait(self, line: str, timeout=None):
        """发送命令并等下一条 SUCCESS/ERROR → (ok, text)。

        多行输出的命令（state/log 回放）以事件行形式先行到达，SUCCESS
        只是收尾——回放数据经 handlers 消费，此处只关心成败。
        """
        with self._lock:
            if self._sock is None:
                return False, "not connected"
            self._resp_event.clear()
            self._resp = (False, "")
            self._sock.sendall(line.encode("utf-8") + b"\n")
        if not self._resp_event.wait(timeout or self._command_timeout):
            return False, "timeout"
        return self._resp

    # ── reader 线程 ──────────────────────────────────────────────

    def _read_loop(self, sock):
        try:
            buf = self._leftover  # 握手期多读的字节先于 socket 继续消费
            while self._alive:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").rstrip("\r")
                    if text:
                        self._handle_line(text)
        except OSError:
            pass
        except Exception:  # noqa: BLE001 — reader 线程绝不带崩进程
            logger.exception("management reader crashed")
        finally:
            self._alive = False
            if not self._disconnect_notified:
                self._disconnect_notified = True
                cb = self._handlers.get("disconnected")
                if cb:
                    try:
                        cb()
                    except Exception:  # noqa: BLE001
                        logger.exception("disconnected handler raised")

    def _handle_line(self, text: str):
        if text.startswith(">STATE:"):
            self._emit("state", parse_state_line(text[len(">STATE:"):]))
        elif text.startswith(">LOG:"):
            self._emit("log", parse_log_line(text[len(">LOG:"):]))
        elif text.startswith(">BYTECOUNT:"):
            value = parse_bytecount(text[len(">BYTECOUNT:"):])
            if value is not None:
                self._emit("bytecount", value)
        elif text.startswith(">PASSWORD:Need"):
            self._emit("password_need",
                       parse_password_need(text[len(">PASSWORD:"):]))
        elif text.startswith(">PASSWORD:Verification Failed"):
            self._emit("password_failed",
                       text[len(">PASSWORD:Verification Failed"):].strip())
        elif text.startswith(">FATAL:"):
            self._emit("fatal", text[len(">FATAL:"):].strip())
        elif text.startswith(">HOLD:"):
            self._emit("hold", text[len(">HOLD:"):].strip())
        elif text.startswith("SUCCESS:") or text.startswith("ERROR:"):
            ok = text.startswith("SUCCESS:")
            body = text.split(":", 1)[1].strip()
            self._resp = (ok, body)
            self._resp_event.set()
        # >INFO: 欢迎行 / >ECHO: / END 等在此无需处理

    def _emit(self, name, value):
        cb = self._handlers.get(name)
        if cb:
            try:
                cb(value)
            except Exception:  # noqa: BLE001 — 事件回调绝不带崩 reader
                logger.exception("management handler %s raised", name)

    # ── 关闭 ─────────────────────────────────────────────────────

    def close(self):
        """关闭连接（quit 只断管理会话、openvpn 继续跑——本类同语义）。"""
        self._alive = False
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        reader = self._reader
        if reader and reader.is_alive():
            reader.join(timeout=1.0)
