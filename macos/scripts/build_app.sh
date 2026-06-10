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

# 署名: 自己署名証明書 "voicekey-codesign" があればそれを使う。
# 証明書署名なら再ビルドしても署名 ID が変わらず、TCC（マイク・入力監視・
# アクセシビリティ）と Keychain の許可が完全に引き継がれる。
# 証明書がない場合は ad-hoc（ビルドごとに別アプリ扱いになり、許可の再付与が必要）
if security find-identity -v -p codesigning 2>/dev/null | grep -q "voicekey-codesign"; then
    echo "==> codesign (voicekey-codesign 証明書)"
    codesign --force --sign "voicekey-codesign" --identifier com.voicekey.app "$APP"
else
    echo "==> codesign (ad-hoc) ※証明書未登録のため毎ビルドで権限再付与が必要"
    codesign --force --sign - --identifier com.voicekey.app "$APP"
fi

echo "==> 完了: $APP"
echo "    open $APP で起動できます"
