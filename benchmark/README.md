# voicekey 文字起こしベンチマーク

同一の日本語音声を OpenAI / Groq / ElevenLabs / Deepgram / Gemini の各モデルに
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

## 難易度の高いテストセット（2026-08-27 追加）

`short` / `long` は**ゆっくり・雑音なし・平易な語彙**なので、いまどきのモデルはどれも
ほぼ同じテキストを返す（実測で Groq も Gemini も完全一致）。モデル差を見るには
`make_hard_audio.py` で作る難しい音声を使う。

```bash
python3 make_hard_audio.py                         # hard1/hard2 + 早口・雑音版を生成
../venv/bin/python run_benchmark.py hard2 hard2_fast_noisy   # REST 各モデル
.venv/bin/python stream_benchmark.py hard2 hard2_fast_noisy  # ストリーミング各モデル
../venv/bin/python rescore.py --clip hard2                   # 保存済み結果を再採点
```

- **hard2**（数字・日付・金額・人名・部屋番号）: **これが本命の弁別テスト**。
  誤ると実害が大きい領域で、モデルの得手不得手がはっきり割れる。
- **`*_fast_noisy`**: 同じ原稿を速度 250 で読ませ、ピンクノイズを SNR 10dB で重ねたもの。
  実マイク環境の劣化耐性を見る。原稿（正解）は素の版と共有する。
- **hard1**（英日混在の技術口述）は **`say` では測定できない**。合成音声が
  `AppController` を「アコントローラー」、`Vercel` を「バーサル」、`明日` を「アス」と
  読んでしまい、どのモデルも忠実に書き起こした結果 CER 40〜56% に張り付く
  （＝ TTS の発音を測っているだけでモデルの差が出ない）。この軸を測りたいときは
  **人間が実際に読んだ録音**を用意すること。原稿は将来のために残してある。

## 採点の注意: 表記の癖を誤りに数えない（重要）

素の CER は**モデルの表記の癖**を誤り扱いする。実測（2026-08-27）で ElevenLabs は
認識自体は完璧なのに数字を漢数字で返すため CER 33% と出た。voicekey は貼り付け前に
`NumeralNormalizer`（漢数字 → 算用数字）を必ず通すので、ユーザーが受け取るテキストは正しい。
**アプリと同じ正規化を通してから比べる**こと（`rescore.py` / `stream_benchmark.py` は対応済み）。
同じ理由で「12.7% と 12.7 パーセント」「支払期限 と 支払い期限」も潰して比べる。

## 注意

- 合成音声（say）のため、実マイクのノイズ環境は再現しない。**モデル間の相対比較**として読む。
- `.env`・`audio/*.wav`・`results/` は `.gitignore` 済み。原稿 `.txt` とスクリプトのみコミットする。
