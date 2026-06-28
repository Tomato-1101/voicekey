#!/bin/bash
# EmbeddedKeys.generated.swift を生成する（git 管理外）。
#
# このモジュールは「配布（DIST）ビルドかどうか」を示すフラグ（isDist）だけを持つ。
# 長期プロバイダーキーは配布バイナリに 1 バイトも埋め込まない（2026-06-28 セキュリティ修正）。
# 製品版（release）は文字起こし・整形をすべて自社サーバー経由（短命 JWT 直叩き / プロキシ）で
# 行うため、アプリ側にプロバイダーキーは不要。以前の XOR 難読化埋め込みは暗号化ではなく
# （マスクも同じバイナリに同梱され復元可能）漏洩源だったため撤去した。
#
# 使い方:
#   ./scripts/generate_embedded_keys.sh          # スタブ（isDist=false・通常開発ビルド用）
#   ./scripts/generate_embedded_keys.sh --dist   # 配布ビルド用（isDist=true・キー埋め込みなし）
set -euo pipefail

cd "$(dirname "$0")/.."
OUT="Sources/Voicekey/Config/EmbeddedKeys.generated.swift"

# ターミナル/チャット経由のコピペで -- が – (en dash) や — (em dash) に化ける事故が
# 実際に複数回起きたため、化けたダッシュの変種も受け付ける。
# また、引数の打ち間違いで黙ってスタブを生成すると「できたつもり」事故になるためエラーにする
ARG="${1:-}"
IS_DIST="false"
case "$ARG" in
    "") IS_DIST="false" ;;
    --dist | –dist | —dist | –-dist | —-dist) IS_DIST="true" ;;
    *)
        echo "エラー: 不明な引数: ${ARG}（使えるのは --dist のみ）" >&2
        exit 1
        ;;
esac

# isDist だけを持つ DIST マーカーを生成（キーは埋め込まない）。
# heredoc は IS_DIST を展開するため非クォート。Swift 本文に $ は無いので安全。
cat > "$OUT" <<EOF
//
//  EmbeddedKeys.generated.swift（自動生成・DIST マーカー）
//  生成元: macos/scripts/generate_embedded_keys.sh（手で編集しない）
//
//  配布バイナリには長期プロバイダーキーを埋め込まない（製品版は自社サーバー経由）。
//  このモジュールは「配布ビルドである」ことを示すフラグだけを持つ。
//

import Foundation

enum EmbeddedKeys {
    /// 配布（DIST）ビルドかどうか。API キータブ非表示・自動アップデート判定などに使う
    static let isDist = ${IS_DIST}
}
EOF
echo "==> 生成: $OUT (isDist=${IS_DIST}・キー埋め込みなし)"
