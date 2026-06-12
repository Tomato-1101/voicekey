#!/bin/bash
# 配布用 DMG / Sparkle 更新 zip / appcast.xml を作る（ベータ配布パイプライン）
#
# 使い方:
#   ./scripts/build_dmg.sh --version 1.0.0
#   ./scripts/build_dmg.sh --version 1.1.0 --identity "Developer ID Application: ..." --notarize
#
# 前提:
#   - Keychain に voicekey.{OpenAI,Groq,ElevenLabs,Deepgram} のキーが保存済み
#     （初回は security の許可ダイアログが出るので「常に許可」を選ぶ）
#   - Sparkle EdDSA 秘密鍵が ~/.voicekey/sparkle_eddsa_key にエクスポート済み
#   - --notarize は Apple Developer Program 加入後、
#     `xcrun notarytool store-credentials voicekey-notary` を済ませてから使う
#
# 出力:
#   dist/voicekey-<version>.dmg        … テスター向け配布物（GitHub Releases に添付）
#   dist/releases/voicekey-<version>.zip … Sparkle 更新用（voicekey-releases/mac/ に置く）
#   dist/releases/appcast.xml          … Sparkle フィード（voicekey-releases/mac/ に置く）
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION=""
IDENTITY="${VOICEKEY_SIGN_IDENTITY:-}"
NOTARIZE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --identity) IDENTITY="$2"; shift 2 ;;
        --notarize) NOTARIZE=1; shift ;;
        *) echo "不明な引数: $1" >&2; exit 1 ;;
    esac
done
[[ -n "$VERSION" ]] || { echo "エラー: --version X.Y.Z を指定してください" >&2; exit 1; }

# 署名 identity: 未指定なら Apple Development を自動検出。
# 配布物に ad-hoc 署名は禁止（テスター環境で確実にブロックされるため、無ければエラー終了）
if [[ -z "$IDENTITY" ]]; then
    IDENTITY="$(
        security find-identity -v -p codesigning 2>/dev/null \
            | sed -n 's/^[[:space:]]*[0-9]*) [A-F0-9]* "\(Apple Development:.*\)"$/\1/p' \
            | head -n 1
    )"
fi
[[ -n "$IDENTITY" ]] || { echo "エラー: 署名証明書が見つかりません（ad-hoc 配布は禁止）" >&2; exit 1; }
echo "==> 署名: $IDENTITY"

EDDSA_KEY="$HOME/.voicekey/sparkle_eddsa_key"
[[ -f "$EDDSA_KEY" ]] || { echo "エラー: Sparkle EdDSA 鍵がありません: $EDDSA_KEY" >&2; exit 1; }

# バージョン更新。CFBundleVersion は Sparkle が新旧比較に使うため必ず単調増加させる
BUILD_NUM=$(( $(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" Resources/Info.plist) + 1 ))
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" Resources/Info.plist
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_NUM" Resources/Info.plist
echo "==> バージョン: $VERSION (build $BUILD_NUM)"

# キー埋め込み。スクリプト終了時は成功・失敗を問わず必ずスタブに戻し、
# キー入りの生成ソースを作業ツリーに残さない
trap './scripts/generate_embedded_keys.sh >/dev/null 2>&1 || true' EXIT
./scripts/generate_embedded_keys.sh --dist

VOICEKEY_SIGN_IDENTITY="$IDENTITY" ./scripts/build_app.sh

APP="dist/voicekey.app"
BIN="$APP/Contents/MacOS/voicekey"
FW="$APP/Contents/Frameworks/Sparkle.framework"

# 開発機の絶対パス rpath（.build/artifacts/...）を除去して自己完結にする。
# @ で始まる rpath（@executable_path 等）は残す。署名前に行うこと
otool -l "$BIN" | awk '/LC_RPATH/{f=1} f && /path /{print $2; f=0}' | while read -r p; do
    [[ "$p" == @* ]] || install_name_tool -delete_rpath "$p" "$BIN"
done

# 配布用の署名（hardened runtime 必須・公証要件）。
# Sparkle の内部コンポーネントから .app 本体へ inside-out の順で署名する
echo "==> codesign (hardened runtime, inside-out)"
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
    "$FW/Versions/B/XPCServices/Installer.xpc"
# Downloader.xpc はサンドボックス entitlements を持つため維持する
codesign --force --options runtime --timestamp --preserve-metadata=entitlements \
    --sign "$IDENTITY" "$FW/Versions/B/XPCServices/Downloader.xpc"
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
    "$FW/Versions/B/Autoupdate"
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
    "$FW/Versions/B/Updater.app"
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$FW"
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
    --entitlements Resources/voicekey.entitlements --identifier com.voicekey.app "$APP"

codesign --verify --deep --strict "$APP"
echo "==> codesign verify OK"

# Sparkle 更新用 zip（DMG より zip 配信が確実・高速）
mkdir -p dist/releases
ZIP="dist/releases/voicekey-$VERSION.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"

# 公証（Developer Program 加入後のみ）。zip を提出 → .app に staple → zip を作り直す
if [[ "$NOTARIZE" -eq 1 ]]; then
    echo "==> notarytool submit（数分かかります）"
    xcrun notarytool submit "$ZIP" --keychain-profile voicekey-notary --wait
    xcrun stapler staple "$APP"
    rm -f "$ZIP"
    ditto -c -k --keepParent "$APP" "$ZIP"
fi

# DMG 作成（レイアウト付き: 左にアプリ・右に Applications・背景にドラッグ誘導矢印）
DMG="dist/voicekey-$VERSION.dmg"
./scripts/package_dmg.sh --version "$VERSION" --identity "$IDENTITY"

# appcast 生成（dist/releases/ 内の全 zip を EdDSA 署名して appcast.xml を更新）
.build/artifacts/sparkle/Sparkle/bin/generate_appcast \
    --ed-key-file "$EDDSA_KEY" \
    --download-url-prefix "https://voicekey.vercel.app/mac/" \
    --link "https://voicekey.vercel.app" \
    dist/releases
echo "==> appcast: dist/releases/appcast.xml"

echo ""
echo "==> 完了。配布手順（すべて voicekey-site / Vercel 経由。GitHub には置かない）:"
echo "    1. $DMG を voicekey-site/downloads/ へ、$ZIP と appcast.xml を voicekey-site/mac/ へコピー"
echo "    2. voicekey-site/downloads.json のバージョン・サイズを更新"
echo "    3. voicekey-site で vercel deploy --prod → Resources/Info.plist のバージョン更新をコミット"
