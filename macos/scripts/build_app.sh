#!/bin/bash
# voicekey.app をビルドする（SPM リリースビルド → .app バンドル組み立て → 証明書署名）
#
# 使い方:
#   ./scripts/build_app.sh
# 出力:
#   dist/voicekey.app
set -euo pipefail

cd "$(dirname "$0")/.."

# EmbeddedKeys.generated.swift（git 管理外）が無いとコンパイルできないため、
# 未生成ならスタブ（isDist=false・キーなし）を自動生成する
if [[ ! -f Sources/Voicekey/Config/EmbeddedKeys.generated.swift ]]; then
    ./scripts/generate_embedded_keys.sh
fi

echo "==> swift build (release)"
swift build -c release

APP="dist/voicekey.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp .build/release/voicekey "$APP/Contents/MacOS/voicekey"
cp Resources/Info.plist "$APP/Contents/Info.plist"

# Sparkle.framework を同梱する。SPM の手組みバンドルでは Xcode と違い自動埋め込み
# されないため、xcframework から自分でコピーして rpath を通す
SPARKLE_FW=".build/artifacts/sparkle/Sparkle/Sparkle.xcframework/macos-arm64_x86_64/Sparkle.framework"
if [[ -d "$SPARKLE_FW" ]]; then
    mkdir -p "$APP/Contents/Frameworks"
    cp -R "$SPARKLE_FW" "$APP/Contents/Frameworks/"
    # 既に同じ rpath があると install_name_tool が失敗するので無視する
    install_name_tool -add_rpath "@executable_path/../Frameworks" \
        "$APP/Contents/MacOS/voicekey" 2>/dev/null || true
else
    echo "エラー: Sparkle.framework が見つかりません（swift package resolve を実行してください）" >&2
    exit 1
fi

# 署名: VOICEKEY_SIGN_IDENTITY があればそれを使う（配布ビルドが Developer ID 等を指定する用）。
# 無ければ Apple 発行の Apple Development 証明書を優先する。
# 自己署名証明書は再ビルドごとに Keychain ACL の cdhash が変わるため使わない。
APPLE_DEVELOPMENT_IDENTITY="${VOICEKEY_SIGN_IDENTITY:-$(
    security find-identity -v -p codesigning 2>/dev/null \
        | sed -n 's/^[[:space:]]*[0-9]*) [A-F0-9]* "\(Apple Development:.*\)"$/\1/p' \
        | head -n 1
)}"

if [[ -n "$APPLE_DEVELOPMENT_IDENTITY" ]]; then
    echo "==> codesign ($APPLE_DEVELOPMENT_IDENTITY)"
    codesign --force --sign "$APPLE_DEVELOPMENT_IDENTITY" --identifier com.voicekey.app "$APP"
else
    echo "==> codesign (ad-hoc) ※証明書未登録のため毎ビルドで権限再付与が必要"
    codesign --force --sign - --identifier com.voicekey.app "$APP"
fi

echo "==> 完了: $APP"
echo "    open $APP で起動できます"
