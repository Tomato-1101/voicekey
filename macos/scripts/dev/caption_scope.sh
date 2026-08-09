#!/bin/bash
# voicekey のキャプチャ対象の絞り込みテスト
#
# 2 つの別プロセス（どちらも `say`）から同時に別々の英文を鳴らし、
# 片方だけを対象にしたときに
#   (A) 対象プロセスの合い言葉が認識テキストに出る（＝対象の音は拾えている）
#   (B) 対象外プロセスの合い言葉が出ない（＝絞り込みが効いている）
# を機械判定する。
#
# RMS では「2 つの音が同時に鳴っている環境で対象側だけを拾えたか」を区別できないため、
# TTS 再キャプチャ検証と同じ合い言葉方式にしている。
#
# 使い方: ./test_scope.sh
#
# `open` 経由で起動する理由は test_e2e.sh と同じ（TCC の責任プロセスを voicekey 自身にする）。
set -uo pipefail

cd "$(dirname "$0")/../.."

LOG="${TMPDIR:-/tmp}/voicekey_caption_scope.log"
# 対象外の語がこの数以上出たら「混ざっている」と判定する
MAX_OTHER_HITS=2
# 対象の語がこの数以上出ないと「そもそも拾えていない」ため判定不能にする
MIN_TARGET_HITS=2

fail() {
    echo ""
    echo "RESULT: FAIL — $1"
    echo "--- ログ全文 (${LOG}) ---"
    cat "$LOG" 2>/dev/null || echo "(ログなし)"
    exit 1
}

inconclusive() {
    echo ""
    echo "RESULT: INCONCLUSIVE — $1"
    echo "--- ログ全文 (${LOG}) ---"
    cat "$LOG" 2>/dev/null || echo "(ログなし)"
    exit 2
}

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

echo "==> ビルド"
./scripts/build_app.sh >/dev/null || fail "build_app.sh が失敗しました"

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
echo "==> voicekey を --caption-scope-test で起動（2 プロセス同時再生 → 片方だけを対象）"
open -n dist/voicekey.app --args --caption-scope-test --log-file "$LOG" \
    || fail "アプリを起動できませんでした"

if ! wait_for "[VERDICT]" 240 "テスト完了"; then
    pkill -x voicekey 2>/dev/null || true
    fail "[VERDICT] が出ませんでした（タイムアウト）"
fi
pkill -x voicekey 2>/dev/null || true

echo ""
echo "==> 判定"
VERDICT="$(grep '\[VERDICT\]' "$LOG" | tail -n 1)"
echo "    $VERDICT"

if echo "$VERDICT" | grep -q "status=error"; then
    fail "テストがエラー終了しました"
fi
if echo "$VERDICT" | grep -q "status=inconclusive"; then
    inconclusive "$(echo "$VERDICT" | sed -n 's/.*reason=//p')"
fi

TARGET_HITS="$(echo "$VERDICT" | sed -n 's/.*targetHits=\([0-9]*\).*/\1/p')"
OTHER_HITS="$(echo "$VERDICT" | sed -n 's/.*otherHits=\([0-9]*\).*/\1/p')"
grep '\[KEYWORD\]' "$LOG" | sed 's/^/    /'
grep '\[TARGET\]' "$LOG" | sed 's/^/    /'

echo "    (A) 対象プロセスの合い言葉: ${TARGET_HITS}（${MIN_TARGET_HITS} 以上で「拾えている」）"
echo "    (B) 対象外プロセスの合い言葉: ${OTHER_HITS}（${MAX_OTHER_HITS} 以上で「混ざっている」）"

if [ "${TARGET_HITS:-0}" -lt "$MIN_TARGET_HITS" ]; then
    inconclusive "対象プロセスの音自体が認識できていません（絞り込みの成否を判定できません）"
fi
if [ "${OTHER_HITS:-0}" -ge "$MAX_OTHER_HITS" ]; then
    fail "対象外プロセスの音が混ざっています (${OTHER_HITS} 語)"
fi

echo ""
echo "RESULT: PASS — 対象プロセスだけを拾えています（対象 ${TARGET_HITS} 語 / 対象外 ${OTHER_HITS} 語）"
echo "  ログ: ${LOG}"
exit 0
