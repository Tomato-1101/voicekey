# voicekey 競合・類似OSS調査（2026-06-29）

GitHub の有名な音声入力/ディクテーション類似プロジェクトを並列調査し、voicekey に採用できる設計・UX を抽出した。
モデル方針（Groq 追加）の意思決定材料として作成。

## 1. 有名な類似プロジェクト（上位）

| プロジェクト | URL | 種別 | なぜ有名か / 特徴 |
|---|---|---|---|
| **Handy** (cjpais) | github.com/cjpais/handy | OSS・約25k★ | OSSディクテーション最大級。Tauri/Rust・完全ローカル(Whisper/Parakeet)・Silero VAD・CLIトグル(--toggle/--cancel) |
| **VoiceInk** (Beingpax) | github.com/Beingpax/VoiceInk | OSS・約5.4k★・Swift | Mac版voicekeyに最も近い構成。完全ローカル(whisper.cpp+Parakeet)。Power Mode(アプリ別自動設定)+整形+固有名詞辞書。「Wispr/Superwhisperの無料OSS代替」 |
| **Whispering** (Epicenter) | github.com/EpicenterHQ/epicenter | OSS・約4.7k★・MIT | BYOKで Groq/OpenAI/ElevenLabs/Deepgram + ローカル。録音モードを Manual/Push-to-Talk/Toggle/VAD の4択で明示。voicekey **main** と設計思想が最も近い |
| **Wispr Flow** | wisprflow.ai | 商用 | AIディクテーション最前線。p99<700ms・streaming partial(発話中に挿入)・ダブルタップ hands-free・AI整形・View Diff |
| **Superwhisper** | superwhisper.com | 商用 | 商用最大手の一角。「Custom Mode = STT+整形プロンプト+ホットキー+自動起動 を1セットに束ねる」設計が高評価 |
| **OWhisper** (Hyprnote) | github.com/fastrepl/hyprnote | OSS | 「Ollama for realtime STT」。Moonshine(Whisper約5倍速)をローカルで Deepgram互換API化 |
| MacWhisper | goodsnooze | 商用 | Mac音声ツール最有名級。ただしファイル文字起こし寄りで「カーソル即挿入」は弱く競合度は中 |

## 2. voicekey が採用すべきアイデア（優先度順）

1. **Groq whisper-large-v3-turbo を「高速」の核に置く（今回採用）** — 実測 短0.33s/長0.66s。Whispering/Handy も Groq turbo を「near-instant 推奨」に置く業界の最速定番。
2. **モード = 「速度↔精度」軸の2択（今回採用）** — Superwhisper/Handy の「小型高速 vs 大型高精度をユーザーに選ばせる」UX。release 既存の特徴名2択に綺麗に重なる。
3. **Groqプロキシにも暖機GET（今回採用）** — ElevenLabs で入れたコールドスタート対策と同型。初回遅延を消す。
4. （将来）**streaming partial = 確定前 interim をカーソルに先行挿入** — Wispr の p99<700ms 体感の正体。Deepgram ストリーミングは実装済みなので拡張は差分小・効果大。
5. （将来）**アプリ別自動プロファイル(Power Mode/Custom Mode)** — 前面アプリ検出は両OS実装済み。2スロットを「アプリ別に高速/高精度を自動切替」へ拡張可。
6. （将来）**CLIトグル/外部トリガ(--toggle/--cancel)** — Handy の設計。Stream Deck 連携・自動化に強い。差分小。
7. （将来）**固有名詞辞書(Personal Dictionary)** — VoiceInk。Groq整形プロンプトに置換辞書を混ぜるだけで実装可。
8. （将来）**View Diff（整形前後の差分表示）** — Wispr。Groq整形が何を変えたか可視化し、誤整形時の信頼を担保。

## 3. 採用しない方がよいもの（過剰機能・スコープ外）

- **ローカル Whisper/Parakeet/Moonshine の自前搭載** — voicekey は「クラウド固定・GPU不要」が差別化軸。モデルDL・推論基盤・サイズ肥大を抱え製品の単純さを壊す。当面不要。
- **ファイル/メディア一括文字起こし・話者分離・字幕エクスポート** — 用途が「押下中録音→即カーソル挿入」と異なる。機能の散漫化。
- **3択以上へのモード細分化・モデル名露出(release)** — 特徴名2択の明快さを損なう。モデル選択は main ブランチに留める。
- **wake word 常時待受** — PC常駐ディクテーションには過剰。誤起動・常時マイクのプライバシー懸念。

## 4. モデル方針への結論（研究の推奨）

ユーザー要望「速さ最優先で基本 Groq、ハンズフリー長文は精度のいいやつ」を満たす最小差分は、
**モードを増やさず（3択化せず）、既存「高速リアルタイム」の中身を Deepgram nova-3 → Groq turbo に差し替え、「正確性」= ElevenLabs を据え置く**こと。

- 実測ベンチ（2026-06-10・macOS say 合成音声・CER）:
  - Groq whisper-large-v3-turbo … 短 0.33s / 長 0.66s（CER 2.7% / 5.4%）← 全レンジ最速
  - ElevenLabs scribe_v1 … 短 1.1s / 長 3.5s（CER 0% / 0.4%）← 最高精度
  - Deepgram nova-3 … 長 1.5s（CER 4.0%）← 押下中のリアルタイム表示が可能な唯一のモード
- Groq 長文 CER 5.4% は、ユーザーが満足している Deepgram 4.0% とほぼ同等＝「酷くない」。よって採用可。
- トレードオフ: Groq はバッチ（録音停止後POST）のため、Deepgram の「押下中に文字が出るストリーミング表示」は原理的に失われる（停止後 0.33s で一括挿入になる）。
