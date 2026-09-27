"""资源清单（issue #14）：dev 查找、PyInstaller add-data、packaged smoke
共用的单一真源。src 相对仓库根；dest 相对 bundle 根（平铺为主）。
"""
RESOURCE_MANIFEST = [
    # (src 相对仓库根, dest 相对 bundle 根)
    ("shellui/config_ui.html", "."),
    # i18n 双语 catalog（ADR-012）：shared/i18n 平铺查找（dev 包内 /
    # frozen _MEIPASS 根）
    ("shared/locales/zh-CN.json", "."),
    ("shared/locales/en.json", "."),
    ("docs/agent.md", "."),
    ("docs/examples/suanpan.example.yaml", "."),
    ("capture/ai_capture_addon.py", "."),
    ("assets/MenubarIcon.png", "."),
    ("assets/MenubarIcon-green.png", "."),
    ("assets/MenubarIcon-gray.png", "."),
    ("assets/MenubarIcon-yellow.png", "."),
    # mitmdump 子树单独 add（dist-mitmdump/mitmdump → mitmdump/），不入此清单
]

# 运行时 import 的域包模块（PyInstaller 经 add-data 装入根平铺——
# frozen 下按扁平名 import；漏装即 ModuleNotFoundError）
RUNTIME_MODULES = [
    "app.py", "util.py", "shared/stats.py",
    "tunnel/proxy.py", "tunnel/async_runtime.py", "tunnel/http_framer.py",
    "tunnel/connection_coordinator.py", "shared/subprocess_monitor.py",
    "tunnel/retry_scheduler.py", "tunnel/host_key.py", "tunnel/host_key_flow.py",
    "tunnel/ssh_launch.py",
    "mpconf/config.py", "shared/config_store.py", "mpconf/config_state.py",
    "shared/netloc.py", "shared/provider_auth.py",
    "shared/defaults.py", "shared/identity.py",
    "shellui/menu_builder.py", "shellui/webview_window.py",
    "shellui/log_window.py", "shellui/bridge_protocol.py",
    "capture/capture.py", "capture/capture_controller.py",
    "capture/capture_store.py", "capture/ca_trust.py",
    "capture/chromium_proxy.py", "capture/resources.py",
    "capture/mitmdump_entry.py",
    "sysctl/system_proxy.py", "sysctl/sys_proxy_controller.py",
    "sysctl/sleep_blocker.py", "sysctl/login_item.py", "sysctl/port_check.py",
    "shared/keychain.py", "sysctl/instance_owner.py",
    "services/config_server.py", "services/suanpan_runtime.py",
    "services/claude_code_setup.py", "services/lifecycle_runtime.py",
    "services/balance_usage.py", "services/authenticated_http.py",
    "services/sp_config.py", "services/provider_probe.py",
    "services/usage_stats.py",
]

# 运行时 resource_path 消费的资源名（必须与上面 dest 平铺名一致）
RESOURCE_NAMES = [src.rsplit("/", 1)[-1] for src, _ in RESOURCE_MANIFEST]


def verify_bundle(bundle_root):
    """packaged smoke：核验 bundle 内每个 dest 文件存在。"""
    import os
    missing = []
    for src, dest in RESOURCE_MANIFEST:
        name = src.rsplit("/", 1)[-1]
        if not os.path.isfile(os.path.join(bundle_root, dest, name)):
            missing.append(os.path.join(dest, name))
    return (not missing, missing)
