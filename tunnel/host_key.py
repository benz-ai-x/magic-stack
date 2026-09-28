"""Explicit SSH host-key enrollment for Magic Stack."""
import os
import fcntl
import re
import stat
import subprocess

from shared import config_store
from shared.server_shape import ssh_node
APP_SECURITY_DIR = os.path.expanduser("~/.magic-proxy")
KNOWN_HOSTS_PATH = os.path.join(APP_SECURITY_DIR, "known_hosts")
_TIMEOUT = 7
_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _validate_host(host):
    if (not host or len(host) > 253 or host.startswith("-")
            or not _HOST_RE.fullmatch(host)):
        raise ValueError("SSH 主机名包含不安全字符")


def _ensure_storage():
    os.makedirs(APP_SECURITY_DIR, mode=0o700, exist_ok=True)
    info = os.lstat(APP_SECURITY_DIR)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise OSError("Magic Stack 安全目录不是普通目录")
    if info.st_uid != os.getuid():
        raise OSError("Magic Stack 安全目录所有者不正确")
    os.chmod(APP_SECURITY_DIR, 0o700)


def _lookup_name(host, port):
    return host if int(port) == 22 else f"[{host}]:{int(port)}"


def inspect(tunnel, force_scan=False):
    """Return (known, scanned_keys, fingerprints, error)."""
    host = str(ssh_node(tunnel).get("host") or "").strip()
    try:
        _validate_host(host)
        _ensure_storage()
    except (ValueError, OSError) as exc:
        return False, "", "", str(exc)
    try:
        port = int(ssh_node(tunnel).get("port", 22))
    except (TypeError, ValueError):
        return False, "", "", "SSH 端口无效"
    if not host or not 1 <= port <= 65535:
        return False, "", "", "SSH 地址或端口无效"

    if not force_scan and os.path.exists(KNOWN_HOSTS_PATH):
        try:
            found = subprocess.run(
                ["ssh-keygen", "-F", _lookup_name(host, port), "-f", KNOWN_HOSTS_PATH],
                capture_output=True, text=True, timeout=_TIMEOUT,
            )
            if found.returncode == 0 and found.stdout.strip():
                return True, "", "", ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, "", "", str(exc)

    try:
        scan = subprocess.run(
            ["ssh-keyscan", "-T", "5", "-p", str(port), host],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "", "", f"无法扫描 SSH 主机密钥：{exc}"
    keys = "\n".join(
        line for line in scan.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if scan.returncode != 0 or not keys:
        return False, "", "", (scan.stderr or "未获取到 SSH 主机密钥").strip()
    try:
        fp = subprocess.run(
            ["ssh-keygen", "-lf", "-"], input=keys + "\n",
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "", "", f"无法计算 SSH 指纹：{exc}"
    if fp.returncode != 0 or not fp.stdout.strip():
        return False, "", "", (fp.stderr or "无法计算 SSH 指纹").strip()
    fingerprints = "\n".join(
        " ".join(line.split()[1:4]) for line in fp.stdout.splitlines() if line.split()
    )
    return False, keys, fingerprints, ""


def accept(keys):
    """Append scanned public keys with private file permissions."""
    if not keys.strip():
        return False
    lock_fd = None
    fd = None
    try:
        _ensure_storage()
        lock_fd = os.open(
            os.path.join(APP_SECURITY_DIR, "known_hosts.lock"),
            os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(KNOWN_HOSTS_PATH, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return False
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fd = None
            fh.write(keys.rstrip("\n") + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except (OSError, ValueError):
        return False
    finally:
        for descriptor in (fd, lock_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    return True


def replace(tunnel, keys):
    """Atomically replace entries for one host after explicit confirmation."""
    host = str(ssh_node(tunnel).get("host") or "").strip()
    lock_fd = None
    try:
        port = int(ssh_node(tunnel).get("port", 22))
        _validate_host(host)
        _ensure_storage()
        lock_fd = os.open(
            os.path.join(APP_SECURITY_DIR, "known_hosts.lock"),
            os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existing = []
        if os.path.exists(KNOWN_HOSTS_PATH):
            fd = os.open(KNOWN_HOSTS_PATH, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                os.close(fd)
                return False
            with os.fdopen(fd, encoding="utf-8") as fh:
                existing = fh.readlines()
        lookup = _lookup_name(host, port)
        kept = [line for line in existing if not line.split() or line.split()[0] != lookup]
        # 末行换行归一（#69 R7）：手编 known_hosts 末行缺 \n 时新 key
        # 会胶接到旧行（刚验证过指纹的新 key 不可达）
        base = "".join(kept)
        if base and not base.endswith("\n"):
            base += "\n"
        text = base + keys.rstrip("\n") + "\n"
        return config_store.atomic_write(KNOWN_HOSTS_PATH, text)
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
