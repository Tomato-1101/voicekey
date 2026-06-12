#!/bin/bash
# 開発版（API キーは Keychain / 設定画面で自分入力・API キータブあり）を起動する。
#
# 使い方:
#   ./scripts/run_dev.sh
#
# dist/voicekey.app はビルドのたびに開発版とベータ版（埋め込みキー）で上書きされるため、
# 常用は /Applications/voicekey.app に固定する。このスクリプトは必ずスタブ鍵で
# ビルドし直してからインストール・再起動するので、「いま動いているのがどっちか分からない」
# 状態にならない。
set -euo pipefail

cd "$(dirname "$0")/.."

# 直前に配布ビルドをしていても、必ず開発版（スタブ鍵）に戻してからビルドする
./scripts/generate_embedded_keys.sh
./scripts/build_app.sh

pkill -x voicekey 2>/dev/null || true
rm -rf /Applications/voicekey.app
ditto dist/voicekey.app /Applications/voicekey.app
open /Applications/voicekey.app

echo ""
echo "==> 開発版を起動しました: /Applications/voicekey.app"
echo "    （API キーは Keychain。設定画面に API キータブが表示されるのが開発版の目印）"
