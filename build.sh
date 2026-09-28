#!/bin/bash
# Build Magic Stack.app with PyInstaller (bundles mitmdump for capture mode)
set -e

# Version. Bump this when cutting a new release; also tag git with v$VERSION.
VERSION="0.15.0"
MAIN_PYTHON_BIN="${MAIN_PYTHON_BIN:-python3.12}"

if ! command -v "$MAIN_PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: $MAIN_PYTHON_BIN not found. Install Python 3.12 or set MAIN_PYTHON_BIN." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# mitmdump packaging (ADR-001 capture mode). Bundled by DEFAULT as of Task 5
# finalization -- capture mode is now a shipped feature, not opt-in build
# tooling (was gated behind --with-mitmdump during the Task 1 Go/No-Go
# spike).
#
# Go/No-Go result (Task 1 spike): PyInstaller PASSES. mitmproxy ships its
# own PyInstaller hooks (mitmproxy/utils/pyinstaller/,
# mitmproxy_rs/_pyinstaller/) that PyInstaller auto-discovers -- zero manual
# --hidden-import / custom hooks needed. Verified: packaged mitmdump started
# inside a nested, ad-hoc-codesigned .app-like bundle, MITM'd a real HTTPS
# request routed through proxy.py's upstream CONNECT tunnel (proxy.py
# untouched), and an addon captured the decrypted plaintext body. Full
# evidence: progress/backend-dev.md (Task 1 + Task 5 entries).
#
# NOTE (verify-before-assert finding, Task 1): mitmproxy 12.x requires
# Python >=3.12, NOT >=3.10 as ADR-001's Global Constraints / version table
# currently state (confirmed against live PyPI metadata: mitmproxy 11.1.0+
# and all 12.x releases declare Requires-Python >=3.12; only 11.0.2
# supports 3.10). Also: CVE-2025-23217 (the ADR's stated reason to avoid
# 11.x) is a GitHub Security Advisory confirmed mitmweb-only ("The
# mitmproxy and mitmdump tools are unaffected") -- irrelevant to this
# project, which only ships mitmdump. This build step therefore targets
# Python 3.12, pending tech-lead's ADR-001/ADR-000 revision (Task 5 Step 3,
# explicitly out of scope for this script -- see progress/backend-dev.md).
# This does NOT change the main app build below, which still targets the
# existing >=3.9 floor.
# ---------------------------------------------------------------------------
build_mitmdump() {
    echo "--- Building bundled mitmdump (ADR-001 capture mode) ---"
    rm -rf dist-mitmdump build-mitmdump

    local py_bin="${MITM_PYTHON_BIN:-python3.12}"
    if ! command -v "$py_bin" >/dev/null 2>&1; then
        echo "ERROR: $py_bin not found. Install: brew install python@3.12" >&2
        echo "       (override interpreter: MITM_PYTHON_BIN=/path/to/python3.12 bash build.sh)" >&2
        exit 1
    fi

    local venv=".build-venv-mitmdump"
    rm -rf "$venv"
    "$py_bin" -m venv "$venv"
    # shellcheck source=/dev/null
    source "$venv/bin/activate"
    pip install -q --upgrade pip
    pip install -q --require-hashes -r requirements-lock.txt

    python -m PyInstaller \
        --onedir \
        --name mitmdump \
        --distpath dist-mitmdump \
        --workpath build-mitmdump \
        --specpath build-mitmdump \
        capture/mitmdump_entry.py

    deactivate
    echo "mitmdump build complete: dist-mitmdump/mitmdump/mitmdump"
}

echo "=== Building Magic Stack.app v${VERSION} ==="

# Clean previous build (main app only -- mitmdump's dist-mitmdump/
# build-mitmdump are cleaned inside build_mitmdump so the two steps don't
# clobber each other's output).
rm -rf build dist ./*.spec

build_mitmdump

# Build main app, bundling mitmdump's onedir output at Resources/mitmdump/
# (matches app.py's _resolve_mitmdump_bin() frozen-mode lookup).
echo "--- Building Magic Stack.app ---"

# Main app venv: 从带 hashes 的 requirements-lock.txt 安装（issue #14）——
# dev requirements 不决定发布成品；lock 已是主构建依赖的完整超集
# （rumps/pyobjc/Suanpan/mitmproxy/Pillow/PyInstaller 全部覆盖）。
MAIN_VENV=".build-venv-main"
rm -rf "$MAIN_VENV"
"$MAIN_PYTHON_BIN" -m venv "$MAIN_VENV"
# shellcheck source=/dev/null
source "$MAIN_VENV/bin/activate"
pip install -q --upgrade pip
pip install -q --require-hashes -r requirements-lock.txt

# Generate the menu-bar state icon if missing (Pillow is in the venv).  The
# production app icon is icons/magic-ai-router-macos-v2.icns (see APP_ICON
# below); the menu-bar state icon is loaded and tinted dynamically by
# menu_builder.py.
if [ ! -f assets/MenubarIcon.png ]; then
    python tools/generate_icon.py
fi

APP_ICON="icons/magic-ai-router-macos-v2.icns"
if [ ! -f "$APP_ICON" ]; then
    echo "ERROR: production app icon not found: $APP_ICON" >&2
    exit 1
fi

# Build-time stamp (MMDDHHMM) bundled into the app; the About menu shows it as
# a version suffix (util.build_stamp). Removed after PyInstaller copies it so
# a stale stamp never shadows the dev-mode mtime fallback.
date +%m%d%H%M > build_time.txt

# tomlkit 仅被散装 services/claude_code_setup.py（--add-data，不经静态
# 分析）import——必须显式收集，否则包内 Codex 配置缺依赖（ADR-010）
python -m PyInstaller \
    --windowed \
    --name "Magic Stack" \
    --add-data "build_time.txt:." \
    --add-data "shellui/config_ui.html:." \
    --add-data "shared/locales/zh-CN.json:." \
    --add-data "shared/locales/en.json:." \
    --add-data "docs/agent.md:." \
    --add-data "docs/examples/suanpan.example.yaml:." \
    --add-data "capture/ai_capture_addon.py:." \
    --add-data "assets/MenubarIcon.png:." \
    --add-data "assets/MenubarIcon-green.png:." \
    --add-data "assets/MenubarIcon-gray.png:." \
    --add-data "assets/MenubarIcon-yellow.png:." \
    --add-data "util.py:." \
    --add-data "shared/stats.py:." \
    --add-data "tunnel/proxy.py:." \
    --add-data "tunnel/async_runtime.py:." \
    --add-data "tunnel/http_framer.py:." \
    --add-data "tunnel/connection_coordinator.py:." \
    --add-data "shared/subprocess_monitor.py:." \
    --add-data "tunnel/retry_scheduler.py:." \
    --add-data "tunnel/host_key.py:." \
    --add-data "tunnel/host_key_flow.py:." \
    --add-data "tunnel/ssh_launch.py:." \
    --add-data "mpconf/config.py:." \
    --add-data "shared/config_store.py:." \
    --add-data "mpconf/config_state.py:." \
    --add-data "shared/netloc.py:." \
    --add-data "shared/provider_auth.py:." \
    --add-data "shellui/menu_builder.py:." \
    --add-data "shellui/webview_window.py:." \
    --add-data "shellui/log_window.py:." \
    --add-data "shellui/bridge_protocol.py:." \
    --add-data "capture/capture.py:." \
    --add-data "capture/capture_controller.py:." \
    --add-data "capture/capture_store.py:." \
    --add-data "capture/ca_trust.py:." \
    --add-data "capture/chromium_proxy.py:." \
    --add-data "capture/resources.py:." \
    --add-data "capture/mitmdump_entry.py:." \
    --add-data "sysctl/system_proxy.py:." \
    --add-data "sysctl/sys_proxy_controller.py:." \
    --add-data "sysctl/sleep_blocker.py:." \
    --add-data "sysctl/login_item.py:." \
    --add-data "sysctl/port_check.py:." \
    --add-data "shared/keychain.py:." \
    --add-data "shared/defaults.py:." \
    --add-data "shared/identity.py:." \
    --add-data "sysctl/instance_owner.py:." \
    --add-data "services/config_server.py:." \
    --add-data "services/suanpan_runtime.py:." \
    --add-data "services/claude_code_setup.py:." \
    --add-data "services/lifecycle_runtime.py:." \
    --add-data "services/balance_usage.py:." \
    --add-data "services/sp_config.py:." \
    --add-data "services/authenticated_http.py:." \
    --add-data "services/provider_probe.py:." \
    --add-data "services/usage_stats.py:." \
    --add-data "dist-mitmdump/mitmdump:mitmdump" \
    --collect-all suanpan \
    --collect-all tomlkit \
    --collect-submodules uvicorn \
    --icon "$APP_ICON" \
    --osx-bundle-identifier com.benzai.magic-ai-router \
    app.py

deactivate

rm -f build_time.txt

# Patch Info.plist: LSUIElement (menu bar only) + version strings.
# Delete-then-Add handles keys PyInstaller may or may not have set.
PLIST="dist/Magic Stack.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Delete :LSUIElement" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :CFBundleShortVersionString" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :CFBundleVersion" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST"

# Re-sign ad-hoc (--deep): the PlistBuddy edits above mutated Info.plist AFTER
# PyInstaller signed the bundle, breaking the seal. Without this, a .dmg
# downloaded with a quarantine attribute is rejected by Gatekeeper on arm64
# ("killed: 9"). --deep is now required (not just defensive): the bundle
# nests the mitmdump executable, which needs its own valid signature too,
# not just the outer .app (verified empirically in the Task 1 spike against
# a nested PyInstaller binary -- see progress/backend-dev.md).
# (notarize.sh later re-signs with a Developer ID identity, superseding this.)
codesign --force --deep --sign - "dist/Magic Stack.app"

# ---------------------------------------------------------------------------
# 打包冒烟（issue #2）：执行 bundled app 二进制的 smoke 钩子——在
# _MEIPASS 内跑资源契约解析并实际 spawn bundled mitmdump 加载 bundled
# addon。判据单一归宿在 capture/resources.py（SMOKE_* 常量）。
# ---------------------------------------------------------------------------
APP_BUNDLE="dist/Magic Stack.app"
if ! MAGIC_PROXY_SMOKE_TEST=1 "$APP_BUNDLE/Contents/MacOS/Magic Stack"; then
    echo "ERROR: frozen capture smoke failed (see stderr above)" >&2
    exit 1
fi
echo "Smoke OK: frozen contract + bundled mitmdump loaded bundled addon"

echo ""
echo "=== Build complete ==="
echo "App: dist/Magic Stack.app"
echo ""
echo "To install: cp -R 'dist/Magic Stack.app' /Applications/"
