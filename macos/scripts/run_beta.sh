#!/bin/bash
# ベータ版（API キー埋め込み・テスターに配っているものと同一の DMG）を一時起動して
# 動作確認する。
#
# 使い方:
#   ./scripts/run_beta.sh            # dist/ にある最新の DMG を起動
#
# 同じアプリ（bundle ID）を 2 つ同時に起動するとホットキーとマイクを取り合うため、
# 起動中の voicekey は先に終了する。確認が終わったら ./scripts/run_dev.sh で開発版に戻る。
set -euo pipefail

cd "$(dirname "$0")/.."

DMG="$(ls -t dist/voicekey-*.dmg 2>/dev/null | head -n 1)"
[[ -n "$DMG" ]] || { echo "エラー: dist/ に DMG がありません（build_dmg.sh で作成するか、配布サイトから取得）" >&2; exit 1; }

pkill -x voicekey 2>/dev/null || true
if [[ ! -d /Volumes/voicekey ]]; then
    hdiutil attach "$DMG" >/dev/null
fi
open /Volumes/voicekey/voicekey.app

echo ""
echo "==> ベータ版を起動しました: $DMG（読み取り専用マウント）"
echo "    （設定画面に API キータブが無いのがベータ版の目印）"
echo "    確認が終わったら: ./scripts/run_dev.sh で開発版に戻る（マウントは自動で外れます）"
