"""Base class for managed subprocess lifecycle.

SSHMonitor (proxy.py) and CaptureMonitor (capture.py) share the same
start/stop/check/reap pattern. This base extracts the common machinery;
subclasses provide command construction and a ready-probe.
"""
import logging
import subprocess
import threading
import time
from collections import deque

logger = logging.getLogger("magic-proxy.subprocess")


class SubprocessMonitor:
    """Manages a subprocess with stderr capture, crash detection, and ready probing.

    Subclasses override:
        _PROCESS_NAME   — log label (e.g. "SSH", "mitmdump")
        _STATUS_STARTING — status string while waiting for ready
        _STATUS_RUNNING  — status string once ready probe passes
        start(...)       — build command, call _start_process()
        _probe_ready(port) — return True if the service is accepting connections
    """

    _PROCESS_NAME = "subprocess"
    _STATUS_STARTING = "starting"
    _STATUS_RUNNING = "running"
    # 状态全集（#71 S3 单点声明）：子类可覆写 STARTING/RUNNING 词汇
    # （SSHMonitor 改 "connecting"/"connected"），STOPPED/ERROR 恒定
    _STATUS_STOPPED = "stopped"
    _STATUS_ERROR = "error"

    def __init__(self, *, line_sink=None):
        self.process = None
        self._status = self._STATUS_STOPPED
        self._error_msg = ""
        self._cmd_str = ""
        self._log_lines = deque(maxlen=100)
        self._log_lock = threading.Lock()
        self._start_time = 0
        self._log_thread = None
        # Optional sink: each decoded stderr line is forwarded here as it is
        # read (in addition to accumulating in _log_lines). Lets a monitor own
        # its live-log forwarding instead of forcing callers to poll the deque.
        self._line_sink = line_sink

    @property
    def status(self):
        return self._status

    @property
    def error_msg(self):
        return self._error_msg

    @property
    def cmd_str(self):
        return self._cmd_str

    @property
    def log(self):
        with self._log_lock:
            lines = list(self._log_lines)
        return "\n".join(lines[-5:])

    @property
    def elapsed(self):
        if self._start_time and self._status in (self._STATUS_STARTING, self._STATUS_RUNNING):
            return time.monotonic() - self._start_time
        return 0

    # ── public mutators (replace private-member access from app.py) ──

    def set_status(self, status):
        """Set status string (used by host-key trust flow before SSH starts)."""
        self._status = status

    def set_error(self, msg):
        """Set error message (used by host-key trust flow on failure)."""
        self._error_msg = msg

    def snapshot_log_lines(self):
        """Return a thread-safe copy of all accumulated stderr lines."""
        with self._log_lock:
            return list(self._log_lines)

    # ── launch helper (called by subclass start()) ─────────────────

    def _start_process(self, cmd, *, env=None, pass_fds=(), display_cmd=None,
                       capture_stdout=False):
        """Common Popen + stderr reader thread launch. Returns True on success.

        capture_stdout=True（openvpn 用）：子进程诊断走 **stdout**（版本
        banner、管理口 bind 失败等致命错误全在 stdout，stderr 恒空）——
        不捕获则秒退子进程的死因被 DEVNULL 吞掉，monitor 只见
        「starting 永不就绪」（真机定位教训，2026-09-27）。
        """
        self._cmd_str = display_cmd or " ".join(cmd)
        self._status = self._STATUS_STARTING
        self._error_msg = ""
        with self._log_lock:
            self._log_lines.clear()
        self._start_time = time.monotonic()
        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture_stdout
                else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
                pass_fds=pass_fds,
            )
        except OSError as exc:
            self.process = None
            self._status = self._STATUS_ERROR
            self._error_msg = str(exc)
            logger.warning("%s could not start: %s", self._PROCESS_NAME, exc)
            return False
        proc = self.process
        self._log_thread = threading.Thread(
            target=self._read_stderr, args=(proc,), daemon=True,
        )
        self._log_thread.start()
        if capture_stdout and proc.stdout is not None:
            threading.Thread(
                target=self._read_stream, args=(proc, proc.stdout),
                daemon=True,
            ).start()
        logger.info("%s starting: %s", self._PROCESS_NAME, self._cmd_str)
        return True

    # ── stream readers (own their pipe) ────────────────────────────

    def _read_stderr(self, proc):
        self._read_stream(proc, proc.stderr)

    def _read_stream(self, proc, stream):
        try:
            for line in stream:
                decoded = line.decode(errors="replace").rstrip()
                if decoded:
                    with self._log_lock:
                        self._log_lines.append(decoded)
                    if self._line_sink:
                        self._line_sink(decoded)
        except Exception:
            logger.exception("stream reader crashed")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    # ── stop / reap ────────────────────────────────────────────────

    def stop(self, blocking=True):
        """Terminate the subprocess.

        blocking=False (quit path) sends SIGTERM and returns immediately.
        We never close stderr here — the reader thread owns that pipe and
        closes it on EOF. Calling stderr.close() on this thread deadlocks:
        BufferedReader.close() blocks on the same buffer lock the reader
        holds while blocked in read().
        """
        if self.process is None:
            self._status = self._STATUS_STOPPED
            return
        proc = self.process
        log_thread = self._log_thread
        proc.terminate()
        if not blocking:
            self.process = None
            self._status = self._STATUS_STOPPED
            threading.Thread(
                target=self._reap_process, args=(proc, log_thread), daemon=True,
            ).start()
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                logger.warning("%s did not exit after SIGKILL", self._PROCESS_NAME)
        self._wait_log_thread()
        self.process = None
        self._status = self._STATUS_STOPPED

    def _wait_log_thread(self):
        if self._log_thread and self._log_thread.is_alive():
            self._log_thread.join(timeout=1)

    @staticmethod
    def _reap_process(proc, log_thread):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if log_thread and log_thread.is_alive():
            log_thread.join(timeout=1)

    # ── health check ───────────────────────────────────────────────

    def check(self, port):
        """Poll subprocess; probe readiness only while in starting state."""
        if self.process is None:
            return

        ret = self.process.poll()
        if ret is not None:
            self._status = self._STATUS_ERROR
            self._wait_log_thread()
            with self._log_lock:
                lines = list(self._log_lines)
            self._error_msg = "\n".join(lines) or f"{self._PROCESS_NAME} exited with code {ret}"
            logger.warning("%s exited (%s): %s", self._PROCESS_NAME, ret, self._error_msg)
            self.process = None
            return

        if self._status != self._STATUS_STARTING:
            return

        if self._probe_ready(port):
            self._status = self._STATUS_RUNNING
            logger.info("%s ready on port %d", self._PROCESS_NAME, port)

    def _probe_ready(self, port):
        """Override: return True if the service is accepting connections."""
        raise NotImplementedError
