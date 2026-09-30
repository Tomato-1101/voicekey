# HANDOFF — voicekey（2026-10-01 更新）

旧版（ベータ配布計画・履歴同期の実装メモ、537 行）は git 履歴 `c0819b6` の HANDOFF.md を参照。
履歴同期の仕様の正本は `docs/HISTORY_SYNC.md`。

## 目的
(1) Mac 版の重さ・ハングを止める（完了）。(2) 録音末尾 2〜3 文字の欠けを直す（実装・入替済み・コミットはレビュー待ち）。
(3) STT モデル選定（公開情報で調査済み・本人の判断待ち）。(4) ロゴ・配色の刷新（24 案提示・本人の選択待ち）。

## 現状
- 録音部は入力専用 AUHAL 1 個の使い回し（7498144）。6 日稼働で footprint 約 120MB・異常 0。coreaudiod は 10-01 時点 60MB・CPU 2%。
- 末尾欠け: stop 前に送り先を外していたため、停止時フラッシュの末尾音声がストリーマーへ届かず確定していた（Mac/Windows とも）。
  送り先を pending/active の 2 段（`ChunkRouting`）にし、stop のフラッシュ後に active だけ外す形へ修正（未コミット）。
  swift test 252 件・Python 548 件 OK。dist/voicekey.app を 10-01 04:48 に入れ替え済み。実機確認は ActionLog の
  「停止時フラッシュ … ストリーム送信=あり／末尾150ms …dBFS」（末尾が大きい＝発話中に離鍵）。
- 常用は slot 1 = apple_local（10-01 の欠け事例は全部これ）。バックエンド切替は本人の判断（Claude は変えない）。
- STT 調査（10-01・公開情報＋Grok 経由の X）: 速さ=Soniox v5 RT（確定 0.05s・ja 5.8）／日本語精度=Scribe v2 バッチ（鍵あり・実装済み）
  と MAI-Transcribe／多言語混在=Soniox v5・Gemini 3.5 Transcribe・AssemblyAI U3.6（ja/zh/en 混在の公開実測は存在しない）。
  openai_live は言語未指定でも "ja" 固定で送るので、多言語枠に使うなら改修が要る。

## 次にやること
- 本人が `/codex:adversarial-review`（Claude からは起動不可）→ 指摘を裏取り → コミット＆push（CHANGELOG は反映済み）。
- ロゴ: 案はキャンバス https://claude.ai/artifact/SJtFByb9SXyDdwEt6brp2j（01–04 は独自案、05–12 は巨匠オマージュ: Rams/Vignelli/Wyman/原研哉/Kare/Rand/Bayer/Aicher。推奨は 01 CARET、次点 08 YOHAKU・05 GRILLE）。本人の好みは 02 KEYCAP・08 YOHAKU・11 BAUHAUS → その派生 13–24 を VOL.3 として追加（13–15 BAUHAUS 系／16–18 KEYCAP 系／19–21 余白系／22–24 かけ合わせ。生成は scratchpad の gen_v3.py）。本人評価は 17 LEGEND が 1 位（15・16 も好評）→ 仕上げ版を「17 LEGEND — FINAL」「17 BEFORE → AFTER」ボードに追加（刻印 Geist・余白 92 で統一・LED にレンズ・ライト=Bone／ダーク=16 の Carbon・メニューバーは待機=輪／録音=橙。生成は scratchpad の gen_v4.py）。決定したら AppIcon.icns／icon.ico／メニューバー用テンプレ／サイト logo.svg／HUD アクセントへ展開。表記は voicekey（小文字）に統一する案。promo/PROMPT.md は所在不明で本人に確認中。
- 本人が新モデルを使うと決めたら: 鍵は本人が発行 → 中央 Keychain → バックエンド追加。テスト目的の課金はしない。
- 範囲外の既知問題: `LocalSpeechTranscriber.attach` が unlock 後に退避チャンクを feed（順序入替の可能性）／
  abortStalledRecordStart 後にキューが復帰すると誰も stop しない録音が残りうる。
- Bluetooth 入力（AirPods 等）で押下ごとに「再構成します」が出ないか実機確認（本人）。MicAutoDetector はまだ AVAudioEngine（低優先）。

## 恒久要件
- 録音部に AVAudioEngine を戻さない。待機中にエンジン／AU を作り直さない。HAL をループで叩かない（再試行は必ず上限付き）。
- 音声入力の経路に待ち時間を足さない。ダブルタップで録音を作り直さない（CLAUDE.md 参照）。送り先は停止前に外さない。
- テスト目的の課金 API 呼び出し・鍵発行はしない。モデル推奨は鍵の有無で絞らず全モデルから用途別に出す。
- 両 OS に存在する変更は Mac / Windows 同時実装。README・OVERVIEW・CHANGELOG はコードと同じコミットで更新。
