#!/bin/bash
# say で日本語テスト音声を生成し、16kHz モノラル WAV に変換する。
# 原稿（audio/*_ja.txt）はベンチマークの正解テキストを兼ねる。
set -euo pipefail
cd "$(dirname "$0")/audio"

VOICE="Kyoko"   # 日本語音声（say -v '?' で一覧）

for name in short long; do
    echo "==> ${name}_ja: say (${VOICE}) → 16kHz mono WAV"
    say -v "$VOICE" -f "${name}_ja.txt" -o "/tmp/voicekey_${name}.aiff"
    afconvert -f WAVE -d LEI16@16000 -c 1 "/tmp/voicekey_${name}.aiff" "${name}_ja.wav"
    rm -f "/tmp/voicekey_${name}.aiff"
    afinfo "${name}_ja.wav" | grep "estimated duration" || true
done
echo "==> 完了: audio/short_ja.wav, audio/long_ja.wav"
