#!/bin/bash
# Build Magic Stack.app with PyInstaller (bundles mitmdump for capture mode)
set -e

# Version. Bump this when cutting a new release; also tag git with v$VERSION.
VERSION="0.16.1"
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
# NOTE: mitmproxy 12.x requires Python >=3.12, and CVE-2025-23217 is
# mitmweb-only（只发 mitmdump 不受影响）——两点均已收录进 ADR-001
# （已修订，见 docs/adr/001-mitm-packet-capture.md；R9-C2 删除本处
# 指向「ADR 待修订」的过期快照注释）。
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
# R9 注：lock 含 mitmproxy ≥3.12——MAIN_PYTHON_BIN 的实际下界同为
# 3.12（「可换 3.9 解释器」不成立）；自有代码 ≥3.9 下界由 CI 语法层
# 保障，不在构建链。
MAIN_VENV=".build-venv-main"
rm -rf "$MAIN_VENV"
"$MAIN_PYTHON_BIN" -m venv "$MAIN_VENV"
# shellcheck source=/dev/null
source "$MAIN_VENV/bin/activate"
pip install -q --upgrade pip
pip install -q --require-hashes -r requirements-lock.txt

# Generate the menu-bar state icons when missing OR stale（R9-C2：原只判
# 缺失——icns 换版后 PNG 永不刷新；且 generate_icon 已纯 Pillow 化，
# 原 numpy 依赖不在 lock，再生成曾是 ModuleNotFoundError 哑弹）。
# Pillow is in the venv; the menu-bar state icon is loaded and tinted
# dynamically by menu_builder.py.
APP_ICON="icons/magic-ai-router-macos-v2.icns"
if [ ! -f assets/MenubarIcon.png ] || [ "$APP_ICON" -nt assets/MenubarIcon.png ]; then
    python tools/generate_icon.py
fi
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
    $(python tools/resource_manifest.py --pyinstaller-args) \
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
