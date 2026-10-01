#!/bin/bash
# 配布用 DMG / Sparkle 更新 zip / appcast.xml を作る（GitHub Releases 配布パイプライン）
#
# 2026-10-01〜: 一般配布するのは personal 版（isPersonal=true・利用者が設定 › API キー で
# 自分のキーを入れて使う）。どのエディションにもプロバイダーキーは埋め込まない。
# 配布先は Tomato-1101/voicekey の GitHub Releases（タグ v<version>）。
#
# 使い方:
#   ./scripts/build_dmg.sh --version 2.1.0
#   ./scripts/build_dmg.sh --version 2.1.0 --identity "Developer ID Application: ..." --notarize
#
# 前提:
#   - Sparkle EdDSA 秘密鍵が ~/.voicekey/sparkle_eddsa_key にエクスポート済み
#   - --notarize は Apple Developer Program 加入後、
#     `xcrun notarytool store-credentials voicekey-notary` を済ませてから使う
#
# 出力（3 つとも GitHub Releases の v<version> に添付する）:
#   dist/voicekey-<version>.dmg                   … 新規インストール用
#   dist/releases/v<version>/voicekey-<version>.zip … Sparkle 更新用
#   dist/releases/v<version>/appcast.xml           … Sparkle フィード（今回の版だけを載せる）
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

# 版番号は awk で置換するので、数字とドット以外が混ざると置換文字列が壊れる。先に弾く
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "エラー: --version は X.Y.Z 形式で指定してください" >&2; exit 1; }

# 後始末を先に仕掛けてから作業ツリーを書き換える（途中で失敗しても元に戻せるように）。
# - 生成マーカー: 実行前の生成ファイル（作者の常用版なら personal、開発中ならスタブ）を退避し、
#   成功・失敗を問わず必ず元に戻す。無条件にスタブへ戻すと、次の build_app.sh で作者の常用版が
#   開発版に化けるため
# - Info.plist: 失敗したときだけ元に戻す。成功時はリリース版としてコミットするので書き換えを残す
#   （失敗で build 番号だけ上がったまま残ると、次の実行で番号が飛ぶ）
PLIST="Resources/Info.plist"
GEN="Sources/Voicekey/Config/EmbeddedKeys.generated.swift"
PLIST_BACKUP="$(mktemp)"
GEN_BACKUP="$(mktemp)"
cp "$PLIST" "$PLIST_BACKUP"
HAD_GEN=0
if [[ -f "$GEN" ]]; then
    cp "$GEN" "$GEN_BACKUP"
    HAD_GEN=1
fi
SUCCEEDED=0
cleanup() {
    if [[ "$HAD_GEN" -eq 1 ]]; then
        cp "$GEN_BACKUP" "$GEN"
    else
        rm -f "$GEN"
    fi
    if [[ "$SUCCEEDED" -ne 1 ]]; then
        cp "$PLIST_BACKUP" "$PLIST"
        echo "==> 失敗したため Info.plist の版番号を元に戻しました" >&2
    fi
    rm -f "$GEN_BACKUP" "$PLIST_BACKUP"
}
trap cleanup EXIT

# Info.plist の <key>$1</key> の次の行にある <string> の中身だけを $2 に置き換える。
# PlistBuddy で書くと Info.plist のコメント（各キーの「なぜ」）が全部消えるため、テキストとして置換する
set_plist_string() {
    local tmp
    tmp="$(mktemp)"
    awk -v key="<key>$1</key>" -v val="$2" '
        hit { sub(/<string>[^<]*<\/string>/, "<string>" val "</string>"); hit = 0 }
        index($0, key) { hit = 1 }
        { print }
    ' "$PLIST" > "$tmp"
    cat "$tmp" > "$PLIST"
    rm -f "$tmp"
    # 置換できなかった（キーの並びが変わった等）まま進むと古い版番号で配布してしまうので確かめる
    [[ "$(/usr/libexec/PlistBuddy -c "Print :$1" "$PLIST")" == "$2" ]] || {
        echo "エラー: Info.plist の $1 を書き換えられませんでした" >&2
        exit 1
    }
}

# バージョン更新。CFBundleVersion は Sparkle が新旧比較に使うため必ず単調増加させる
BUILD_NUM=$(( $(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$PLIST") + 1 ))
set_plist_string CFBundleShortVersionString "$VERSION"
set_plist_string CFBundleVersion "$BUILD_NUM"
echo "==> バージョン: $VERSION (build $BUILD_NUM)"

# 配布リリース用マーカー生成（isPersonal=true・isRelease=true・キーは埋め込まない）。
# isRelease はこのスクリプトで作る作者署名の版だけに付け、Sparkle の自動更新をそこだけで有効にする
./scripts/generate_embedded_keys.sh --personal --release

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

# プロバイダーキー漏洩チェック（生成マーカー＋ .app バンドル全体）。
# personal 版は利用者が自分のキーを入れて使う。作者のキーが 1 バイトでも混ざる回帰をここで止める。
# 本体バイナリだけでなく Resources・Frameworks も含め、zip / DMG に入るものと同じ署名後の .app を走査する。
# このスクリプトは macos/ を cwd にしているため、検証スクリプトはリポジトリ直下を参照する。
python3 ../scripts/build/verify_no_embedded_keys.py "$GEN" "$APP"

# Sparkle 更新用 zip（DMG より zip 配信が確実・高速）。
# 版ごとの作業フォルダを毎回作り直し、appcast に古い zip・delta が混ざらないようにする
REL_DIR="dist/releases/v$VERSION"
rm -rf "$REL_DIR"
mkdir -p "$REL_DIR"
ZIP="$REL_DIR/voicekey-$VERSION.zip"
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

# appcast 生成（今回の版の zip だけを EdDSA 署名して appcast.xml を作る）。
# zip の URL は GitHub Releases のタグ v<version> の添付ファイルを指す
.build/artifacts/sparkle/Sparkle/bin/generate_appcast \
    --ed-key-file "$EDDSA_KEY" \
    --download-url-prefix "https://github.com/Tomato-1101/voicekey/releases/download/v$VERSION/" \
    --link "https://github.com/Tomato-1101/voicekey/releases" \
    "$REL_DIR"
echo "==> appcast: $REL_DIR/appcast.xml"
# ここまで来たら成功。Info.plist の版番号の書き換えはリリース版としてコミットするので残す
SUCCEEDED=1

echo ""
echo "==> 完了。GitHub Releases への公開手順:"
echo "    1. Resources/Info.plist のバージョン更新をコミットして push"
echo "    2. gh release create v$VERSION \"$DMG\" \"$ZIP\" \"$REL_DIR/appcast.xml\" \\"
echo "         --repo Tomato-1101/voicekey --title \"voicekey $VERSION\" --notes \"<変更点>\""
echo "       ※ pre-release / draft にしない（アプリは releases/latest/download/appcast.xml を見る）"
echo "    3. Homebrew の tap（~/Project/homebrew-tap/Casks/voicekey.rb）を新しい版に合わせてコミットして push:"
echo "         version \"$VERSION\""
echo "         sha256 \"$(shasum -a 256 "$ZIP" | awk '{print $1}')\""
echo "       ※ install.sh は appcast から最新版を取るので更新不要"
