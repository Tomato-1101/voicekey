#!/bin/bash
# voicekey.app をビルドする（SPM リリースビルド → .app バンドル組み立て → ad-hoc 署名）
#
# 使い方:
#   ./scripts/build_app.sh
# 出力:
#   dist/voicekey.app
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> swift build (release)"
swift build -c release

APP="dist/voicekey.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp .build/release/voicekey "$APP/Contents/MacOS/voicekey"
cp Resources/Info.plist "$APP/Contents/Info.plist"

# ad-hoc 署名。identifier を固定することで、再ビルドしても
# TCC（マイク・入力監視・アクセシビリティ）の許可が引き継がれやすくなる
echo "==> codesign (ad-hoc)"
codesign --force --sign - --identifier com.voicekey.app "$APP"

echo "==> 完了: $APP"
echo "    open $APP で起動できます"
