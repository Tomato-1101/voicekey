#!/bin/bash
# voicekey の E2E ハーネス
#
# ビルド → dist/voicekey.app を --caption-pipeline-test で起動 → say で英語音声を再生 →
# (a) RMS が無音時より十分大きい (b) 認識テキストに再生文の主要語が含まれる
# (c) 翻訳結果に日本語（ひらがな/カタカナ/漢字）が含まれる
# を機械判定して PASS / FAIL を出す。
#
# (c) は翻訳エンジンが使える場合のみ。Apple 翻訳の言語モデルが未ダウンロードだと
# 翻訳は失敗するため、その場合は PASS ではなく PASS-NO-TRANSLATION として区別する
# （黙って PASS にすると「訳が出ない」ことに気付けない）。
#
# 使い方:
#   ./test_e2e.sh [キャプチャ秒数(既定 20)]
#   VOICEKEY_CAPTION_TRANSLATOR=mock ./test_e2e.sh   # 外部通信なしで翻訳結線だけ確認
#
# なぜ `open` 経由で起動するか:
#   Terminal から直接バイナリを exec すると TCC の「責任プロセス」が Terminal になり、
#   システム音声録音の許可が Terminal に紐づいてしまう。LaunchServices 経由なら
#   voicekey 自身に紐づく。その代わり標準出力が捨てられるので --log-file を使う。
set -uo pipefail

cd "$(dirname "$0")/../.."

DURATION="${1:-20}"
LOG="${TMPDIR:-/tmp}/voicekey_caption_e2e.log"
PHRASE="Hello, this is a live translation test. The weather is nice today."
# 判定に使う主要語（この個数以上一致で合格とする）
KEYWORDS=(hello live translation test weather nice today)
MIN_KEYWORD_HITS=4
# 無音との差の判定しきい値（RMS）
MIN_RMS=0.002

fail() {
    echo ""
    echo "RESULT: FAIL — $1"
    echo "--- ログ全文 (${LOG}) ---"
    cat "$LOG" 2>/dev/null || echo "(ログなし)"
    exit 1
}

blocked() {
    echo ""
    echo "RESULT: TCC-BLOCKED — $1"
    echo "  システム設定 > プライバシーとセキュリティ > システムオーディオ録音 で"
    echo "  「voicekey」を許可してから再実行してください。"
    echo "--- ログ全文 (${LOG}) ---"
    cat "$LOG" 2>/dev/null || echo "(ログなし)"
    exit 2
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
echo "==> voicekey を --caption-pipeline-test ${DURATION} で起動"
# VOICEKEY_CAPTION_SCOPE=all を明示するのは、既定が「最前面のアプリだけ」になったため。
# このハーネスは `say`（別プロセス）の音を拾う前提なので、従来モードで回す。
# VOICEKEY_CAPTION_TRANSLATOR が指定されていればアプリ側へ引き継ぐ（モック翻訳での結線確認用）。
# 空配列の展開は macOS 標準の bash 3.2 + `set -u` で落ちるため、分岐で書く。
if [ -n "${VOICEKEY_CAPTION_TRANSLATOR:-}" ]; then
    open -n --env "VOICEKEY_CAPTION_SCOPE=all" --env "VOICEKEY_CAPTION_TRANSLATOR=${VOICEKEY_CAPTION_TRANSLATOR}" dist/voicekey.app \
        --args --caption-pipeline-test "$DURATION" --log-file "$LOG" \
        || fail "アプリを起動できませんでした"
else
    open -n --env "VOICEKEY_CAPTION_SCOPE=all" dist/voicekey.app \
        --args --caption-pipeline-test "$DURATION" --log-file "$LOG" \
        || fail "アプリを起動できませんでした"
fi

# 言語モデル未導入だとダウンロードで数分かかるため長めに待つ
if ! wait_for "[READY]" 900 "音声認識の準備"; then
    if grep -q "\[ERROR\]" "$LOG" 2>/dev/null; then
        fail "パイプラインの開始に失敗: $(grep '\[ERROR\]' "$LOG" | head -n 1)"
    fi
    fail "[READY] が出ませんでした（起動またはアセット取得で停止）"
fi

echo "==> 無音の基準線を測るため 3 秒待機"
sleep 3

echo "==> 英語音声を再生: ${PHRASE}"
say -v Samantha "$PHRASE"

echo "==> テスト終了を待機"
if ! wait_for "[DONE]" "$((DURATION + 60))" "テスト完了"; then
    pkill -x voicekey 2>/dev/null || true
    fail "[DONE] が出ませんでした（タイムアウト）"
fi

echo ""
echo "==> 判定"
DONE_LINE="$(grep '\[DONE\]' "$LOG" | tail -n 1)"
echo "    $DONE_LINE"

if echo "$DONE_LINE" | grep -q "status=error"; then
    fail "パイプラインがエラー終了しました: $(grep '\[ERROR\]' "$LOG" | head -n 1)"
fi

MAX_RMS="$(echo "$DONE_LINE" | sed -n 's/.*maxRMS=\([0-9.]*\).*/\1/p')"
BASE_RMS="$(echo "$DONE_LINE" | sed -n 's/.*baselineRMS=\([0-9.]*\).*/\1/p')"
FRAMES="$(echo "$DONE_LINE" | sed -n 's/.*frames=\([0-9]*\).*/\1/p')"

if [ -z "${FRAMES:-}" ] || [ "$FRAMES" -eq 0 ]; then
    blocked "タップからフレームが 1 つも届いていません"
fi

# (a) RMS 判定
#   基本は絶対値（真の無音なら RMS は 1e-6 未満になる）。
#   さらに開始直後が無音だった場合に限り「say で立ち上がったこと」も確認する。
#   テスト中に他アプリの音が鳴っていると基準線が無音でなくなるため、比率判定は条件付きにする。
RMS_OK="$(awk -v m="$MAX_RMS" -v b="$BASE_RMS" -v t="$MIN_RMS" 'BEGIN {
    if (m < t)  { print "0"; exit }   # そもそも音が乗っていない
    if (b < t)  { print (m > b * 5) ? "1" : "0"; exit }  # 静かな環境: 立ち上がりを見る
    print "1"                                            # 既に他の音が鳴っている: 絶対値のみで判定
}')"
if awk -v b="$BASE_RMS" -v t="$MIN_RMS" 'BEGIN { exit (b < t) ? 1 : 0 }'; then
    echo "    (注) 開始時点で他の音声が鳴っています (baseline=${BASE_RMS})。立ち上がり比率の判定は省略します。"
fi
echo "    (a) RMS: max=${MAX_RMS} baseline=${BASE_RMS} しきい値=${MIN_RMS} → $([ "$RMS_OK" = "1" ] && echo OK || echo NG)"

if [ "$RMS_OK" != "1" ]; then
    # 許可が無い場合、タップは動くが常に無音（全ゼロ）になる
    ALL_ZERO="$(awk -v m="$MAX_RMS" 'BEGIN { print (m < 0.000001) ? "1" : "0" }')"
    if [ "$ALL_ZERO" = "1" ]; then
        blocked "フレームは届くが全サンプルがゼロです（システム音声録音の許可が未付与の可能性が高い）"
    fi
    fail "音声レベルが不足しています (maxRMS=${MAX_RMS})"
fi

# (b) 認識テキスト判定: 確定行を優先し、無ければ途中経過も見る
RECOGNIZED="$(grep '\[FINAL\]' "$LOG" | sed 's/^\[FINAL\] //' | tr '\n' ' ')"
if [ -z "$(echo "$RECOGNIZED" | tr -d '[:space:]')" ]; then
    RECOGNIZED="$(grep '\[VOLATILE\]' "$LOG" | sed 's/^\[VOLATILE\] //' | tr '\n' ' ')"
fi
LOWER="$(echo "$RECOGNIZED" | tr '[:upper:]' '[:lower:]')"

HITS=0
MATCHED=""
for word in "${KEYWORDS[@]}"; do
    if echo "$LOWER" | grep -q "$word"; then
        HITS=$((HITS + 1))
        MATCHED="${MATCHED}${word} "
    fi
done
echo "    (b) 認識テキスト: \"${RECOGNIZED}\""
echo "        一致語 ${HITS}/${#KEYWORDS[@]} (必要 ${MIN_KEYWORD_HITS}): ${MATCHED}"

if [ "$HITS" -lt "$MIN_KEYWORD_HITS" ]; then
    fail "認識テキストに主要語が足りません (${HITS}/${MIN_KEYWORD_HITS})"
fi

# (c) 翻訳判定: 訳文に日本語（ひらがな/カタカナ/漢字）が含まれるか
ENGINE="$(echo "$DONE_LINE" | sed -n 's/.*engine=\([a-z]*\).*/\1/p')"
TRANSLATED_COUNT="$(echo "$DONE_LINE" | sed -n 's/.*translated=\([0-9]*\).*/\1/p')"
TRANSLATE_FAILS="$(echo "$DONE_LINE" | sed -n 's/.*translateFails=\([0-9]*\).*/\1/p')"
JA_TEXT="$(grep '\[JA\]' "$LOG" | tail -n 1 | sed 's/^\[JA\] //')"
# LC_ALL=C で日本語文字（UTF-8 の3バイト表現）の有無を見る
JA_HIT=0
echo "$JA_TEXT" | grep -qE '[ぁ-んァ-ヶ一-龥]' && JA_HIT=1

echo "    (c) 翻訳エンジン=${ENGINE:-?} 成功=${TRANSLATED_COUNT:-0} 失敗=${TRANSLATE_FAILS:-0}"
echo "        訳文: \"${JA_TEXT}\""

if [ "${TRANSLATED_COUNT:-0}" -eq 0 ]; then
    echo ""
    echo "RESULT: PASS-NO-TRANSLATION — キャプチャと認識は正常。翻訳は 1 件も成立しませんでした"
    echo "  最初の失敗理由: $(grep '\[TRANSLATE-FAIL\]' "$LOG" | head -n 1 | sed 's/^\[TRANSLATE-FAIL\] //')"
    echo "  Apple 翻訳の場合はメニューから字幕を開始して、英→日の言語ダウンロードを許可してください。"
    echo "  maxRMS=${MAX_RMS} (無音基準 ${BASE_RMS}) / 一致語 ${HITS}"
    echo "  ログ: ${LOG}"
    exit 3
fi

if [ "$JA_HIT" -ne 1 ]; then
    fail "翻訳は成功しているのに訳文へ日本語が含まれていません"
fi

# (d) 全文保証: 確定した文がひとつ残らず翻訳されたか
#     アプリ側が「確定テキストを繋いだもの」と「実際に訳した原文を繋いだもの」を
#     突き合わせて [COVERAGE] に出している。1 文字でも欠けたら FAIL。
COVERAGE_LINE="$(grep '\[COVERAGE\]' "$LOG" | tail -n 1)"
echo "    (d) 全文保証: ${COVERAGE_LINE:-(出力なし)}"
if [ -z "$COVERAGE_LINE" ]; then
    fail "[COVERAGE] が出力されていません"
fi
if ! echo "$COVERAGE_LINE" | grep -q "status=ok"; then
    echo "        欠落: $(grep '\[COVERAGE-MISSING\]' "$LOG" | tail -n 1 | sed 's/^\[COVERAGE-MISSING\] //')"
    fail "確定した文の一部が翻訳・表示されていません（全文保証に違反）"
fi

echo ""
echo "RESULT: PASS"
echo "  maxRMS=${MAX_RMS} (無音基準 ${BASE_RMS}) / 一致語 ${HITS} / 訳文 ${TRANSLATED_COUNT} 件"
echo "  全文保証: $(echo "$COVERAGE_LINE" | sed 's/^\[COVERAGE\] //')"
echo "  ログ: ${LOG}"
exit 0
