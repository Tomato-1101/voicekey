#!/bin/bash
# voicekey.app をビルドする（SPM リリースビルド → .app バンドル組み立て → 証明書署名）
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

# 署名: Apple 発行の Apple Development 証明書を優先する。
# 自己署名証明書は再ビルドごとに Keychain ACL の cdhash が変わるため使わない。
APPLE_DEVELOPMENT_IDENTITY="$(
    security find-identity -v -p codesigning 2>/dev/null \
        | sed -n 's/^[[:space:]]*[0-9]*) [A-F0-9]* "\(Apple Development:.*\)"$/\1/p' \
        | head -n 1
)"

if [[ -n "$APPLE_DEVELOPMENT_IDENTITY" ]]; then
    echo "==> codesign ($APPLE_DEVELOPMENT_IDENTITY)"
    codesign --force --sign "$APPLE_DEVELOPMENT_IDENTITY" --identifier com.voicekey.app "$APP"
else
    echo "==> codesign (ad-hoc) ※証明書未登録のため毎ビルドで権限再付与が必要"
    codesign --force --sign - --identifier com.voicekey.app "$APP"
fi

echo "==> 完了: $APP"
echo "    open $APP で起動できます"
