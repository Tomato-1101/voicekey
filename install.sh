#!/bin/sh
# voicekey（Mac）を最新版で /Applications へ入れるスクリプト。
#   curl -fsSL https://raw.githubusercontent.com/Tomato-1101/voicekey/main/install.sh | sh
# 配布版は Apple の公証を通していない。ブラウザで落とすと Gatekeeper に止められるが、
# curl で落としたファイルには quarantine 属性が付かないので、この経路なら警告なしで開ける。
set -eu

REPO="Tomato-1101/voicekey"
DEST="/Applications"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "voicekey は Apple シリコンの Mac（macOS 14 以降）専用です。" >&2
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# 最新版の zip の URL は appcast（Sparkle の更新フィード）から取る。
# GitHub API を使わないのでレート制限に当たらず、自動更新と同じ版が必ず入る。
ZIP_URL=$(curl -fsSL "https://github.com/$REPO/releases/latest/download/appcast.xml" \
  | grep -oE 'url="[^"]+\.zip"' | head -n 1 | sed -e 's/^url="//' -e 's/"$//')
if [ -z "$ZIP_URL" ]; then
  echo "最新版が見つかりませんでした: https://github.com/$REPO/releases" >&2
  exit 1
fi

echo "ダウンロード中: $ZIP_URL"
curl -fL --progress-bar "$ZIP_URL" -o "$TMP/voicekey.zip"
ditto -x -k "$TMP/voicekey.zip" "$TMP"

# 起動中なら止めてから差し替える（動いている .app を上書きしない）。
pkill -x voicekey 2>/dev/null || true
rm -rf "$DEST/voicekey.app"
ditto "$TMP/voicekey.app" "$DEST/voicekey.app"
xattr -dr com.apple.quarantine "$DEST/voicekey.app" 2>/dev/null || true

echo "インストールしました: $DEST/voicekey.app"
open "$DEST/voicekey.app"
