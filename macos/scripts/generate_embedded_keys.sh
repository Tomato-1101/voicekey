#!/bin/bash
# EmbeddedKeys.generated.swift を生成する（git 管理外・配布ビルド用キー埋め込み）
#
# 使い方:
#   ./scripts/generate_embedded_keys.sh               # スタブ生成（isDist=false・キーなし、通常開発ビルド用）
#   ./scripts/generate_embedded_keys.sh --dist        # Keychain から現行キーを抽出して埋め込み版を生成
#   ./scripts/generate_embedded_keys.sh --export-env  # Keychain からキーを .env.dist（リポジトリ直下・git 管理外）へ書き出す
#
# --dist / --export-env は macOS Keychain の voicekey.{OpenAI,Groq,ElevenLabs,Deepgram} を読む。
# 初回はサービスごとに許可ダイアログが出るので「常に許可」を選ぶこと。
# --export-env は Windows ビルド機へキーを手動コピーするためのもの
# （scripts/build/generate_embedded_keys.py が .env.dist を読む）。
# 注意: ad-hoc 署名の検証ハーネスからは絶対に実行しない（Keychain ACL の partition 退行事故の既知原因）。
set -euo pipefail

cd "$(dirname "$0")/.."
OUT="Sources/Voicekey/Config/EmbeddedKeys.generated.swift"

# ターミナル/チャット経由のコピペで -- が – (en dash) や — (em dash) に化ける事故が
# 実際に複数回起きたため、化けたダッシュの変種も受け付ける。
# また、引数の打ち間違いで黙ってスタブを生成すると「できたつもり」事故になるためエラーにする
ARG="${1:-}"
MODE="stub"
case "$ARG" in
    "") MODE="stub" ;;
    --dist | –dist | —dist | –-dist | —-dist) MODE="dist" ;;
    --export-env | –export-env | —export-env | –-export-env | —-export-env) MODE="export-env" ;;
    *)
        echo "エラー: 不明な引数: ${ARG} (使えるのは --dist / --export-env)" >&2
        exit 1
        ;;
esac

if [[ "$MODE" == "stub" ]]; then
    cat > "$OUT" <<'EOF'
//
//  EmbeddedKeys.generated.swift（スタブ・自動生成）
//  通常開発ビルド用。キーは埋め込まれていない。
//  生成元: macos/scripts/generate_embedded_keys.sh（手で編集しない）
//

import Foundation

enum EmbeddedKeys {
    /// 配布（DIST）ビルドかどうか。API キータブの表示判定などに使う
    static let isDist = false

    /// 埋め込みキーを返す（スタブは常に nil）
    static func key(forService service: String) -> String? {
        nil
    }
}
EOF
    echo "==> スタブ生成: $OUT (isDist=false)"
    exit 0
fi

# --dist / --export-env: Keychain から 4 サービスのキーを抽出（取れたものだけ使う。0 件ならエラー）
# キーはコマンドライン引数に載せず環境変数で python に渡す（ps での露出防止）
SERVICES=(voicekey.OpenAI voicekey.Groq voicekey.ElevenLabs voicekey.Deepgram)
FOUND=0
for svc in "${SERVICES[@]}"; do
    var="KEY_${svc//./_}"
    if value="$(security find-generic-password -s "$svc" -a default -w 2>/dev/null)"; then
        export "$var=$value"
        FOUND=$((FOUND + 1))
        echo "==> 取得: $svc"
    else
        export "$var="
        echo "==> 未設定（スキップ）: $svc" >&2
    fi
done

if [[ "$FOUND" -eq 0 ]]; then
    echo "エラー: Keychain から API キーを 1 件も取得できませんでした" >&2
    exit 1
fi

if [[ "$MODE" == "export-env" ]]; then
    # Windows 版ビルド用の .env.dist をリポジトリ直下に書き出す
    # （サービス名 → 環境変数名の対応は scripts/build/generate_embedded_keys.py と揃える）
    ENV_OUT="../.env.dist"
    : > "$ENV_OUT"
    chmod 600 "$ENV_OUT"
    {
        echo "# voicekey 配布ビルド用 API キー（自動生成・git 管理外・コミット絶対禁止）"
        echo "# 生成元: macos/scripts/generate_embedded_keys.sh --export-env"
        if [[ -n "${KEY_voicekey_OpenAI:-}" ]]; then echo "OPENAI_API_KEY=${KEY_voicekey_OpenAI}"; fi
        if [[ -n "${KEY_voicekey_Groq:-}" ]]; then echo "GROQ_API_KEY=${KEY_voicekey_Groq}"; fi
        if [[ -n "${KEY_voicekey_ElevenLabs:-}" ]]; then echo "ELEVENLABS_API_KEY=${KEY_voicekey_ElevenLabs}"; fi
        if [[ -n "${KEY_voicekey_Deepgram:-}" ]]; then echo "DEEPGRAM_API_KEY=${KEY_voicekey_Deepgram}"; fi
    } >> "$ENV_OUT"
    echo "==> 書き出し: $(cd .. && pwd)/.env.dist（$FOUND 件・chmod 600）"
    echo "    Windows ビルド機のリポジトリ直下へ手動コピーし、ビルド後は削除すること"
    exit 0
fi

# XOR 難読化した Swift ソースを生成（マスクは実行ごとにランダム）
python3 - "$OUT" <<'PYEOF'
import os, secrets, sys

out_path = sys.argv[1]
services = ["voicekey.OpenAI", "voicekey.Groq", "voicekey.ElevenLabs", "voicekey.Deepgram"]
mask = secrets.token_bytes(32)

def lit(data: bytes) -> str:
    """バイト列を Swift の配列リテラルにする"""
    return "[" + ", ".join(str(b) for b in data) + "]"

entries = []
for svc in services:
    value = os.environ.get("KEY_" + svc.replace(".", "_"), "")
    if not value:
        continue
    raw = value.encode("utf-8")
    enc = bytes(b ^ mask[i % len(mask)] for i, b in enumerate(raw))
    entries.append(f'        "{svc}": {lit(enc)},')

payload = "\n".join(entries)
src = f'''//
//  EmbeddedKeys.generated.swift（DIST・自動生成）
//  配布ビルド用。開発者の API キーが XOR 難読化されて埋め込まれている。
//  git にコミット絶対禁止（.gitignore 済み）。手で編集しない。
//  生成元: macos/scripts/generate_embedded_keys.sh --dist
//

import Foundation

enum EmbeddedKeys {{
    /// 配布（DIST）ビルドかどうか。API キータブの表示判定などに使う
    static let isDist = true

    /// XOR マスク（生成ごとにランダム）
    private static let mask: [UInt8] = {lit(mask)}

    /// サービス名 → XOR 済みキーバイト列
    private static let payload: [String: [UInt8]] = [
{payload}
    ]

    /// 埋め込みキーを復元して返す（未埋め込みのサービスは nil）
    static func key(forService service: String) -> String? {{
        guard let bytes = payload[service] else {{ return nil }}
        let decoded = bytes.enumerated().map {{ $0.element ^ mask[$0.offset % mask.count] }}
        return String(bytes: decoded, encoding: .utf8)
    }}
}}
'''
with open(out_path, "w", encoding="utf-8") as f:
    f.write(src)
print(f"==> DIST 生成: {out_path} (埋め込み {len(entries)} 件)")
PYEOF
