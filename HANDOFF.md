# HANDOFF — voicekey（2026-10-01 更新）

旧版（ベータ配布計画・履歴同期の実装メモ、537 行）は git 履歴 `c0819b6` の HANDOFF.md を参照。
履歴同期の仕様の正本は `docs/HISTORY_SYNC.md`。

## 恒久要件
- 録音部に AVAudioEngine を戻さない。待機中にエンジン／AU を作り直さない。HAL をループで叩かない（再試行は必ず上限付き）。
- 音声入力の経路に待ち時間を足さない。ダブルタップで録音を作り直さない（CLAUDE.md 参照）。送り先は停止前に外さない。
- テスト目的の課金 API 呼び出し・鍵発行はしない。モデル推奨は鍵の有無で絞らず全モデルから用途別に出す。
- 両 OS に存在する変更は Mac / Windows 同時実装。README・OVERVIEW・CHANGELOG はコードと同じコミットで更新。
- 文字起こしエンジン（スロットの backend）の切替は本人の判断。Claude は変えない（10-01 時点は右⌥・右⌘とも apple_local）。

## STT エンジン入れ替え（10-02 01:50 更新）
- 目的: 本人決定（10-01）の追加・削除を Mac に実装（Windows は開発停止中）。本人は主に日本語（混在は重視しない）。
- 現状: 3f207c9 で push・dist 入替済み。追加＝Soniox stt-rt-v5（ライブ・新規の既定）／gpt-transcribe／MAI-Transcribe-2／scribe_v2。
  疎通済み: Soniox（米国窓口）離鍵→確定 0.28〜0.30s 誤りゼロ／gpt-transcribe 1.7〜1.8s／scribe_v2 0.6〜1.0s。
- Soniox JP リージョンは 10-01 にサポートが有効化（返信メール確認済み）。733d7f4 で接続先をキーから自動判定（無料の /v1/models を
  jp・us 並列で叩き 200 の方。キーの指紋ごとに UserDefaults へ記憶）。いまの米国キーは jp=401 / us=200 で us 判定になることを確認。
- 次にやること: 本人が Soniox コンソールで region=JP のプロジェクトとキーを作り `SONIOX_API_KEY` を差し替え →
  無料の /v1/models で jp=200 を確認 → JP 窓口で疎通（要承認・数回）→ ActionLog「Soniox 再送」の実測で再送待ち上限 15s を詰める。
  Azure（MAI）は本人がまだ作らない方針（10-02）。作る時はサブスク作成→southeastasia に Speech→ENDPOINT/KEY 登録→MAI 3 回（承認済み）。

## 録音部の既知問題（10-01 15:10 更新）
- 目的: 重さ・ハング（7498144/91ded40）と末尾欠け（1ea7e59）は完了済み。残りの既知問題だけを置く。
- 範囲外: `LocalSpeechTranscriber.attach` が unlock 後に退避チャンクを feed（順序入替の可能性）／
  abortStalledRecordStart 後にキューが復帰すると誰も stop しない録音が残りうる。
- Bluetooth 入力（AirPods 等）で押下ごとに「再構成します」が出ないか実機確認（本人）。MicAutoDetector はまだ AVAudioEngine（低優先）。

## 設定画面の作り直し・画面記憶（10-01 21:00 更新）
- 目的: 本人指示「最小化して戻したら同じ画面のまま」「設定が押しにくいのでデザインし直して」。
- 現状: 68d4673 で push。サイドバーは見出し付きの常時一覧、grouped Form・行全体トグル・大きめの操作部品、最後の画面と窓の位置を復元。
  見た目確認は `dist/voicekey.app/Contents/MacOS/voicekey --ui-snapshot <dir> --appearance light|dark`（`[VERDICT] status=ok files=18`）。
- 次にやること: 本人の使用感待ち。参考収集（Superwhisper・MacWhisper・Wispr Flow 等）から残る改善候補は、ホットキーのチップ化・
  エンジン＋モデルの検索付き 1 リスト化・辞書の常時入力欄。ライトのスナップショットで一部 SF Symbol が薄いのは作り直し前からある画面外描画の癖。

## ロゴ・配色の刷新「17 LEGEND」（10-01 19:15 更新）
- 目的: 確定ロゴ 17 LEGEND（ライト=Bone／ダーク=Carbon を外観で自動切替）と配色（灯り #FF5A1F）を全面採用。原本・生成手順は design/brand/。
- 現状: Mac は a6980d9 で push 済み（アイコン・メニューバー・HUD・字幕ガラス・設定 UI）。Windows は停止中のため icon.ico だけ差替。
  GitHub: 本リポ PUBLIC・About/README/プロフィールから https://voicekey.vercel.app へ導線・ソーシャルプレビュー（本リポ＋releases）設定済み。
  サイトは 81937ed を本番反映済み（voicekey.vercel.app）。**voicekey.app は他社ドメイン**（一度誤ってリンクし、10-01 に全部差し戻した）。
  Vercel の環境変数は Production のみ＝preview は 500 になる。確認は `vercel deploy --prod --skip-domain` → 確認 → `vercel promote`。
- 次にやること: 任意で DMG 副題色を #7F796F に。promo/PROMPT.md は所在不明（本人に確認）。
