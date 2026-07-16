#!/bin/bash
# EmbeddedKeys.generated.swift を生成する（git 管理外）。
#
# このモジュールは「配布（DIST）ビルドかどうか（isDist）」「個人用最速版か（isPersonal）」の
# フラグ **だけ** を持つ。長期プロバイダーキーはどのビルドにも 1 バイトも埋め込まない
# （2026-06-28 セキュリティ修正）。
#
# 【配布（release/main）】: 製品版（release）は文字起こし・整形をすべて自社サーバー経由
#   （短命 JWT 直叩き / プロキシ）で行うため、アプリ側にプロバイダーキーは不要。
#
# 【personal（開発者本人の日常利用専用・非配布）】: サーバー/ログイン/課金の往復ゼロで最速に
#   叩くが、キーは **埋め込まず** 開発者の Keychain（voicekey.Deepgram 等）から直読する
#   （isDist=false と同じ「Keychain 直叩き」経路を流用）。プロセス内キャッシュ済みで即時＝
#   サーバー往復ゼロ、しかも鍵ローテ時の再ビルドが不要。よって焼き込むのは isPersonal フラグだけ。
#
# 使い方:
#   ./scripts/generate_embedded_keys.sh            # スタブ（isDist=false・isPersonal=false・通常開発）
#   ./scripts/generate_embedded_keys.sh --dist     # 配布ビルド用（isDist=true・isPersonal=false）
#   ./scripts/generate_embedded_keys.sh --personal # 個人用最速版（isPersonal=true・isDist=false）
set -euo pipefail

cd "$(dirname "$0")/.."
OUT="Sources/Voicekey/Config/EmbeddedKeys.generated.swift"

# ターミナル/チャット経由のコピペで -- が – (en dash) や — (em dash) に化ける事故が
# 実際に複数回起きたため、化けたダッシュの変種も受け付ける。
# また、引数の打ち間違いで黙ってスタブを生成すると「できたつもり」事故になるためエラーにする
ARG="${1:-}"
IS_DIST="false"
IS_PERSONAL="false"
case "$ARG" in
    "") IS_DIST="false" ;;
    --dist | –dist | —dist | –-dist | —-dist) IS_DIST="true" ;;
    --personal | –personal | —personal | –-personal | —-personal)
        IS_PERSONAL="true"
        IS_DIST="false"
        ;;
    *)
        echo "エラー: 不明な引数: ${ARG}（使えるのは --dist / --personal のみ）" >&2
        exit 1
        ;;
esac

# フラグだけを持つエディションマーカーを生成する（キーは埋め込まない）。
# heredoc は変数展開のため非クォート。Swift 本文に他の $ は無いので安全。
cat > "$OUT" <<EOF
//
//  EmbeddedKeys.generated.swift（自動生成・エディションマーカー）
//  生成元: macos/scripts/generate_embedded_keys.sh（手で編集しない）
//
//  どのビルドにも長期プロバイダーキーは埋め込まない（フラグだけを持つ）。
//  - 配布（release/main）: 文字起こし・整形は自社サーバー経由。
//  - personal（個人用最速版・非配布）: 開発者の Keychain から直読して直叩き（サーバー往復ゼロ）。
//

import Foundation

enum EmbeddedKeys {
    /// 配布（DIST）ビルドかどうか。API キータブ非表示・自動アップデート判定などに使う
    static let isDist = ${IS_DIST}
    /// 個人用最速版（personal エディション）ビルドかどうか。
    /// STT を Keychain 直読の直叩き経路に固定し、認証/課金 UI を隠し、ライト固定にするのに使う。
    static let isPersonal = ${IS_PERSONAL}
}
EOF

if [[ "$IS_PERSONAL" == "true" ]]; then
    echo "==> 生成: $OUT (isPersonal=true・isDist=false・キー埋め込みなし＝Keychain 直読)"
else
    echo "==> 生成: $OUT (isDist=${IS_DIST}・isPersonal=false・キー埋め込みなし)"
fi
