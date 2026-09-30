# HANDOFF — voicekey（2026-10-01 更新）

旧版（ベータ配布計画・履歴同期の実装メモ、537 行）は git 履歴 `c0819b6` の HANDOFF.md を参照。
履歴同期の仕様の正本は `docs/HISTORY_SYNC.md`。

## 目的
(1) Mac 版の重さ・ハングを止める（完了）。(2) STT モデル選定（実測は承認待ち）。(3) ロゴ・配色の刷新（24 案提示・本人の選択待ち）。

## 現状
- 録音部は入力専用 AUHAL 1 個の使い回し（7498144）。6 日稼働で footprint 約 120MB・leaks 32 バイト・異常 0。
  coreaudiod は 10-01 時点 60MB・CPU 2%（再起動不要）。
- STT 調査（10-01・API 課金なし）: 第三者ベンチ（Artificial Analysis・Pipecat）は英語データのみで日本語の選定に使えない。
  自前実測（benchmark/results の 08-27・`say` 合成音・アプリ同等の数字正規化後 CER）では
  REST は Groq whisper-large-v3-turbo が数字・日付で最良（hard2 0.0%／雑音 1.2%）、ストリーミングは
  gpt-live-transcribe が最良精度（3.6／4.1%・確定 0.68s）、Deepgram nova-3 が最速（確定ほぼ 0s だが雑音 21%）。
- 現在の常用は openai_live（離鍵→貼付 約 750ms）。Groq は約 200ms。バックエンド切替は本人の判断（Claude は変えない）。

## 次にやること
- ロゴ: 案はキャンバス https://claude.ai/artifact/SJtFByb9SXyDdwEt6brp2j（01–04 は独自案、05–12 は巨匠オマージュ: Rams/Vignelli/Wyman/原研哉/Kare/Rand/Bayer/Aicher。推奨は 01 CARET、次点 08 YOHAKU・05 GRILLE）。本人の好みは 02 KEYCAP・08 YOHAKU・11 BAUHAUS → その派生 13–24 を VOL.3 として追加（13–15 BAUHAUS 系／16–18 KEYCAP 系／19–21 余白系／22–24 かけ合わせ。生成は scratchpad の gen_v3.py）。選ばれたら AppIcon.icns／icon.ico／メニューバー用テンプレ／サイト logo.svg／HUD アクセントへ展開。表記は voicekey（小文字）に統一する案。promo/PROMPT.md は所在不明で本人に確認中。
- 承認が出たら STT 再計測: 鍵ありモデル（gpt-transcribe 追加、gpt-live、groq turbo、nova-3、scribe v2、gemini-3.5）
  を short/long/hard2/hard2_fast_noisy で 3 回ずつ（概算 $0.5 未満）。本人の実声録音があれば同時に測る。
- 新規候補（Soniox stt-rt-v5・MAI-Transcribe-2・Meta Muse・Grok Voice Transcribe 2.0）は鍵の発行が本人待ち。
- Bluetooth 入力（AirPods 等）で押下ごとに「サンプルレート」通知→「再構成します」が出ないか実機確認（本人）。
- MicAutoDetector はまだ AVAudioEngine（ユーザー操作時のみ・低優先）。

## 恒久要件
- 録音部に AVAudioEngine を戻さない。待機中にエンジン／AU を作り直さない。HAL をループで叩かない（再試行は必ず上限付き）。
- 音声入力の経路に待ち時間を足さない。ダブルタップで録音を作り直さない（CLAUDE.md 参照）。
- 課金 API を叩くベンチは件数と概算額を示して承認を取ってから。`say` 合成音では英日混在（hard1）は測れない。
- 両 OS に存在する変更は Mac / Windows 同時実装。README・OVERVIEW・CHANGELOG はコードと同じコミットで更新。
