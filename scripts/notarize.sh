#!/bin/bash
# Sign + notarize + staple Magic Stack (.app AND .dmg) for distribution.
# One-shot: codesign the app → notarize it → staple → rebuild the dmg from the
# signed app → notarize the dmg → staple → Gatekeeper assess.
# Run AFTER build.sh.
#
# Required env on FIRST run (credentials are then stored in keychain profile
# "magic-proxy-notary"; the password is no longer needed on later runs):
#   MP_APPLE_ID      Apple ID email
#   MP_APP_PASSWORD  app-specific password (https://appleid.apple.com)
#   MP_TEAM_ID       10-char Team ID
# Optional:
#   MP_IDENTITY      exact "Developer ID Application: Name (TEAMID)" string;
#                    omit to auto-pick the only Developer ID Application cert.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Magic Stack"
APP="$ROOT/dist/$APP_NAME.app"
KEYCHAIN_PROFILE="magic-proxy-notary"
NOTARY_TMP="$(mktemp -d -t magicproxy-notary)"
trap 'rm -rf "$NOTARY_TMP"' EXIT INT TERM

[ -d "$APP" ] || { echo "找不到 $APP —— 请先运行 bash build.sh"; exit 1; }

VERSION=$(grep -E '^VERSION=' "$ROOT/build.sh" | head -1 | sed -E 's/VERSION="([^"]*)"/\1/')
if [ "${MP_ALLOW_DIRTY_RELEASE:-0}" != "1" ]; then
    if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
        echo "工作区不干净，拒绝发布（紧急覆盖：MP_ALLOW_DIRTY_RELEASE=1）"; exit 1
    fi
    TAG=$(git -C "$ROOT" describe --tags --exact-match HEAD 2>/dev/null || true)
    # printf 传参展开（2026-09-28：内联 "$VERSION（" 在 macOS bash 3.2 的
    # 非 ASCII 邻接处解析成畸形变量名——此分支首次被走到才暴露）
    [ "$TAG" = "v$VERSION" ] \
        || { printf '当前提交必须标记为 %s（实际: %s）\n' \
               "v$VERSION" "${TAG:-无 tag}"; exit 1; }
fi

# ── 选择签名身份 ───────────────────────────────────────────────
IDENTITY="${MP_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
    LINE=$(security find-identity -p codesigning -v | grep "Developer ID Application" | head -1) || true
    [ -n "$LINE" ] || { echo "钥匙串里没有 Developer ID Application 证书。请先创建并导入。"; exit 1; }
    IDENTITY=$(echo "$LINE" | sed -E 's/.*"(.*)".*/\1/')
fi
echo "▶ 签名身份: $IDENTITY"

# ── 存公证凭证到钥匙串(仅当本次提供了密码时)─────────────────
if [ -n "${MP_APP_PASSWORD:-}" ]; then
    : "${MP_APPLE_ID:?需要 MP_APPLE_ID}"
    : "${MP_TEAM_ID:?需要 MP_TEAM_ID}"
    echo "▶ 存公证凭证到 keychain profile '$KEYCHAIN_PROFILE'"
    xcrun notarytool store-credentials "$KEYCHAIN_PROFILE" \
        --apple-id "$MP_APPLE_ID" --password "$MP_APP_PASSWORD" \
        --team-id "$MP_TEAM_ID" >/dev/null
fi

# ── 时间戳策略（2026-09-18 Apple TSA 全局拒接 443 的降级开关）─────
# 缺省 --timestamp（Apple TSA）；MP_TIMESTAMP_URL=none → --timestamp=none
# （公证仍接受，仅记「无安全时间戳」警告——撤销语义丢失，Apple TSA
# 恢复后应移除该环境变量重发）；MP_TIMESTAMP_URL=http://… → 自选 TSA
# （codesign 仅支持 http）。
case "${MP_TIMESTAMP_URL:-}" in
    none) TIMESTAMP_FLAG="--timestamp=none" ;;
    "")   TIMESTAMP_FLAG="--timestamp" ;;
    *)    TIMESTAMP_FLAG="--timestamp=${MP_TIMESTAMP_URL}" ;;
esac

# ── 1. codesign .app(深签 + hardened runtime + 时间戳)─────────
# --deep 已覆盖 ADR-001 抓包模式打包进 Contents/Resources|Frameworks/mitmdump/
# 的嵌套 mitmdump 可执行文件（build.sh 现默认 bundle，Task 5）——deep sign
# 会递归对 bundle 内所有嵌套二进制用同一身份 + hardened runtime + 时间戳重签，
# 不需要为 mitmdump 单独加一条 codesign 命令。下面额外对该嵌套二进制单独
# verify 一次，把"确实签到了"变成可见证据而非隐含假设。
echo "▶ 1/6  codesign .app"
codesign --deep --force --options runtime $TIMESTAMP_FLAG --sign "$IDENTITY" "$APP"
codesign --verify --strict --deep "$APP" && echo "  签名验证 OK"
MITMDUMP_BIN="$APP/Contents/Resources/mitmdump/mitmdump"
if [ -f "$MITMDUMP_BIN" ]; then
    codesign --verify --strict "$MITMDUMP_BIN" && echo "  mitmdump 嵌套二进制签名验证 OK"
    "$MITMDUMP_BIN" --version >/dev/null || { echo "mitmdump smoke 失败"; exit 1; }
else
    echo "未找到必需组件 $MITMDUMP_BIN"; exit 1
fi

# ── 2. 公证 .app(--wait 阻塞到 Apple 审核完成,约 2–10 分钟)──
echo "▶ 2/6  公证 .app(等待 Apple 审核…)"
ditto -c -k --keepParent "$APP" "$NOTARY_TMP/$APP_NAME.zip"
xcrun notarytool submit "$NOTARY_TMP/$APP_NAME.zip" --keychain-profile "$KEYCHAIN_PROFILE" --wait
rm -f "$NOTARY_TMP/$APP_NAME.zip"

# ── 3. 装订 .app ────────────────────────────────────────────────
echo "▶ 3/6  装订 .app"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"

# ── 4. 用已签名 app 重建 dmg ────────────────────────────────────
echo "▶ 4/6  重建 dmg + codesign dmg"
bash "$ROOT/scripts/build_dmg.sh" >/dev/null
DMG="$ROOT/dist/$APP_NAME-$VERSION.dmg"
[ -f "$DMG" ] || { echo "未生成预期 DMG: $DMG"; exit 1; }
# dmg 必须也用 Developer ID 签名,否则 spctl --assess 报 "no usable signature"
codesign --force --sign "$IDENTITY" $TIMESTAMP_FLAG "$DMG"

# ── 5. 公证 dmg ────────────────────────────────────────────────
echo "▶ 5/6  公证 dmg(等待 Apple 审核…)"
xcrun notarytool submit "$DMG" --keychain-profile "$KEYCHAIN_PROFILE" --wait

# ── 6. 装订 dmg + Gatekeeper 评估 ───────────────────────────────
echo "▶ 6/6  装订 dmg + Gatekeeper 评估"
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"
spctl --assess --type install --verbose "$DMG"

echo ""
echo "✅ 完成,可分发: $DMG"
