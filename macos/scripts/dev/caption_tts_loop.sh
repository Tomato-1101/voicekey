#!/bin/bash
# voicekey の TTS 再キャプチャループ検証ハーネス
#
# CLAUDE.md の不変条件「TTS 再キャプチャループ禁止」を数値で確かめる。
# アプリを --caption-tts-loop-test で起動すると、1 回のキャプチャの中で
#   baseline(無音) → selftone(自プロセスの再生) → gap → tts(自プロセスの読み上げ)
#   → settle(無音) → control(外部 say)
# を測ってログに出す。このスクリプトはその結果を機械判定する。
#
# 判定（主判定は「合い言葉」。環境音に左右されない）:
#   (A) control 窓で合い言葉が認識される … 検出器とタップが生きている証拠（前提条件）
#   (B) tts 窓で合い言葉が認識されない   … 自プロセスの読み上げを拾っていない（本題）
#   (C) RMS は参考値として表示するだけ。他アプリが鳴っている環境では基準線が汚れて
#       判別できないため（実測で基準線が 0.006〜0.18 の間で揺れた）判定には使わない。
#
# 使い方:
#   ./test_tts_loop.sh
set -uo pipefail

cd "$(dirname "$0")/../.."

LOG="${TMPDIR:-/tmp}/voicekey_caption_tts_loop.log"
# control 窓でこの数以上の合い言葉が認識されれば「検出器は生きている」とみなす
MIN_CONTROL_HITS=2
# tts 窓でこの数以上の合い言葉が認識されたら「自分の読み上げを拾っている」とみなす
# （1 語だけなら他アプリの音声にたまたま含まれた可能性を排除できない）
LEAK_HITS=2

finish() {
    echo ""
    echo "RESULT: $1 — $2"
    echo "  ログ: ${LOG}"
    exit "$3"
}

# ログに指定パターンが現れるまで待つ
# 引数: パターン 最大待ち秒数 説明
wait_for() {
    local pattern="$1" limit="$2" what="$3" waited=0
    while [ "$waited" -lt "$limit" ]; do
        if [ -f "$LOG" ] && grep -qF "$pattern" "$LOG"; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
        if [ $((waited % 10)) -eq 0 ]; then
            echo "    ...${what} を待機中 (${waited}s) 直近: $(tail -n 1 "$LOG" 2>/dev/null)"
        fi
    done
    return 1
}

# フェーズ結果行から値を取り出す
# 引数: フェーズ名 キー名
phase_value() {
    grep "\[PHASE-RESULT\] phase=$1 " "$LOG" | tail -n 1 | sed -n "s/.*$2=\([0-9.]*\).*/\1/p"
}

echo "==> ビルド"
./scripts/build_app.sh >/dev/null || finish FAIL "build_app.sh が失敗しました" 1

echo "==> 出力音量を確認"
VOLUME_BEFORE="$(osascript -e 'output volume of (get volume settings)' 2>/dev/null || echo 50)"
MUTED_BEFORE="$(osascript -e 'output muted of (get volume settings)' 2>/dev/null || echo false)"
RESTORE_VOLUME=0
if [ "$MUTED_BEFORE" = "true" ] || [ "$VOLUME_BEFORE" -lt 10 ]; then
    echo "    ミュート/音量不足のため一時的に 25 へ変更します（テスト後に復元）"
    osascript -e "set volume output volume 25 without output muted" >/dev/null 2>&1
    RESTORE_VOLUME=1
fi
restore_volume() {
    if [ "$RESTORE_VOLUME" = "1" ]; then
        osascript -e "set volume output volume ${VOLUME_BEFORE}" >/dev/null 2>&1
        [ "$MUTED_BEFORE" = "true" ] && osascript -e "set volume with output muted" >/dev/null 2>&1
    fi
}
trap restore_volume EXIT

echo "==> 旧プロセスを終了"
pkill -x voicekey 2>/dev/null || true

rm -f "$LOG"
echo "==> voicekey を --caption-tts-loop-test で起動"
# `open` 経由なのは TCC の責任プロセスを voicekey 自身にするため（test_e2e.sh と同じ理由）
# VOICEKEY_CAPTION_SCOPE=all を明示する。既定は「最前面のアプリだけ」だが、この検証は
# 「すべてを拾うタップでも自分の読み上げは入らない」ことを確かめるものなので従来モードで回す。
open -n --env "VOICEKEY_CAPTION_SCOPE=all" dist/voicekey.app --args --caption-tts-loop-test --log-file "$LOG" \
    || finish FAIL "アプリを起動できませんでした" 1

if ! wait_for "[READY]" 900 "音声認識の準備"; then
    grep -q "\[ERROR\]" "$LOG" 2>/dev/null \
        && finish FAIL "パイプラインの開始に失敗: $(grep '\[ERROR\]' "$LOG" | head -n 1)" 1
    finish FAIL "[READY] が出ませんでした" 1
fi

echo "==> 4 フェーズの計測を待機（読み上げが2回鳴ります）"
if ! wait_for "[VERDICT]" 180 "計測完了"; then
    pkill -x voicekey 2>/dev/null || true
    finish FAIL "[VERDICT] が出ませんでした（タイムアウト）" 1
fi

echo ""
echo "==> 計測結果"
grep -E "\[PHASE-RESULT\]|\[PHASE-PROC\]|\[PHASE-TEXT\]" "$LOG" | sed 's/^/    /'
grep -E "\[TTS-NEW-PROC\]|\[CONTROL-NEW-PROC\]|\[RENDER\]|\[KEYWORD\]" "$LOG" | sed 's/^/    /'
VERDICT="$(grep '\[VERDICT\]' "$LOG" | tail -n 1)"
echo "    $VERDICT"

echo "$VERDICT" | grep -q "status=error" \
    && finish FAIL "計測がエラー終了しました: $(grep '\[ERROR\]' "$LOG" | head -n 1)" 1

BASE_RMS="$(phase_value baseline maxRMS)"
SELFTONE_RMS="$(phase_value selftone maxRMS)"
TTS_RMS="$(phase_value tts maxRMS)"
CONTROL_RMS="$(phase_value control maxRMS)"
CONTROL_FRAMES="$(phase_value control frames)"
TTS_HITS="$(echo "$VERDICT" | sed -n 's/.*ttsKeywordHits=\([0-9]*\).*/\1/p')"
CONTROL_HITS="$(echo "$VERDICT" | sed -n 's/.*controlKeywordHits=\([0-9]*\).*/\1/p')"
FOREIGN="$(echo "$VERDICT" | sed -n 's/.*foreignTTSOutput=\([0-9]*\).*/\1/p')"

[ -z "${CONTROL_FRAMES:-}" ] && finish FAIL "control フェーズの計測行がありません" 1
[ "$CONTROL_FRAMES" -eq 0 ] \
    && finish INCONCLUSIVE "control フェーズでタップからフレームが届いていません（システム音声録音の許可を確認）" 2

echo ""
echo "==> 判定"

# (A) 前提条件: 外部プロセスの読み上げは認識できているか（検出器とタップの生存確認）
echo "    (A) control 窓の合い言葉: ${CONTROL_HITS:-0}/${MIN_CONTROL_HITS} 必要 → $([ "${CONTROL_HITS:-0}" -ge "$MIN_CONTROL_HITS" ] && echo OK || echo NG)"
[ "${CONTROL_HITS:-0}" -lt "$MIN_CONTROL_HITS" ] && finish INCONCLUSIVE \
    "外部プロセスの読み上げすら認識されていません（タップ停止・音量ゼロ・認識失敗のいずれか）。この状態では除外の効きを判定できません" 2

# (B) 本題: 自プロセスの読み上げが認識テキストへ混入していないか
echo "    (B) tts 窓の合い言葉:     ${TTS_HITS:-0}（${LEAK_HITS} 以上で混入と判定） → $([ "${TTS_HITS:-0}" -lt "$LEAK_HITS" ] && echo OK || echo NG)"
[ "${TTS_HITS:-0}" -ge "$LEAK_HITS" ] && finish FAIL \
    "自プロセスの読み上げが認識テキストに混入しています（再キャプチャループが起きます）" 1

# (C) 参考値
echo "    (C) 参考 RMS: baseline=${BASE_RMS} selftone=${SELFTONE_RMS} tts=${TTS_RMS} control=${CONTROL_RMS}"
echo "        読み上げ中に出力した他プロセス: ${FOREIGN:-?} 件"

finish PASS "自プロセス除外が効いています（tts窓の合い言葉 ${TTS_HITS:-0} 語 / control窓 ${CONTROL_HITS:-0} 語）" 0
