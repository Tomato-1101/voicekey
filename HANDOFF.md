# HANDOFF — voicekey（2026-10-01 更新）

旧版（ベータ配布計画・履歴同期の実装メモ、537 行）は git 履歴 `c0819b6` の HANDOFF.md を参照。
履歴同期の仕様の正本は `docs/HISTORY_SYNC.md`。

## 恒久要件
- 録音部に AVAudioEngine を戻さない。待機中にエンジン／AU を作り直さない。HAL をループで叩かない（再試行は必ず上限付き）。
- 音声入力の経路に待ち時間を足さない。ダブルタップで録音を作り直さない（CLAUDE.md 参照）。送り先は停止前に外さない。
- テスト目的の課金 API 呼び出し・鍵発行はしない。モデル推奨は鍵の有無で絞らず全モデルから用途別に出す。
- 両 OS に存在する変更は Mac / Windows 同時実装。README・OVERVIEW・CHANGELOG はコードと同じコミットで更新。
- 文字起こしエンジン（スロットの backend）の切替は本人の判断。Claude は変えない（10-01 時点は右⌥・右⌘とも apple_local）。

## STT エンジン入れ替え・API 料金表示（10-02 06:45 更新）
- 目的: 本人決定（10-01）の追加・削除を Mac に実装（Windows は開発停止中）。本人は主に日本語（混在は重視しない）。
- 現状: 3f207c9 で push・dist 入替済み。追加＝Soniox stt-rt-v5（ライブ・新規の既定）／gpt-transcribe／MAI-Transcribe-2／scribe_v2。
  疎通済み: gpt-transcribe 1.7〜1.8s／scribe_v2 0.6〜1.0s。Soniox は 733d7f4 で接続先をキーから自動判定（/v1/models を jp・us 並列、
  200 の方。キー指紋ごとに UserDefaults へ記憶）。
- Soniox JP（10-02）: 本人が JP プロジェクトのキーに差し替え済み（jp=200 / us=401）。承認済み 3 回で離鍵→確定 120〜128ms・誤りゼロ
  （米国窓口の 280〜300ms から短縮）。初回判定 554ms、2 回目以降はキャッシュ命中 0ms。
- 診断ログ（056bb13）: 1 回の入力ごとに `[計測]` 1 行（離鍵→確定→整形→翻訳→貼付の ms・経路・結果）、ほか [ホットキー][録音][デバイス]
  [貼付][整形][HUD][アイコン][ウィンドウ][設定][環境] を ~/Library/Logs/voicekey/ に出す（発話本文・キーは出さない）。
- 不具合 3 件（離鍵取りこぼし・貼付失敗の結果消失・API エラー本文の通知混入）は ae34aa7＋3a5f9b9 で修正済み。
- API 料金表示（8f079cb＋89bb0d3）: ホームに今日/今月/累計・内訳・30 日推移、メニューに今日の API 代。単価表は ApiPricing.swift（出典付き）。
  Groq の llama-3.1-8b-instant / llama-3.3-70b は公式が「Enterprise・Contact Sales」表記で単価未確認（整形・字幕の既定なので常に「未確認を含む」）。
  GroqTranslator のストリーミング usage は実応答で未確認（来なければ記録されないだけ）。
  bf953a4: ドルのみ（円は出さない・本人指示）＋「定価／実際に払った分」切替（有料＝OpenAI・Soniox・Azure、ApiPricing.paidProviders）＋
  10/2 以前の推定取り込み（ログの文字起こし要求行／ログの無い 7/3〜9/17 は stats.json を Groq と仮定）を本番反映済み。backfillVersion=1。
  5907d38: Google（Gemini）も有料側へ（本人「Google Cloud は Google 系モデルにお金がかかっている」）。有料＝OpenAI・Soniox・Azure・Google。
- 課金先の一本化は本人が不要と判断（10-02）。新モデル MAI-Transcribe-2-Streaming（$0.54/時・日本語可・リージョンは北米/欧州/印のみ）は未実装。
- 0341af3: `swift test` が本番ログへ書いていた（gen=- の Soniox 401・HUD 無効行の正体）のを一時ディレクトリへ分離。
  Soniox JP の実使用（10-02, n=70）は離鍵→確定 180〜300ms・STT 76〜194ms。発話が長いほど伸びる（120ms は短文 3 回の値）。
- 次にやること: ActionLog の `[計測]`／「Soniox 再送」の実測で再送待ち上限 15s を詰める。
  Azure（MAI）は本人が後で作る（10-02「作らないのではなく後で」）。作ったらサブスク→southeastasia に Speech→ENDPOINT/KEY 登録→MAI 3 回（承認済み）。

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

## GitHub 配布・自動アップデート（10-02 06:30 更新）
- 現状: v2.2.0（build 23）を本体リポの GitHub Releases で公開（Latest・zip と appcast の 2 点）。DMG は本人指示で廃止。
  入口は `brew install --cask tomato-1101/tap/voicekey`（`~/Project/homebrew-tap`、0ede8c3 で 2.2.0）と `install.sh` だけ（両方 2.2.0 で導入確認済み）。
  サイトの「はじめかた」は brew / curl の 1 行＋コピー＋GitHub 導線（c194d82・本番反映済み）。
- 更新: 6 時間おきに確認→裏 DL→メニュー先頭・ホーム・設定の「vX に更新して再起動」で確認なしに入れ替え、押さなくても終了時。録音中は待つ。
  Sparkle 実 e2e（裏 DL・DL 中に押す・録音中待ち・自前再起動の肩代わり）は scratchpad で通過。2.1.0 の人は旧ピルから 1 回だけ従来手順。
- 次の版: `cd macos && ./scripts/build_release.sh --version X.Y.Z` → Info.plist・CHANGELOG・README 表をコミット → 表示される `gh release create`
  → 表示される version / sha256 で tap を更新 → 常用版は `./scripts/build_app.sh` で作り直す（build_release が dist を配布版で上書きするため）。
- 旧配布リポ voicekey-releases は 10-02 に GitHub・ローカルとも削除済み（保険は `_archive/voicekey-releases-2026-10-02.bundle`）。
  残り: Windows 側の voicekey-releases 参照（src/utils/updater.py・build_windows_dist.ps1・windows-build.yml）は開発停止中のため未修正。

## ロゴ・配色の刷新「17 LEGEND」（10-01 19:15 更新）
- 目的: 確定ロゴ 17 LEGEND（ライト=Bone／ダーク=Carbon を外観で自動切替）と配色（灯り #FF5A1F）を全面採用。原本・生成手順は design/brand/。
- 現状: Mac は a6980d9 で push 済み（アイコン・メニューバー・HUD・字幕ガラス・設定 UI）。Windows は停止中のため icon.ico だけ差替。
  GitHub: 本リポ PUBLIC・About/README/プロフィールから https://voicekey.vercel.app へ導線・ソーシャルプレビュー（本リポ＋releases）設定済み。
  サイトは 81937ed を本番反映済み（voicekey.vercel.app）。**voicekey.app は他社ドメイン**（一度誤ってリンクし、10-01 に全部差し戻した）。
  Vercel の環境変数は Production のみ＝preview は 500 になる。確認は `vercel deploy --prod --skip-domain` → 確認 → `vercel promote`。
- 次にやること: 任意で DMG 副題色を #7F796F に。promo/PROMPT.md は所在不明（本人に確認）。
