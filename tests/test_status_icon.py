import os

import pytest
from PIL import Image

from shellui.menu_builder import (
    STATUS_ICON_GREEN_RESOURCE,
    STATUS_ICON_RESOURCE,
    _menubar_color,
)


@pytest.mark.parametrize(
    ("ssh", "paused", "vpn", "expected"),
    [
        # 蓝 = SSH 已连接（图标本体即蓝）
        ("connected", False, "idle", "blue"),
        ("connected", False, "stopped", "blue"),
        # 绿 = VPN 已连接（互斥模型下 VPN 优先——收尾窗口以 VPN 为准）
        ("stopped", False, "connected", "green"),
        ("connected", False, "connected", "green"),
        # 黄 = 连接中 / 暂停 / VPN 过渡态
        ("connecting", False, "idle", "yellow"),
        ("connected", True, "idle", "yellow"),
        ("stopped", False, "connecting", "yellow"),
        ("stopped", False, "reconnecting", "yellow"),
        ("stopped", False, "exiting", "yellow"),
        # 灰 = 无连接（SSH error / VPN error·stopped·idle 均未连接）
        ("stopped", False, "idle", "gray"),
        ("error", False, "idle", "gray"),
        ("", False, "idle", "gray"),
        ("stopped", False, "error", "gray"),
    ],
)
def test_menubar_color_language(ssh, paused, vpn, expected):
    """主图标四色语义（用户拍板 2026-09-27）：灰=无连接 / 蓝=SSH /
    绿=VPN / 黄=连接中或暂停。"""
    assert _menubar_color(ssh, paused, vpn) == expected


@pytest.mark.parametrize(
    "resource",
    [STATUS_ICON_RESOURCE, STATUS_ICON_GREEN_RESOURCE],
    ids=["blue(full-color)", "green"],
)
def test_menubar_icon_is_valid(resource):
    with Image.open(os.path.join("assets", resource)) as icon:
        assert icon.size == (256, 256)
        assert icon.mode == "RGBA"
        alpha = icon.getchannel("A")
        assert alpha.getbbox() is not None
        assert alpha.getextrema() == (0, 255)
