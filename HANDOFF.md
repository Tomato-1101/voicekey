# HANDOFF — voicekey（2026-10-01 更新）

旧版（ベータ配布計画・履歴同期の実装メモ、537 行）は git 履歴 `c0819b6` の HANDOFF.md を参照。
履歴同期の仕様の正本は `docs/HISTORY_SYNC.md`。

## 恒久要件
- 録音部に AVAudioEngine を戻さない。待機中にエンジン／AU を作り直さない。HAL をループで叩かない（再試行は必ず上限付き）。
- 音声入力の経路に待ち時間を足さない。ダブルタップで録音を作り直さない（CLAUDE.md 参照）。送り先は停止前に外さない。
- テスト目的の課金 API 呼び出し・鍵発行はしない。モデル推奨は鍵の有無で絞らず全モデルから用途別に出す。
- 両 OS に存在する変更は Mac / Windows 同時実装。README・OVERVIEW・CHANGELOG はコードと同じコミットで更新。
- 文字起こしエンジン（スロットの backend）の切替は本人の判断。Claude は変えない（10-01 時点は右⌥・右⌘とも apple_local）。

## STT エンジン入れ替え（10-01 15:10 更新）
- 目的: 本人決定（10-01）の追加・削除を Mac に実装（Windows は開発停止中）。本人は主に日本語（混在は重視しない）。
- 現状: 3f207c9 で push・dist 入替済み。追加＝Soniox stt-rt-v5（ライブ・新規の既定）／gpt-transcribe／MAI-Transcribe-2／scribe_v2。
  削除＝Deepgram・nova-2・gpt-realtime-whisper・whisper-large-v3・gpt-4o 系・scribe_v1_experimental（廃止モデルは V19 の一回限り移行）。
  疎通済み: gpt-transcribe 1.7〜1.8s 誤りゼロ／scribe_v2 0.6〜1.0s（漢数字は数字入力の正規化で 3時）。Codex は上限のため Opus 2 体で代行レビュー→12 件修正。
- 次にやること: 本人が SONIOX_API_KEY・AZURE_SPEECH_KEY・AZURE_SPEECH_ENDPOINT を中央 Keychain へ登録（Azure は対応リージョン・東南アジアが最寄り）→
  Soniox・MAI を各 3 回疎通（承認済みの範囲）→ ActionLog「Soniox 再送」の実測で再送の待ち上限 15s を詰める。
  見送った指摘: 終端送信を前の発話の処理待ちより先に出す（待ちの直列化）／鍵未設定のネガティブキャッシュ。新モデル監視は本人が ChatGPT で回す。

## 録音部の既知問題（10-01 15:10 更新）
- 目的: 重さ・ハング（7498144/91ded40）と末尾欠け（1ea7e59）は完了済み。残りの既知問題だけを置く。
- 範囲外: `LocalSpeechTranscriber.attach` が unlock 後に退避チャンクを feed（順序入替の可能性）／
  abortStalledRecordStart 後にキューが復帰すると誰も stop しない録音が残りうる。
- Bluetooth 入力（AirPods 等）で押下ごとに「再構成します」が出ないか実機確認（本人）。MicAutoDetector はまだ AVAudioEngine（低優先）。

## 旧形式の引き継ぎ
- 目的: ロゴ・配色の刷新（17 LEGEND が最有力・仕上げ版の確認待ち）。
- ロゴ: 案はキャンバス https://claude.ai/artifact/SJtFByb9SXyDdwEt6brp2j（01–04 は独自案、05–12 は巨匠オマージュ: Rams/Vignelli/Wyman/原研哉/Kare/Rand/Bayer/Aicher。推奨は 01 CARET、次点 08 YOHAKU・05 GRILLE）。本人の好みは 02 KEYCAP・08 YOHAKU・11 BAUHAUS → その派生 13–24 を VOL.3 として追加（13–15 BAUHAUS 系／16–18 KEYCAP 系／19–21 余白系／22–24 かけ合わせ。生成は scratchpad の gen_v3.py）。本人評価は 17 LEGEND が 1 位（15・16 も好評）→ 仕上げ版を「17 LEGEND — FINAL」「17 BEFORE → AFTER」ボードに追加（刻印 Geist・余白 92 で統一・LED にレンズ・ライト=Bone／ダーク=16 の Carbon・メニューバーは待機=輪／録音=橙。生成は scratchpad の gen_v4.py）。決定したら AppIcon.icns／icon.ico／メニューバー用テンプレ／サイト logo.svg／HUD アクセントへ展開。表記は voicekey（小文字）に統一する案。promo/PROMPT.md は所在不明で本人に確認中。
