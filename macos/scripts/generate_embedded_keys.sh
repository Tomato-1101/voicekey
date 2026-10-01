#!/bin/bash
# EmbeddedKeys.generated.swift を生成する（git 管理外）。
#
# このモジュールは「旧製品版（DIST）ビルドか（isDist）」「personal エディションか（isPersonal）」
# 「GitHub Releases で配る正式リリース版か（isRelease）」のフラグ **だけ** を持つ。
# 長期プロバイダーキーはどのビルドにも 1 バイトも埋め込まない（2026-06-28 セキュリティ修正）。
#
# 【DIST（旧製品版）】: 文字起こし・整形をすべて自社サーバー経由（短命 JWT 直叩き / プロキシ）で
#   行うエディション。製品版の運用は終了しており、今は配布していない。
#
# 【personal】: 作者の常用版であり、2026-10-01〜は GitHub Releases で一般配布している版そのもの。
#   キーは **埋め込まず**、設定 › API キー で入れた値 → 環境変数 → 中央 Keychain の順に読む
#   （サーバー往復ゼロ＝最速、鍵ローテ時の再ビルドも不要）。README の手順でソースから
#   ビルドした利用者も personal になる。
#
# 【release】: build_dmg.sh だけが --personal と一緒に付ける「作者署名の配布物」の印。
#   Sparkle の自動更新はこの印があるビルドでだけ有効にする。自分でビルドした personal に
#   作者署名の版への更新が出ると、署名が変わって TCC（マイク等）と Keychain の許可が外れるため。
#
# 使い方:
#   ./scripts/generate_embedded_keys.sh                      # スタブ（全フラグ false・通常開発）
#   ./scripts/generate_embedded_keys.sh --dist               # 旧製品版（isDist=true）
#   ./scripts/generate_embedded_keys.sh --personal           # personal 版（ソースからビルドする人はこれ）
#   ./scripts/generate_embedded_keys.sh --personal --release # 配布リリース用（build_dmg.sh 専用）
set -euo pipefail

cd "$(dirname "$0")/.."
OUT="Sources/Voicekey/Config/EmbeddedKeys.generated.swift"

# ターミナル/チャット経由のコピペで -- が – (en dash) や — (em dash) に化ける事故が
# 実際に複数回起きたため、化けたダッシュの変種も受け付ける。
# また、引数の打ち間違いで黙ってスタブを生成すると「できたつもり」事故になるためエラーにする
IS_DIST="false"
IS_PERSONAL="false"
IS_RELEASE="false"
for ARG in "$@"; do
    case "$ARG" in
        --dist | –dist | —dist | –-dist | —-dist) IS_DIST="true" ;;
        --personal | –personal | —personal | –-personal | —-personal) IS_PERSONAL="true" ;;
        --release | –release | —release | –-release | —-release) IS_RELEASE="true" ;;
        *)
            echo "エラー: 不明な引数: ${ARG}（使えるのは --dist / --personal / --release のみ）" >&2
            exit 1
            ;;
    esac
done
if [[ "$IS_DIST" == "true" && "$IS_PERSONAL" == "true" ]]; then
    echo "エラー: --dist と --personal は同時に指定できません" >&2
    exit 1
fi
# 配布するのは personal 版だけなので、release の印は personal にしか付けない
if [[ "$IS_RELEASE" == "true" && "$IS_PERSONAL" != "true" ]]; then
    echo "エラー: --release は --personal と一緒に指定してください（build_dmg.sh 専用）" >&2
    exit 1
fi

# フラグだけを持つエディションマーカーを生成する（キーは埋め込まない）。
# heredoc は変数展開のため非クォート。Swift 本文に他の $ は無いので安全。
cat > "$OUT" <<EOF
//
//  EmbeddedKeys.generated.swift（自動生成・エディションマーカー）
//  生成元: macos/scripts/generate_embedded_keys.sh（手で編集しない）
//
//  どのビルドにも長期プロバイダーキーは埋め込まない（フラグだけを持つ）。
//  - DIST（旧製品版・配布終了）: 文字起こし・整形は自社サーバー経由。
//  - personal: 設定 › API キー → 環境変数 → 中央 Keychain の順に読んで直叩き（サーバー往復ゼロ）。
//  - release: build_dmg.sh で作る作者署名の配布物。Sparkle の自動更新はこれだけで有効。
//

import Foundation

enum EmbeddedKeys {
    /// 旧製品版（DIST）ビルドかどうか。API キータブ非表示などの分岐に使う
    static let isDist = ${IS_DIST}
    /// personal エディションのビルドかどうか。
    /// STT を Keychain 直読の直叩き経路に固定し、認証/課金 UI を隠すのに使う。
    static let isPersonal = ${IS_PERSONAL}
    /// GitHub Releases で配る作者署名の配布物かどうか（build_dmg.sh だけが true にする）。
    /// 自動アップデート（Sparkle）はこれが true のときだけ有効にする。
    static let isRelease = ${IS_RELEASE}
}
EOF

echo "==> 生成: $OUT (isDist=${IS_DIST}・isPersonal=${IS_PERSONAL}・isRelease=${IS_RELEASE}・キー埋め込みなし)"
