#!/bin/bash
# voicekey の翻訳エンジン一括ベンチ（手動実行のみ）
#
# 実ログから採った固定 10 文（音声認識の聞き間違いを 7 文含む）を
#   Apple / Groq / gemini-3.5-flash-lite / gemini-3.5-flash / gemini-3.6-flash / OpenAI
# へ **1 文につき 1 回だけ** 投げ、訳文・遅延（中央値/最大）・トークン・概算費用（円）を並べる。
#
# 【重要】課金が発生する。以下の上限はアプリ側（BenchTestRunner）で強制している:
#   - 各エンジン各文 1 回・自動リトライは 1 回まで・ループしない
#   - API リクエスト総数 80 件まで（うち Gemini 系は 35 件まで）
#   - Apple はオンデバイスなので上限には数えない（無料）
# **cron / launchd に入れないこと**（毎回課金される）。
#
# API キーは APIKeyStore の探索順（環境変数 → 中央 Keychain(service=変数名, account=shared)
# → アプリ Keychain(service=voicekey) → .env）で拾う。キーが無いエンジンは SKIP と出て、
# 後からキーを入れれば同じスクリプトで追試できる。
#
# 使い方:
#   ./bench_translators.sh              # ベンチ本体
#   ./bench_translators.sh --models     # Groq のモデル一覧＋候補 3 文の小テスト（選定用）
set -uo pipefail

cd "$(dirname "$0")/../.."

LOG="${TMPDIR:-/tmp}/voicekey_caption_bench.log"
MODE="bench"
if [ "${1:-}" = "--models" ]; then
    MODE="models"
    LOG="${TMPDIR:-/tmp}/voicekey_caption_groq_models.log"
fi

echo "==> ビルド"
./scripts/build_app.sh >/dev/null || { echo "RESULT: FAIL — build_app.sh が失敗しました"; exit 1; }

rm -f "$LOG"

if [ "$MODE" = "models" ]; then
    echo "==> Groq のモデル一覧と候補 3 文の小テスト"
    ./dist/voicekey.app/Contents/MacOS/voicekey --caption-groq-models --log-file "$LOG"
    STATUS=$?
    echo ""
    echo "  ログ: ${LOG}"
    exit $STATUS
fi

echo "==> 翻訳エンジンのベンチ（固定 10 文 × 各エンジン 1 回）"
# `open` ではなく直接実行する。GUI も音声キャプチャも使わないので TCC の責任プロセスを
# 気にする必要がなく、標準出力をそのまま読めた方が結果を確認しやすい。
./dist/voicekey.app/Contents/MacOS/voicekey --caption-bench --log-file "$LOG"
STATUS=$?

echo ""
echo "==> 集計"
grep '\[SUMMARY\]' "$LOG" | tail -n 20 | sed 's/^/    /'
grep '\[COST\]' "$LOG" | sed 's/^/    /'

if [ "$STATUS" -ne 0 ]; then
    echo ""
    echo "RESULT: FAIL — 実行できたエンジンがありません（APIキー未設定 / Apple 言語モデル未導入）"
    echo "  ログ: ${LOG}"
    exit 1
fi

echo ""
echo "RESULT: PASS — ベンチを実行しました"
echo "  訳文の比較は ログの [COMPARE] 以降を参照: ${LOG}"
exit 0
