"""OpenVPN profile 落盘存取（spec §4——profile_set 布尔的写者）。

profile 正文内嵌用户私钥，属机密：不进 config JSON，存独立文件
``<vpn_profiles_dir>/<server_id>.ovpn``（0600，经 config_store 原子写
管线——唯一安全写入口）。目录经 PATHS 注册表取用，测试单点重定向
（patch.dict）。

职责边界：本模块只管字节落盘/读取/删除；解析/净化归 vpn/profile.py，
``profile_set`` 布尔的翻转归保存流（services adapter，M2 接线）。
"""
from __future__ import annotations

import logging
import os

from shared import config_store

logger = logging.getLogger("magic-proxy.vpn-profile-store")


def profile_path(server_id: str) -> str:
    """server_id → profile 文件路径（id 形如 t-<sha1>[#n]，本就文件名安全；
    防御性替换路径分隔符，绝不因 id 形状越出注册目录）。"""
    safe = str(server_id or "").replace("/", "-").replace("\\", "-").strip()
    return os.path.join(config_store.PATHS["vpn_profiles_dir"],
                        f"{safe or 'default'}.ovpn")


def save_profile(server_id: str, text: str) -> bool:
    """原子写 profile（0600）。调用方应先经 sanitize_profile 净化。"""
    return config_store.atomic_write(profile_path(server_id), text or "",
                                     mode=0o600)


def load_profile(server_id: str) -> str:
    """读 profile；缺席/读失败一律 ''（绝不抛）。"""
    try:
        with open(profile_path(server_id), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def profile_exists(server_id: str) -> bool:
    return os.path.isfile(profile_path(server_id))


def delete_profile(server_id: str) -> bool:
    """删 profile（服务器删除时清理）；条目不存在视为成功。"""
    path = profile_path(server_id)
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        logger.warning("profile delete failed: %s", path)
        return False
