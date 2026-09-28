"""mitmdump subprocess manager (ADR-001 Task 2).

Inherits the shared SubprocessMonitor lifecycle (start/stop/check/reap).
Config flows one-way via env vars to the mitmproxy addon; the addon only
reads those + writes to disk. proxy.py itself is untouched — mitmdump
talks to it purely as an upstream HTTP CONNECT proxy.
"""
import logging
import os
import socket
from capture.capture_store import (DEFAULT_CAPTURE_DIR,
    cleanup_expired_captures, prepare as prepare_capture_dir)
from shared.defaults import DEFAULT_CAPTURE_PORT
from shared.subprocess_monitor import SubprocessMonitor

logger = logging.getLogger("magic-proxy.capture")

PORT_PROBE_TIMEOUT = 0.1  # seconds for starting-state port probe

# Matches ai_capture_addon.py's write_jsonl() naming: <capture_dir>/<local
# date %Y-%m-%d>.jsonl. Anything else in capture_dir is left untouched by

# One-way env-var contract injected into the mitmdump child. The addon reads
# these; capture.py never reads anything back from the addon.
ENV_CAPTURE_DIR = "MAGIC_PROXY_CAPTURE_DIR"
ENV_CAPTURE_RAW_SSE = "MAGIC_PROXY_CAPTURE_RAW_SSE"
ENV_PRESERVE_STREAMING = "MAGIC_PROXY_PRESERVE_STREAMING"

DEFAULT_UPSTREAM = "http://127.0.0.1:8888"
LOOPBACK_HOST = "127.0.0.1"


class CaptureMonitor(SubprocessMonitor):
    """Manages the mitmdump capture-mode subprocess."""

    _PROCESS_NAME = "mitmdump"

    def start(
        self,
        mitmdump_bin: str,
        addon_path: str,
        capture_port: int = DEFAULT_CAPTURE_PORT,
        upstream: str = DEFAULT_UPSTREAM,
        capture_dir: str = None,
        raw_sse: bool = False,
        preserve_streaming: bool = False,
        retention_days: int = 0,
    ):
        """Start the mitmdump capture subprocess."""
        self.stop()

        try:
            resolved_capture_dir = prepare_capture_dir(capture_dir or DEFAULT_CAPTURE_DIR)
        except OSError as exc:
            self.set_status(self._STATUS_ERROR)
            self.set_error(str(exc))
            return False
        try:
            cleanup_expired_captures(resolved_capture_dir, retention_days)
        except Exception:
            logger.exception("capture retention: cleanup pass failed, continuing to start")

        cmd = [
            mitmdump_bin,
            "--mode", f"upstream:{upstream}",
            "--listen-host", LOOPBACK_HOST,
            "--listen-port", str(capture_port),
            "-s", addon_path,
        ]

        env = os.environ.copy()
        env[ENV_CAPTURE_DIR] = resolved_capture_dir
        env[ENV_CAPTURE_RAW_SSE] = "1" if raw_sse else "0"
        env[ENV_PRESERVE_STREAMING] = "1" if preserve_streaming else "0"

        return self._start_process(cmd, env=env)

    def _probe_ready(self, port):
        """Raw TCP connect probe — mitmdump accepts connections when ready."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(PORT_PROBE_TIMEOUT)
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False
        finally:
            s.close()
