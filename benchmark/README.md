# voicekey 文字起こしベンチマーク

同一の日本語音声（短文・長文）を OpenAI / Groq / ElevenLabs / Deepgram の各モデルに
送り、**レイテンシ（API 往復時間）** と **CER（文字誤り率）** を測定する。

## 使い方

```bash
cd benchmark

# 1. テスト音声を生成（say で合成 → 16kHz mono WAV。原稿が正解テキストを兼ねる）
bash make_audio.sh

# 2. API キーを用意
#    OpenAI / Groq / ElevenLabs は voicekey アプリの Keychain にあれば自動取得。
#    Deepgram など未保存のキーは .env に書くか、アプリの設定 → API キータブで保存。
cp env.example .env   # 必要なら編集

# 3. 実行
python3 run_benchmark.py
```

## 仕組み

- **音声**: `make_audio.sh` が `audio/short_ja.txt` `audio/long_ja.txt` を macOS の
  `say`（Kyoko）で読み上げ、`afconvert` で 16kHz モノラル WAV に変換する。
  原稿 `.txt` がそのまま CER 採点の正解になる。
- **キー取得**: 環境変数 / `.env` → Keychain（`voicekey.OpenAI` など、アプリと共用）の順。
  キーの中身は画面に出さない（「キーあり / なし」だけ表示）。
- **CER**: 句読点・空白・記号を除去（長音「ー」は残す）した文字列の編集距離 ÷ 正解文字数。
  小さいほど高精度。
- **レイテンシ**: 各モデル `RUNS` 回（既定 2 回）測り最速値を採用。
- 認識結果の全文は `results/result_*.json` に保存される（git 管理外）。

## 注意

- 合成音声（say）のため、実マイクのノイズ環境は再現しない。**モデル間の相対比較**として読む。
- `.env`・`audio/*.wav`・`results/` は `.gitignore` 済み。原稿 `.txt` とスクリプトのみコミットする。
