"""VpnClient——openvpn 子进程生命周期 + 管理口状态机（spec §3.3 时序）。

SubprocessMonitor 的第三类消费者（SSH / mitmdump / openvpn）：子进程层
健康（进程在跑、管理口可达）归基类；隧道层状态（CONNECTED/RECONNECTING/
EXITING 与凭证事件）归本类 ``vpn`` 投影——UI 只消费隧道层。

日志面：openvpn 默认日志走 stdout（基类 DEVNULL），管理口 ``log on all``
订阅是唯一日志源——>LOG 行进 ``_log_lines``（日志窗消费）+ line_sink。

错误面：结构化错误码（auth_failed/cipher_mismatch/…），本域零文案——
services/UI adapter 映射 i18n。用户主动停止绝不标记 error。
"""
from __future__ import annotations

import logging
import re
import socket
import subprocess
import threading
import time
from collections import deque

from shared.subprocess_monitor import SubprocessMonitor
from vpn.mgmt_client import ManagementClient, ManagementError, quote_arg

logger = logging.getLogger("magic-proxy.vpn")

# ── 错误码（结构化；UI 层映射 i18n 文案）──────────────────────────
ERR_AUTH_FAILED = "auth_failed"            # 凭据错（重问上限后放弃）
ERR_AUTH_REQUIRED = "auth_required"        # 需要凭证但 provider 未给
ERR_AUTH_CHALLENGE = "auth_challenge_unsupported"  # CRV1/SC 挑战（v1 不支持）
ERR_CIPHER = "cipher_mismatch"             # data-ciphers 协商失败（旧服务器）
ERR_CERT_EXPIRED = "cert_expired"          # 服务器证书过期
ERR_CERT_INVALID = "cert_invalid"          # 证书校验/身份钉扎失败
ERR_TLS = "tls_error"                      # TLS 握手（多为网络/防火墙）
ERR_UNREACHABLE = "unreachable"            # 解析失败/连接拒绝
ERR_SERVER_EXIT = "server_exit"            # 服务器主动断开
ERR_CRASHED = "crashed"                    # 进程意外退出且无更细分类
ERR_MGMT_LOST = "mgmt_lost"                # 管理口丢失且进程尚活
ERR_MGMT_ATTACH = "mgmt_attach_failed"     # 子进程起了管理口迟迟不可达

# >LOG 文本 → 错误码（顺序敏感：具体在前，兜底在后；openvpn 真实日志语料）
_LOG_CLASSIFIERS = (
    (re.compile(r"AUTH_FAILED, ?Data channel cipher negotiation failed", re.I),
     ERR_CIPHER),
    (re.compile(r"VERIFY ERROR.*certificate has expired", re.I), ERR_CERT_EXPIRED),
    (re.compile(r"VERIFY (ERROR|X509NAME ERROR)", re.I), ERR_CERT_INVALID),
    (re.compile(r"TLS Error: TLS key negotiation failed|TLS handshake failed", re.I),
     ERR_TLS),
    (re.compile(r"Cannot resolve host|RESOLVE_ERROR|Connection refused", re.I),
     ERR_UNREACHABLE),
    (re.compile(r"AUTH_FAILED", re.I), ERR_AUTH_FAILED),
)


def classify_log(text: str) -> str:
    """openvpn 日志行 → 错误码（空串 = 无分类）。auth 失败经管理口
    PASSWORD 事件单独计数，此表主要服务 >LOG/>FATAL 兜底路径。"""
    for pattern, kind in _LOG_CLASSIFIERS:
        if pattern.search(text or ""):
            return kind
    return ""


def adopt_stale_openvpn(mgmt_password, port, *, timeout=2.0):
    """孤儿收养原语（spec §5.4）：固定管理口上的残留 root openvpn →
    经管理口 SIGTERM 优雅收尸。sudo 之下的 root openvpn 不随 sudo 死——
    process.terminate 只杀得到 sudo，孤儿会永远占住管理口（真机教训：
    一次 attach 失败后所有后续连接全撞死在端口占用）。
    无孤儿/不可达/密码不符 → False；已发送 SIGTERM → True。绝不抛。"""
    try:
        client = ManagementClient(password=mgmt_password, handlers={},
                                  handshake_timeout=timeout)
        client.connect("127.0.0.1", port)
        try:
            client.send_and_wait("signal SIGTERM", timeout=timeout)
        finally:
            client.close()
        logger.info("stale openvpn adopted (SIGTERM via management)")
        return True
    except Exception:
        return False


class VpnState:
    """隧道层状态投影（进程层 status 之外的单一真相）。"""

    def __init__(self):
        self.status = "idle"        # idle/connecting/connected/reconnecting/exiting/stopped/error
        self.tun_ip = ""
        self.since = ""             # CONNECTED 的 STATE 时间戳
        self.error_kind = ""
        self.error_text = ""

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "tun_ip": self.tun_ip,
            "since": self.since,
            "error_kind": self.error_kind,
            "error_text": self.error_text,
        }


class VpnClient(SubprocessMonitor):
    """一台服务器的 openvpn 连接（全局至多一条活跃——spec §5.3 不变量）。

    ``full_cmd`` 由 vpn/privilege.build_full_command 产出（sudo -n + 钉死
    argv），本类不拼特权命令——它只负责生命周期与协议。
    """

    _PROCESS_NAME = "openvpn"

    def __init__(self, *, full_cmd, mgmt_port, mgmt_password=None,
                 credentials=None, on_state_change=None, on_error=None,
                 bytecount_interval=1, attach_timeout=10.0,
                 max_auth_retries=3, line_sink=None):
        super().__init__(line_sink=line_sink, capture_stdout=True)
        self._full_cmd = list(full_cmd)
        self._mgmt_port = int(mgmt_port)
        self._mgmt_password = mgmt_password
        self._credentials = credentials          # () -> (user, password) | None
        self._on_state_change = on_state_change
        self._on_error = on_error
        self._bytecount_interval = bytecount_interval
        self._attach_timeout = attach_timeout
        self._max_auth_retries = max_auth_retries

        self.vpn = VpnState()
        self._mgmt = None
        self._user_stop = False
        self._auth_failures = 0
        self._log_error_kind = ""
        self._fatal_text = ""
        self._samples = deque(maxlen=12)         # (monotonic, in_total, out_total)
        self._last_in = None
        self._last_out = None
        self._base_in = 0
        self._base_out = 0

    # ── 启动 / 停止 ──────────────────────────────────────────────

    def start(self):
        """拉子进程 + 后台线程重试连管理口（hold 语义保证事件零丢失）。
        起前先收养：管理口若有残留 openvpn（上次异常退出的 root 孤儿）
        先 SIGTERM 清场，等端口让位再 spawn——否则新进程 bind 失败且
        死因走 stdout 不可见。"""
        self.stop()
        # 清场收养（端口空时 connect 立即 refused，零成本）
        if self._mgmt_password:
            adopt_stale_openvpn(self._mgmt_password, self._mgmt_port)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    s = socket.create_connection(
                        ("127.0.0.1", self._mgmt_port), timeout=0.2)
                    s.close()
                    time.sleep(0.3)
                except OSError:
                    break
        self._user_stop = False
        self._auth_failures = 0
        self._log_error_kind = ""
        self._fatal_text = ""
        self.vpn = VpnState()
        self.vpn.status = "connecting"
        if not self._start_process(self._full_cmd):
            self.vpn.status = "error"
            self.vpn.error_kind = "start_failed"
            self.vpn.error_text = self.error_msg
            return False
        threading.Thread(target=self._attach_mgmt, daemon=True).start()
        return True

    def _attach_mgmt(self):
        client = None
        deadline = time.monotonic() + self._attach_timeout
        while time.monotonic() < deadline:
            proc = self.process
            if proc is not None and proc.poll() is not None:
                return  # 进程已死——check() 的崩溃路径收尾，不重复报错
            try:
                client = ManagementClient(
                    password=self._mgmt_password,
                    handlers={
                        "state": self._on_state,
                        "log": self._on_log,
                        "bytecount": self._on_bytecount,
                        "password_need": self._on_password_need,
                        "password_failed": self._on_password_failed,
                        "fatal": self._on_fatal,
                        "disconnected": self._on_mgmt_disconnected,
                    })
                client.connect("127.0.0.1", self._mgmt_port)
                break
            except ManagementError:
                client = None
                time.sleep(0.2)
        if client is None:
            # 诊断尾巴：attach 失败时把 openvpn 输出尾部带上（stdout 捕获
            # 后 bind 失败/配置错误一目了然——真机曾在此全盲）
            tail = "\n".join(self.snapshot_log_lines()[-6:])
            self._fail(ERR_MGMT_ATTACH, tail)
            return
        self._mgmt = client
        # 初始化序列（openvpn-gui OnReady 同款）：hold off + release（旗标
        # 跨重启持久，off 让 SIGHUP 后不再挂）→ 原子订阅（on all = 历史
        # 回放 + 实时）→ 流量
        client.send_and_wait("hold off")
        client.send_and_wait("hold release")
        client.send_and_wait("state on all")
        client.send_and_wait("log on all")
        client.send_and_wait(f"bytecount {self._bytecount_interval}")
        logger.info("openvpn management session initialized")

    def stop(self, blocking=True, *, timeout=6.0):
        """优雅断开：管理口 SIGTERM（down 脚本跑全）→ 等进程退出 → 兜底杀。

        EXITING 只是「退出进行中」——必须等进程真正退出才算断开完成
        （spec §2.2）。

        blocking 契约与基类同（R7-C1）：False=quit 路径，SIGTERM 发出
        即返回，进程等待与收养兜底交后台 daemon 线程——此前主线程裸
        阻塞（mgmt 2s + wait 6s + terminate 5s + 收养）最坏 ~15s 的
        quit 挂脸病根。root openvpn 收到 SIGTERM 自行跑 down 脚本退
        出，不依赖本进程等待。
        """
        self._user_stop = True
        client, self._mgmt = self._mgmt, None
        if client is not None and client.alive:
            try:
                client.send_and_wait("signal SIGTERM", timeout=2.0)
            except (ManagementError, OSError):
                logger.warning("mgmt SIGTERM failed, falling back")
        if not blocking:
            if client is not None:
                client.close()
            proc = self.process
            if proc is not None:
                self.process = None
                threading.Thread(target=self._quit_reap, args=(proc,),
                                 daemon=True).start()
            self._status = self._STATUS_STOPPED
            if self.vpn.status != "error":
                self.vpn.status = "stopped"
            self._notify_change()
            return
        proc = self.process
        if proc is not None:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        if client is not None:
            client.close()
        if self.process is not None:
            super().stop()
        else:
            self._status = self._STATUS_STOPPED
        # 兜底收养：attach 从未成功时（client 为 None）上面的 SIGTERM 不
        # 可达——sudo 被 terminate 杀掉后 root openvpn 成孤儿继续占管理
        # 口。收养 SIGTERM 之（幂等：进程已死则连接 refused 直接跳过）。
        if self._mgmt_password:
            adopt_stale_openvpn(self._mgmt_password, self._mgmt_port)
        if self.vpn.status != "error":
            self.vpn.status = "stopped"
        self._notify_change()

    def _quit_reap(self, proc):
        """quit 路径的后台收尾：等进程退出 + 收养兜底（daemon 线程，
        进程退出时被杀亦无碍——SIGTERM 已发出，残留由下次启动的
        清场收养兜底）。"""
        try:
            proc.wait(timeout=8.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        if self._mgmt_password:
            try:
                adopt_stale_openvpn(self._mgmt_password, self._mgmt_port)
            except Exception:  # noqa: BLE001
                logger.exception("quit adoption failed")

    # ── 健康检查（进程层）────────────────────────────────────────

    def _probe_ready(self, port):
        """进程级就绪：管理口可连（hold 语义下子进程先开管理口再冬眠）。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(0.3)
            s.connect(("127.0.0.1", int(port or self._mgmt_port)))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def check(self, port=None):
        """tick 对账：基类管进程存活，本方法把意外退出翻译成隧道层错误。"""
        prev = self._status
        super().check(self._mgmt_port if port is None else port)
        if (prev != self._STATUS_ERROR and self._status == self._STATUS_ERROR
                and not self._user_stop):
            self.vpn.status = "error"
            # 分类优先级：>LOG/>FATAL 已归出的具体码 > 兜底 crashed
            self.vpn.error_kind = self._log_error_kind or ERR_CRASHED
            self.vpn.error_text = self._fatal_text or self.error_msg
            self._close_mgmt_quietly()
            self._notify_error()

    # ── 管理口事件 ───────────────────────────────────────────────

    def _on_state(self, parsed):
        name = parsed.get("name", "")
        if name == "CONNECTED":
            if parsed.get("desc") == "SUCCESS":
                self.vpn.status = "connected"
                self.vpn.tun_ip = parsed.get("tun_ip", "")
                self.vpn.since = parsed.get("ts", "")
                self._auth_failures = 0
            # ROUT_ERROR 等异常描述：不置绿，交给 LOG 分类报错
        elif name == "RECONNECTING":
            if self.vpn.status in ("connected", "connecting"):
                self.vpn.status = "reconnecting"
        elif name == "EXITING":
            if not self._user_stop:
                self.vpn.status = "exiting"
        elif self.vpn.status == "idle":
            self.vpn.status = "connecting"
        self._notify_change()

    def _on_log(self, parsed):
        text = parsed.get("text", "")
        if not text:
            return
        self._emit_log_line(text)
        if not self._log_error_kind:
            kind = classify_log(text)
            if kind:
                self._log_error_kind = kind
                logger.info("openvpn log classified: %s", kind)

    def _on_bytecount(self, pair):
        now = time.monotonic()
        new_in, new_out = pair
        if self._last_in is not None and (new_in < self._last_in
                                          or new_out < self._last_out):
            # 计数器随重连归零——折叠进基数，跨重连总量单调（Tunnelblick 实践）
            self._base_in += self._last_in
            self._base_out += self._last_out
        self._last_in, self._last_out = new_in, new_out
        self._samples.append((now, self._base_in + new_in,
                              self._base_out + new_out))

    def _on_password_need(self, parsed):
        if parsed.get("challenge"):
            # CRV1/SC 挑战 v1 不支持——协议面已核实，给可读错误而非挂死
            self._fail(ERR_AUTH_CHALLENGE)
            return
        creds = None
        if self._credentials is not None:
            try:
                creds = self._credentials()
            except Exception:  # noqa: BLE001 — 凭证 provider 绝不带崩 reader
                logger.exception("credentials provider raised")
        user = (creds or ("", ""))[0]
        password = (creds or ("", ""))[1]
        client = self._mgmt
        if client is None:
            return
        kind = parsed.get("kind", "Auth")
        if kind == "Private Key":
            if not password:
                self._fail(ERR_AUTH_REQUIRED)
                return
            client.send(f'password "Private Key" {quote_arg(password)}')
            return
        if parsed.get("need_password"):
            if not password:
                self._fail(ERR_AUTH_REQUIRED)
                return
            client.send(f'username "Auth" {quote_arg(user)}')
            client.send(f'password "Auth" {quote_arg(password)}')
        else:
            client.send(f'username "Auth" {quote_arg(user)}')

    def _on_password_failed(self, rest):
        self._auth_failures += 1
        logger.warning("openvpn auth verification failed (%d): %s",
                       self._auth_failures, rest)
        if self._auth_failures >= self._max_auth_retries:
            self._fail(ERR_AUTH_FAILED)

    def _on_fatal(self, text):
        self._fatal_text = text
        if not self._log_error_kind:
            self._log_error_kind = classify_log(text) or "fatal"

    def _on_mgmt_disconnected(self):
        if self._user_stop or self.vpn.status in ("exiting", "error", "stopped"):
            return
        # 管理口先于进程死掉（openvpn 退出路径正常会先 EXITING 再 EOF）——
        # 标记错误并异步停进程，避免 reader 线程里做 join 类阻塞
        self._fail(ERR_MGMT_LOST)

    # ── 失败收尾 ─────────────────────────────────────────────────

    def _fail(self, kind, text=""):
        if self.vpn.status == "error":
            return  # 首错保留，不叠加
        self.vpn.status = "error"
        self.vpn.error_kind = kind
        self.vpn.error_text = text or self._fatal_text
        self._notify_error()
        threading.Thread(
            target=self.stop, daemon=True).start()

    def _close_mgmt_quietly(self):
        client, self._mgmt = self._mgmt, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _notify_change(self):
        if self._on_state_change:
            try:
                self._on_state_change(self.vpn.snapshot())
            except Exception:  # noqa: BLE001
                logger.exception("on_state_change raised")

    def _notify_error(self):
        self._notify_change()
        if self._on_error:
            try:
                self._on_error(self.vpn.error_kind, self.vpn.error_text)
            except Exception:  # noqa: BLE001
                logger.exception("on_error raised")

    # ── 投影 ─────────────────────────────────────────────────────

    def traffic_snapshot(self) -> dict:
        """累计字节 + 窗口速率（samples 滑动平均，Tunnelblick 实践）。"""
        if not self._samples:
            return {"bytes_in": self._base_in, "bytes_out": self._base_out,
                    "rate_in": 0, "rate_out": 0}
        first_t, first_in, first_out = self._samples[0]
        last_t, last_in, last_out = self._samples[-1]
        dt = last_t - first_t
        if dt <= 0:
            rate_in = rate_out = 0
        else:
            rate_in = int((last_in - first_in) / dt)
            rate_out = int((last_out - first_out) / dt)
        return {"bytes_in": last_in, "bytes_out": last_out,
                "rate_in": rate_in, "rate_out": rate_out}

    def snapshot(self) -> dict:
        """RuntimeProjection.vpn 的单一来源（生产者加字段——spec §7.3）。"""
        snap = self.vpn.snapshot()
        snap.update(self.traffic_snapshot())
        return snap
