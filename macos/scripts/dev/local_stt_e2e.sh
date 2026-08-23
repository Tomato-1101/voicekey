#!/bin/bash
# ローカル（Apple）文字起こしの E2E ハーネス
#
# ビルド → say で既知の日本語音声を生成 → dist/voicekey.app を --local-stt-test で起動 →
# 認識テキストに期待語がすべて含まれるかを機械判定して PASS / FAIL を出す。
# **マイクは使わない**（音声ファイルを直接流し込む）ので、無人で回せる。
#
# 使い方:
#   ./scripts/dev/local_stt_e2e.sh              # 単独
#   ./scripts/dev/local_stt_e2e.sh --with-caption   # ライブ字幕と同時に走らせる
#                                                   （SpeechAnalyzer 2 本同時の確認）
#
# なぜ `open` 経由で起動するか:
#   Terminal から直接 exec すると TCC の「責任プロセス」が Terminal になる（字幕側と同じ理由）。
#   その代わり標準出力が捨てられるので --log-file を使う。
set -uo pipefail

cd "$(dirname "$0")/../.."

WITH_CAPTION=""
[ "${1:-}" = "--with-caption" ] && WITH_CAPTION="--with-caption"

LOG="${TMPDIR:-/tmp}/voicekey_local_stt_e2e.log"
AUDIO="${TMPDIR:-/tmp}/voicekey_local_stt_e2e.aiff"
PHRASE="今日は良い天気ですね。音声入力のテストをしています。"
# 判定に使う語（すべて含まれれば PASS）
EXPECT="天気,音声入力,テスト"

fail() {
    echo ""
    echo "RESULT: FAIL — $1"
    echo "--- ログ全文 (${LOG}) ---"
    cat "$LOG" 2>/dev/null || echo "(ログなし)"
    exit 1
}

echo "==> ビルド"
./scripts/build_app.sh >/dev/null || fail "build_app.sh が失敗しました"

echo "==> 音声を生成: ${PHRASE}"
rm -f "$AUDIO" "$LOG"
say -v Kyoko -o "$AUDIO" "$PHRASE" || fail "say で音声を生成できませんでした"

echo "==> voicekey を --local-stt-test で起動"
open -n dist/voicekey.app --args \
    --local-stt-test "$AUDIO" --expect "$EXPECT" $WITH_CAPTION --log-file "$LOG" \
    || fail "アプリを起動できませんでした"

# 初回は言語モデルのダウンロードが走るため長めに待つ
WAITED=0
while [ "$WAITED" -lt 900 ]; do
    if [ -f "$LOG" ] && grep -qF "[DONE]" "$LOG"; then break; fi
    sleep 2
    WAITED=$((WAITED + 2))
    if [ $((WAITED % 20)) -eq 0 ]; then
        echo "    ...認識を待機中 (${WAITED}s) 直近: $(tail -n 1 "$LOG" 2>/dev/null)"
    fi
done
grep -qF "[DONE]" "$LOG" 2>/dev/null || fail "[DONE] が出ませんでした（タイムアウト）"

echo ""
echo "==> 判定"
grep -E "^\[(INFO|ASSET|TEXT|FINALIZE-MS|MATCH|DONE|ERROR|WARN)\]" "$LOG"

DONE_LINE="$(grep '\[DONE\]' "$LOG" | tail -n 1)"
if ! echo "$DONE_LINE" | grep -q "status=ok"; then
    fail "認識結果が期待と一致しませんでした: ${DONE_LINE}"
fi

echo ""
echo "RESULT: PASS"
echo "  $(grep '\[TEXT\]' "$LOG" | tail -n 1)"
echo "  $DONE_LINE"
echo "  ログ: ${LOG}"
exit 0
