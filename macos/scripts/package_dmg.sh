#!/bin/bash
# ビルド済み dist/voicekey.app から配布用 DMG を作る（レイアウト付き）
#
# 一般的なアプリの DMG と同じ「左にアプリ・右に Applications・背景にドラッグ誘導矢印」の
# 見た目にするため、一旦 UDRW で作成 → Finder (AppleScript) でアイコン位置・背景を
# .DS_Store に焼き込み → UDZO に変換して署名する。
#
# 使い方:
#   ./scripts/package_dmg.sh --version 1.0.0 [--identity "Apple Development: ..."]
#
# 前提:
#   - dist/voicekey.app がビルド・署名済み（通常は build_dmg.sh から呼ばれる）
#   - 初回は osascript の「Finder を制御する」許可ダイアログが出るので OK を選ぶ
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION=""
IDENTITY="${VOICEKEY_SIGN_IDENTITY:-}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --identity) IDENTITY="$2"; shift 2 ;;
        *) echo "不明な引数: $1" >&2; exit 1 ;;
    esac
done
[[ -n "$VERSION" ]] || { echo "エラー: --version X.Y.Z を指定してください" >&2; exit 1; }

APP="dist/voicekey.app"
[[ -d "$APP" ]] || { echo "エラー: $APP がありません（先に build_dmg.sh / build_app.sh を実行）" >&2; exit 1; }

# 署名 identity: 未指定なら Apple Development を自動検出（build_dmg.sh と同じ規則）
if [[ -z "$IDENTITY" ]]; then
    IDENTITY="$(
        security find-identity -v -p codesigning 2>/dev/null \
            | sed -n 's/^[[:space:]]*[0-9]*) [A-F0-9]* "\(Apple Development:.*\)"$/\1/p' \
            | head -n 1
    )"
fi
[[ -n "$IDENTITY" ]] || { echo "エラー: 署名証明書が見つかりません" >&2; exit 1; }

VOL="voicekey"
README="はじめにお読みください.txt"

# 同名ボリュームが残っているとマウント先と Finder の対象がずれるため先に外す
if [[ -d "/Volumes/$VOL" ]]; then
    hdiutil detach "/Volumes/$VOL" -quiet || true
fi

# ステージング（背景画像は不可視の .background/ に入れる）
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp scripts/dmg_readme.txt "$STAGE/$README"
mkdir "$STAGE/.background"
cp scripts/assets/dmg_background.png "$STAGE/.background/"

DMG="dist/voicekey-$VERSION.dmg"
RW="dist/voicekey-$VERSION.rw.dmg"
rm -f "$DMG" "$RW"

# レイアウト編集のため、まず読み書き可能（UDRW）で作る
hdiutil create -volname "$VOL" -srcfolder "$STAGE" -ov -format UDRW "$RW" >/dev/null
rm -rf "$STAGE"

MNT="$(hdiutil attach "$RW" -readwrite -noverify | awk -F'\t' '/\/Volumes\//{print $NF}')"
echo "==> マウント: $MNT"

# Finder でウィンドウサイズ・背景・アイコン位置を設定（.DS_Store に保存される）。
# 座標は scripts/assets/dmg_background.png（660x400pt）のデザインと対応している
# 注意: close → open をやり直すと Finder が以前のウィンドウサイズで bounds を
# 保存し直してしまう（実測）ため、開いたまま設定 → update → close の一発で焼き込む。
# bounds は +28pt がタイトルバー分（コンテンツ領域を背景画像と同じ 660x400 にする）
osascript <<EOF
tell application "Finder"
    tell disk "$VOL"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {200, 120, 860, 548}
        set vo to the icon view options of container window
        set arrangement of vo to not arranged
        set icon size of vo to 116
        set text size of vo to 12
        set background picture of vo to file ".background:dmg_background.png"
        set position of item "voicekey.app" of container window to {165, 205}
        set position of item "Applications" of container window to {495, 205}
        set position of item "$README" of container window to {575, 80}
        update without registering applications
        delay 2
        close
    end tell
end tell
EOF

sync
hdiutil detach "$MNT" -quiet

# 配布用に圧縮（UDZO）へ変換して署名
hdiutil convert "$RW" -format UDZO -imagekey zlib-level=9 -o "$DMG" >/dev/null
rm -f "$RW"
codesign --force --sign "$IDENTITY" "$DMG"
echo "==> DMG: $DMG"
