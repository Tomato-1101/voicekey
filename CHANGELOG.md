# Changelog

voicekeyの変更履歴を記録するファイルです。

## [Unreleased] - 2026-06-10 (voicekey for Mac)

### Added
- **macOS ネイティブ版 `macos/` を新規作成（Swift / SwiftUI）**
  - 方針転換: 「voicekey for Mac（Swift ネイティブ）」と「voicekey for Windows（既存 Python 版がベース）」をそれぞれ最適な技術で開発する。本リポジトリはモノレポ構成とする
  - メニューバー常駐（`MenuBarExtra`、Dock 非表示）。アイコン色で状態表示（待機=テンプレート / 録音=赤 / 自動送信録音=紫 / 変換中=オレンジ）
  - **HotkeyMonitor**: CGEventTap（listen-only）によるグローバルキー監視。修飾キーの左右をデバイス依存ビットで厳密判定し、`tapDisabledByTimeout` 受信時は即時再有効化（pynput で起きていた「ホットキー永久無反応」を OS レベルで根治）
  - **AudioRecorder**: AVAudioEngine 録音 + 16kHz モノラル変換。エンジン操作は専用シリアルキューで直列化
  - **VoiceActivity**: 音量正規化（ゲイン上限 +20dB）+ Apple SoundAnalysis のオンデバイス ML 分類器による発話判定（フォールバックはエネルギーベース）+ 無音トリミング
  - **Transcriber**: OpenAI / Groq REST（WAV multipart、URLSession）。録音開始時の TLS プリウォーム付き
  - **Keychain**: API キー保存。サービス名は Python 版（keyring）と互換で、保存済みキーをそのまま読める
  - **Paster**: クリップボード貼り付け + 元内容の自動復元 + ダブルタップ自動 Enter
  - **HUD**: 録音中のみ画面下部中央に音声レベル連動の波形ピル（NSPanel、クリック透過、フルスクリーン上にも表示）。変換中スピナー / エラー・無音通知（2 秒）
  - **設定ウィンドウ**: 一般（言語・VAD・HUD・自動 Enter 遅延・ログイン時起動）/ ホットキー 1・2（キャプチャ式レコーダー・モード・バックエンド・モデル・プロンプト）/ API キー
  - **AppController**: 文字起こしパイプラインの直列チェーン化（録音順の挿入保証）、録音 300 秒上限の保険、起動時の権限チェック（マイク・入力監視・アクセシビリティ）とシステム設定への誘導
  - ビルド: SwiftPM + `scripts/build_app.sh`（.app バンドル組み立て + ad-hoc 署名）。`swift build` 警告ゼロ、起動スモークテスト済み

### Added (2026-06-10 追記)
- **ElevenLabs / Deepgram バックエンドを追加（Mac 版）**
  - ElevenLabs Scribe（`scribe_v1` / `scribe_v1_experimental`）: multipart + `xi-api-key` 認証。音声イベントタグ（笑い声など）は音声入力に不要なため無効化
  - Deepgram（`nova-2` 既定 / `nova-3`）: WAV 生バイト POST + `Token` 認証。`smart_format` 有効。言語未指定時は自動判定、nova-3 + 非英語は多言語モードに自動切替
  - `Transcriber` をバックエンド別のリクエスト構築/応答解析に再構成（`MultipartForm` ヘルパー新設）。Keychain サービス名は Python 版と互換（`voicekey.ElevenLabs` / `voicekey.Deepgram`）。設定 UI の API キータブ・バックエンド/モデル選択は全 4 バックエンド対応
- **ログイン時自動起動（Mac 版）**
  - 初回起動時に `SMAppService.mainApp.register()` を自動実行（Mac を開けば voicekey が必ず起動する）。設定画面の「ログイン時に起動」トグルでオフにすれば再登録しない
- **文字起こしベンチマーク基盤 `benchmark/` を新規追加**
  - 同一の日本語音声（短文 6.8s / 長文 41.7s、`say` で合成）を 4 バックエンドの各モデルに送り、レイテンシと CER（文字誤り率）を測定する `run_benchmark.py`
  - API キーは環境変数 / `.env` → Keychain（アプリと共用）の順で取得（中身は非表示）。`make_audio.sh` で音声生成、原稿 `.txt` が CER 採点の正解を兼ねる
  - 初回計測（OpenAI / Groq / ElevenLabs、Deepgram はキー待ち）: ElevenLabs scribe_v1 が最高精度（CER 0.0%/0.4%）だが長文 3.5s と低速、Groq whisper-large-v3-turbo が最速（385ms/742ms）で精度も良好（2.7%/5.4%）、OpenAI gpt-4o-transcribe は中速・長文 CER 7.6%

### Added (2026-06-10 追記 3)
- **各社の実在モデルを API から取得する `benchmark/list_models.py` を追加**
  - 推測でなく `/models` エンドポイントから最新モデルを確認。OpenAI は `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` / `gpt-4o-transcribe-diarize` / `gpt-realtime-whisper` / `whisper-1`、Deepgram は `nova-2` / `nova-3` を確認。ElevenLabs STT は `scribe_v1` / `scribe_v1_experimental` のみ（`/v1/models` は TTS 用で STT v2 は存在しない）
- **バッチ比較を全 4 社・全モデルに拡充（`run_benchmark.py`）。Deepgram 込みの確定計測**
  - レイテンシ（最速値）/ CER（短文・長文）:
    - Groq turbo: 330ms / 695ms、CER 2.7% / 5.4%（REST 最速）
    - Deepgram nova-3: 1084ms / 1475ms、CER 0.0% / 4.0%（長文でもレイテンシが伸びない・精度安定）
    - ElevenLabs scribe_v1: 1106ms / 3682ms、CER 0.0% / 0.4%（短文・長文とも最高精度だが長文は低速）
    - OpenAI gpt-4o-mini-transcribe: 861ms / 1937ms、CER 2.7% / 8.0%
  - 注意点: Deepgram は日本語出力に単語間スペースを挿入する（CER は空白除去後のため低いが、貼り付け時はスペース除去が必要）。`gpt-4o-transcribe-diarize` は長文で `chunking_strategy` 必須エラー（話者分離モデルはディクテーション用途では不適）
- **リアルタイムストリーミング速度測定 `benchmark/stream_benchmark.py` を追加（WebSocket）**
  - 同一音声を 1 倍速で WS 送信し、TTFB（喋り出して最初の字が出るまで）と確定レイテンシ（送信完了＝ホットキーを離した相当から最終確定まで）を測定
  - OpenAI Realtime は GA 仕様に対応（`session.update` で `session.type="transcription"`、`OpenAI-Beta` ヘッダ廃止）。**入力 PCM は 24kHz 以上必須**のため 16kHz テスト音声を `audioop.ratecv` で 24kHz にアップサンプルして送信
  - 結果（TTFB / 確定 / CER、短文・長文）:
    - Deepgram nova-3: 1049ms / **61ms** / 0.0%（短）、1089ms / **125ms** / 1.3%（長）。真のストリーミング（発話中に逐次確定）で離した瞬間ほぼ確定。最良
    - OpenAI gpt-realtime-whisper: 1051ms / 951ms / 0.0%（短）、1192ms / 668ms / 2.7%（長）。真のストリーミングだが確定は Deepgram より遅い
    - OpenAI gpt-4o-transcribe / mini: TTFB が音声長と同じ（発話中は字が出ず commit 後に一括）。確定は離してから 0.4〜2.8s。ライブ字幕は不可
  - デバッグ用 `benchmark/debug_openai_rt.py`（OpenAI Realtime の生イベント確認）も追加
  - 結論: HUD にライブ表示するなら Deepgram nova-3 が最適（確定 60〜170ms・精度安定・多言語）。OpenAI で揃えるなら gpt-realtime-whisper。ホールド入力で離してから一括でよいなら REST の Groq turbo が最速

### Added (2026-06-10 追記 4)
- **アプリ本体にリアルタイムストリーミング + ライブ字幕を実装（Mac 版）**
  - 新規 `StreamingTranscriber.swift`: Deepgram WebSocket（`URLSessionWebSocketTask`、外部依存なし）。録音中の 16kHz PCM を逐次送信し、暫定（interim）/確定（is_final）を受信
  - `AudioRecorder`: 逐次チャンク通知 `chunkHandler` を追加。全バッファ蓄積とは独立して送るため、ストリーミングが失敗しても同じ録音バッファで REST にフォールバックできる
  - `AppController`: backend=Deepgram かつストリーミング有効のとき WS 経路。ホットキーを離した瞬間に確定テキストを貼り付け、空・失敗時は REST フォールバック。録音破棄時は `cancel()`
  - HUD: 録音中に**ライブ字幕**を表示（発話しながら文字が伸び、最新の語尾が見えるよう頭を省略表示）。HUD 幅を 460pt に拡張（ピル自体は内容に追従）
  - 日本語スペース除去: Deepgram は日本語に単語間スペースを挿入するため、「前後どちらかが CJK 文字」のスペースのみ除去（英単語間スペースは維持）
  - 設定の一般タブに「リアルタイムストリーミング（Deepgram）」トグルを追加（既定 ON）
  - 既定モデル更新: Deepgram を **nova-3** に（ストリーミング最良）、ElevenLabs に **scribe_v2** を追加
- **ベンチに最新モデルを追加（検索＋API 実測で見落としを洗い出し）**
  - ElevenLabs **Scribe v2**（バッチ）: 短文 CER 0.0%（1255ms）だが**長文は 5.8% と scribe_v1 の 0.4% より精度後退**。利用可能 STT は scribe_v1 / scribe_v1_experimental / scribe_v2 の 3 つ（`probe_stt_models.py` で確定）
  - Deepgram **Flux**（`flux-general-multi`、会話向け新モデル、`/v2/listen`）: TTFB 885〜1107ms、確定 407〜546ms、CER 0.0%/2.7%。ターン検出ぶん確定が遅くホールド入力には nova-3 が上
  - ElevenLabs **Scribe v2 Realtime**（`/v1/speech-to-text/realtime` WebSocket、`scribe_v2_realtime`）: 短文 確定 266ms/CER 0.0% だが TTFB ~2.2s と高め、長文で確定取得に失敗（要調整）。公称 150ms は本計測では未再現
  - `debug_realtime_new.py` で Flux / ElevenLabs Realtime の生プロトコル（TurnInfo/event、partial/committed_transcript）を確定してから実装
  - **ストリーミング最終結論**: 離して→確定が最速・最精度なのは **Deepgram nova-3（確定 94〜109ms、CER 0.0%/1.3%）**。ライブ字幕の既定として最適。OpenAI 統一なら gpt-realtime-whisper（確定 ~800ms）。Flux は会話エージェント向け、ElevenLabs Realtime は現状 nova-3 に及ばず
  - 新規キーが要る有力候補（未計測・要サインアップ）: Soniox（日本語 WER 8.7% 主張・$0.12/h）、Speechmatics（月 40h 無料）、Mistral Voxtral Realtime（$0.006/min）

### Added (2026-06-10 追記 5) — Windows 版を Mac 版と同等まで刷新
- **ElevenLabs / Deepgram バックエンドを追加（Windows/Python 版、全 4 バックエンド対応）**
  - `TranscriptionBackend` 列挙型に `ELEVENLABS` / `DEEPGRAM` を追加（従来は groq/openai のみ）
  - `ElevenLabsTranscriber`（REST、`scribe_v1` 既定）: `xi-api-key` 認証 + multipart（`model_id` / `language_code`）。応答 JSON の `text` を採用。実 API で短文 CER 0.0% を確認
  - `DeepgramTranscriber`（REST、`nova-3` 既定）: `Token` 認証 + WAV 生バイト POST、`punctuate` / `smart_format` 有効。nova-3 は日本語単言語非対応のため `language=multi` に自動切替。日本語の単語間スペースを除去。実 API で短文 CER 0.0% を確認
  - `ApiTranscriber` 基底に `_auth_headers()` / `_raise_for_status()` / `_post()` を抽出し、4 バックエンドで共通化（重複削減）。Keychain サービス名は Mac 版と互換（`voicekey.Deepgram` 追加）
- **Deepgram リアルタイムストリーミング + HUD ライブ字幕を実装（Mac 版と同挙動）**
  - 新規 `src/core/streaming_transcriber.py`: Deepgram WebSocket（`websockets` 同期クライアント、受信は専用スレッド。アプリのスレッドベース設計に合わせ asyncio 不使用）。Mac 版 `StreamingTranscriber.swift` の移植
  - `AudioRecorder` に `chunk_callback` を追加。REST 用バッファ（`audio_q`）とは独立に録音中の生 PCM を逐次送出。ストリーミング失敗時は同じ録音バッファで REST に自動フォールバック
  - `app.py`: backend=Deepgram かつストリーミング有効のとき WS 経路。離した瞬間に確定テキストを貼り付け、空/失敗時は REST フォールバック。設定ホットリロード・ハング復旧・終了時に接続を確実に破棄
  - 接続確立前に `finish()` が呼ばれた短い発話向けに接続猶予（最大 1 秒）を実装し、取りこぼしを防止
  - 実 API 結合テスト: 短文はリアルタイム送信で完全一致（離して→確定 1.4s）、長文も全文取得を確認
- **録音中 HUD を新規実装（`src/ui/hud.py`、UI 刷新）**
  - 画面下部中央の小型ピル。録音中は音声レベル連動の波形バー、ストリーミング時はライブ字幕（頭省略で最新の語尾を表示）、変換中は「変換中…」、通知は 2 秒表示
  - **フォーカスを絶対に奪わない**設計（`WindowDoesNotAcceptFocus` + `WA_ShowWithoutActivating` + 入力透過）。貼り付け先ウィンドウのフォーカスを維持。タスクバー非登録
  - これまで表示先のなかった `notice` シグナル（エラー・無音検出）も HUD で可視化
- **設定 UI を全 4 バックエンド対応に拡張（`settings_window.py`）**
  - バックエンド選択に elevenlabs / deepgram を追加。モデル一覧をバックエンド別マップ（`_BACKEND_MODELS`）で差し替え、保存済みモデルが非対応なら既定（先頭）へ
  - Advanced タブに「リアルタイムストリーミング（Deepgram）」「録音中の HUD を表示」トグルを追加（保存・ロード対応）
- **既定値 / 依存 / パッケージング**
  - `constants.py`: `streaming_enabled` / `hud_enabled`（既定 ON）、`default_api_models` を全 4 バックエンド（deepgram=nova-3 / elevenlabs=scribe_v1）に拡張。`config_manager.py` の `API_BACKENDS` を 4 種へ
  - `requirements.txt` に `websockets>=13.0`、`voicekey.spec` に `collect_submodules('websockets')`（遅延 import のため明示収集）
- **ユニットテスト基盤を新規追加（`tests/`、これまで皆無）**
  - text_utils（CJK スペース除去）/ streaming_transcriber（メッセージ解析・PCM 変換・接続前 finish）/ api_transcriber（認証ヘッダー・言語解決・ステータス検査）/ config_manager（マイグレーション・正規化・既定）/ audio_utils（WAV ヘッダ・クリップ）の 38 テスト。ネットワーク/実機非依存で全パス

### Technical Details (追記 5)
- **types.py**: `TranscriptionBackend` に ELEVENLABS/DEEPGRAM、`TranscriptionTask` に `streamer` フィールド
- **app.py**: `_BACKEND_CLASSES` マップ、`interim_text` シグナル、`_active_streamer` 状態、`_insert_and_enter()` ヘルパー、ストリーミング優先＋REST フォールバックの `_process_task`
- **検証**: 全変更ファイル `py_compile` OK、オフスクリーン結合スモーク OK、実 Deepgram/ElevenLabs API で REST・ストリーミングとも一致、ユニットテスト 38 件パス。Windows GUI/ホットキー/トレイ/マイクの実機確認はユーザー側で実施（開発は macOS のため）

### Added (2026-06-11 追記 6) — 入力デバイス選択（Mac）とログイン時起動（Windows）
両プラットフォームに「相手側が既に持っていた機能」を追加し、parity を揃えた。

- **Mac 版に入力デバイス選択を追加**（Windows 版は既存）
  - Core Audio (HAL) で入力チャンネルを持つマイクを列挙する `AudioDevices`（新規）を追加
  - 設定の「一般」タブに入力デバイスのピッカーと更新ボタンを追加（「システム既定」+ 接続中のマイク。未接続の保存済みデバイスも選択を保持して表示）
  - 選択は安定した UID で永続化し（`AudioDeviceID` は再接続で変わるため）、録音開始のたびに `AVAudioEngine` の inputNode へ `setDeviceID` で適用
- **Windows 版にログイン時自動起動を追加**（Mac 版は既存 = SMAppService）
  - レジストリ Run キー（`HKCU\...\CurrentVersion\Run`）で管理する `autostart`（新規）を追加。`sys.platform` ガードで非 Windows では安全に no-op
  - 設定の「Advanced」に「ログイン時に起動」チェックボックスを追加（非 Windows では無効化＋注記）。状態はレジストリが真実なので settings.yaml には保存しない

### Technical Details (追記 6)
- **macos/Core/AudioDevices.swift**（新規）: `inputDevices()` / `deviceID(forUID:)`。`kAudioHardwarePropertyDevices` 列挙 + `kAudioDevicePropertyStreamConfiguration`（入力スコープ）でチャンネル判定
- **macos/Core/AudioRecorder.swift**: `inputDeviceUID` を追加し `start()` 内で `input.auAudioUnit.setDeviceID()` を実行
- **macos/Config/AppConfig.swift**: `@Published inputDeviceUID` を UserDefaults に永続化
- **macos/UI/SettingsView.swift** / **AppController.swift**: ピッカー追加、録音開始前に `recorder.inputDeviceUID = config.inputDeviceUID`
- **src/utils/autostart.py**（新規）: `is_supported()` / `is_enabled()` / `set_enabled()` / `_launch_command()`（凍結 exe か pythonw+run.py を引用符付きで組み立て）
- **src/ui/settings_window.py**: `_autostart_check` 追加（load=レジストリ実状態、save=`set_enabled`）
- **検証**: Mac `swift build` 成功、Windows `py_compile` OK・ユニットテスト 41 件パス（autostart 3 件追加）・オフスクリーンで設定ウィンドウ構築と非対応時の無効化を確認。Windows 実機でのレジストリ登録・自動起動はユーザー側で確認（開発は macOS のため）

### Added (2026-06-11 追記 7) — LLM テキスト自動整形（Mac / Windows）
文字起こし確定テキストを貼り付け直前に Groq の高速 LLM（既定 `llama-3.1-8b-instant`、Chat Completions）で 1 回整形する機能。主流ディクテーションアプリ（Wispr Flow / Superwhisper 等）と同等の後処理。

- **ホットキーごとにオン/オフ**（既定オフ）。片方は raw 高速・もう片方は整形済み、という使い分けができる
- **整形モード 6 種**（識別子・プロンプト文言は Mac / Windows で完全一致）: 自動クリーン（フィラー除去・句読点整形）/ 箇条書き / 丁寧（敬語）/ カジュアル / メール調 / カスタム（自由プロンプト。空なら自動クリーンにフォールバック）
- **発話を絶対に失わない**: 空入力は API 非呼出、キー未設定・タイムアウト（10 秒上限）・HTTP 非 200・応答不正・空応答・あらゆる例外は警告ログ + 原文をそのまま貼り付け。整形失敗で例外が貼り付け経路へ漏れることはない
- 整形モデルは設定（一般 / Advanced）で変更可能。ストリーミング確定・REST の両経路に適用

### Added (2026-06-11 追記 8) — 整形モード「おまかせ（自動判断）」と UI 改善
- **「おまかせ（自動判断）」モードを追加し既定に**（Mac / Windows、識別子 `auto`）
  - ユーザーが毎回モードを選ばなくても、LLM がテキストの内容から整形方法を自動判断する（フィラー除去は常時。列挙・手順なら箇条書き、それ以外は自然な文章。文体は元の発言を維持）
  - 自動判断の指示（システムプロンプト）は設定で自由に編集可能（Mac: 一般タブ「おまかせ整形の指示」+ 既定に戻すボタン / Windows: Advanced「Auto Format Prompt」）。空欄なら既定の指示を使用
- **整形モデルを自由入力からリスト選択に変更**（Mac / Windows 共通リスト）: llama-3.1-8b-instant（既定・最速）/ llama-3.3-70b-versatile / openai/gpt-oss-20b / openai/gpt-oss-120b / moonshotai/kimi-k2-instruct。保存済みのリスト外モデルも選択を保持
- Technical: Mac `FormatMode.auto` + `defaultAutoPromptBody` + `TextFormatter.knownModels`、`ConfigStore.autoFormatPrompt`（UserDefaults 永続化）。Windows `DEFAULT_AUTO_PROMPT` / `KNOWN_FORMAT_MODELS`、`format_auto_prompt` 設定（空 = 既定で保存し既定文の将来更新に追従）。`build_system_prompt` / `format_text` に `auto_prompt` 引数追加。テスト 5 件追加（全 58 件パス）。Mac は実機でモード選択 UI・一般タブの編集欄・既存スロット設定の温存を確認

### Added (2026-06-11 追記 9) — モデル選択リストに「（推奨）」表記
- **音声（文字起こし）・文字（整形）両方のモデル選択リストで、推奨モデルの表示名に「（推奨）」を付けた**（Mac / Windows）
  - 表示ラベルだけの変更で、保存値・API へ送る値はモデル識別子のまま（Mac: Picker tag / Windows: QComboBox userData）
  - 推奨 = ベンチ実測 2026-06-10 に基づく各バックエンドの既定: OpenAI `gpt-4o-mini-transcribe` / Groq `whisper-large-v3-turbo` / ElevenLabs `scribe_v1`（日本語最高精度。v2 は長文後退）/ Deepgram `nova-3` / 整形 `llama-3.1-8b-instant`（速度テスト実行待ちの暫定）
  - Mac の knownModels の並びを「先頭＝既定＝推奨」に統一（OpenAI を mini 先頭、ElevenLabs を scribe_v1 先頭へ。Windows と同順序に）。保存済みの選択はそのまま温存される
- **整形モデル速度ベンチ `benchmark/format_speed_bench.py` を追加**: 全 5 整形モデルに同一リクエスト（おまかせプロンプト + フィラー多め日本語 110 文字 × 3 回）を送りレイテンシのみ計測。キーは既存ベンチと同じ手順で取得し表示しない

### Fixed (2026-06-11 追記 7)
- **API キー使用のたびに Keychain の承認ダイアログが出る問題（Mac 版）**
  - 原因: Python 版 keyring や旧 ad-hoc 署名ビルドが作成した Keychain 項目は ACL 上の所有者が「別アプリ」のため、現在の署名アプリの読み取りで毎回承認を求められていた
  - 修正 1: 保存処理を `SecItemUpdate` から **`SecItemDelete` → `SecItemAdd`** に変更し、項目を常に現アプリが新規作成して所有権を取る
  - 修正 2: 読み取り成功直後に同じ値で書き直す**自己修復移行**を追加（環境変数フォールバック経路では行わない）。既存ユーザーは各キーにつき**次回の読み取りで 1 回だけ承認すれば以後ダイアログが出なくなる**（キーの再入力は不要）

### Technical Details (追記 7)
- **macos/Core/TextFormatter.swift**（新規）: `FormatMode` enum（clean/bullets/polite/casual/email/custom + 日本語ラベル + システムプロンプト）と `TextFormatter`（ephemeral URLSession、リクエスト 10 秒、temperature 0.2、失敗時は原文返しで throws しない）
- **macos/Core/Keychain.swift**: `write()` を delete→add 化、`apiKey(for:)` に自己修復移行を追加
- **macos/Config/AppConfig.swift**: `SlotConfig` に `formatEnabled` / `formatMode` / `formatCustomPrompt`（既定値付き）。手書き `init(from decoder:)` を extension に実装し、既存ユーザーの保存スロットを decodeIfPresent + 既定値で後方互換読み込み（設定リセット防止）。`ConfigStore` に `@Published formatModel`
- **macos/UI/SettingsView.swift**: スロットタブに整形トグル + モード Picker +（custom 時）プロンプト欄、一般タブに整形モデル欄
- **macos/AppController.swift**: ストリーミング / REST 両経路の `Paster.paste` 直前で `formatter.format()` を適用
- **src/core/text_formatter.py**（新規）: `build_system_prompt` / `format_text`（httpx、Keychain → `GROQ_API_KEY` 環境変数の順でキー解決、失敗時原文）
- **src/config/constants.py / types.py**: `format_model`（グローバル）と hotkey1/2 の `format_enabled` / `format_mode` / `format_custom_prompt` を追加
- **src/app.py**: `_maybe_format` ヘルパーを追加し、ストリーミング確定・REST 完了の両経路で `_insert_and_enter` 直前に適用
- **src/ui/settings_window.py**: 各ホットキーに整形チェックボックス + モード Combo +（カスタム時のみ表示の）プロンプト欄、Advanced に Format Model 欄
- **検証**: Mac `swift build` エラー/警告 0 → `build_app.sh` → 実機再起動し、スロットタブの整形 UI（トグル → モード Picker → カスタム欄の段階表示）と既存設定の後方互換読み込み（ホットキー・バックエンドがリセットされないこと）をスクリーンショットで確認。Windows `py_compile` OK・ユニットテスト 53 件（新規 13 件含む）全パス・オフスクリーンで設定 UI 構築を確認。Groq への実呼び出しとKeychain 承認ダイアログ消滅は実ディクテーションでの確認待ち

### Fixed (2026-06-10 追記 2)
- **ホットキーがほとんど反応しない問題（Mac 版）**
  - 原因 1: ウィンドウを 1 つも持たないメニューバーアプリは App Nap の対象になり、イベントタップのコールバックが遅延 → OS にタイムアウト無効化されてホットキーが死ぬ。`beginActivity` と Info.plist `NSAppSleepDisabled` で App Nap を無効化し、タップ監視スレッドの QoS を `.userInteractive` に引き上げ
  - 原因 2: ad-hoc 署名はビルドごとに署名が変わり、入力監視の TCC 許可と不一致になる（タップは作成できるが OS が無効化し続ける）。ビルドスクリプトを自己署名証明書 `voicekey-codesign` があればそれで署名するよう変更（証明書の信頼登録はユーザー操作が必要）
  - 防御策: タップスレッドに 5 秒間隔のウォッチドッグを追加し、無効化通知の取りこぼしでも自動復旧。無効化理由（timeout/userInput）のログも追加
  - **根本原因（確定）**: ad-hoc 署名はビルドごとに署名 ID が変わり、入力監視の TCC 許可と不一致になるため OS がタップを無効化し続けていた（5 秒ごとに発生）。自己署名証明書 `voicekey-codesign` で署名を固定し、`tccutil reset` 後に 1 回だけ許可し直すことで完全解決。再ビルド→再起動でもタップ無効化 0 回・権限再要求なしを実機で確認
  - 署名固定の手順は `macos/README.md` の「署名について」に記載
- **設定画面の API キー・プロンプト入力欄が見えない問題（Mac 版）**
  - グループ化フォーム内の SecureField / TextField は枠が描画されず、プレースホルダがただのテキストに見えて入力欄と認識できなかった。`.textFieldStyle(.roundedBorder)` で明示的に枠付きに変更（API キー 4 欄・プロンプト・自動 Enter 遅延）
  - `fixedSize()` による高さ潰れの可能性も排除し、設定ウィンドウを明示サイズ（480×520）に変更
  - デバッグ用の設定ウィンドウ自動表示フラグ（`VOICEKEY_OPEN_SETTINGS`、一回限り）を追加し、スクリーンショットでの実機検証を可能にした

### Fixed
- **メニューバーアイコンが表示されない問題（Mac 版）**
  - 原因 1: SwiftUI `MenuBarExtra` がラベルの `NSImage` を正しく描画できない（青い円になる）
  - 原因 2: アイテム位置の永続化値（`NSStatusItem Preferred Position`）が不可視領域（ノッチ下・Hidden Bar の隠し領域）を指すと二度と表示されない。ユーザー環境では Hidden Bar が新規アイテムを隠し領域に配置していた
  - 対策: `MenuBarExtra` を廃止し AppKit `NSStatusItem` 直接管理へ書き換え（`VoicekeyApp.swift`）。エントリポイントを SwiftUI App から `NSApplicationDelegate` ベースに変更し、`startup()` をラベルの `.task` から `applicationDidFinishLaunching` へ移動。ドラッグでの取り外しを禁止（`behavior = []`）、設定ウィンドウは `NSHostingController` で自前管理。位置記録を可視領域にリセットして復旧

## [Unreleased] - 2026-06-10

### Changed
- **コア層の全面刷新（大規模バグ調査の結果に基づく Phase 1）**
  - `src/core/audio_recorder.py` 全面書き換え: 永続ストリーム方式へ移行
    - PortAudio の open/close を録音のたびに行わず、ストリームを開いたまま start/stop だけで録音を切り替える（close は 30 秒アイドル時・デバイス変更時・終了時のみ）
    - 専用の AudioControl スレッドが PortAudio 呼び出しをすべて直列実行。公開 API（`start_async` / `stop_async`）は完全ノンブロッキングで、pynput リスナースレッドを一切ブロックしない（macOS の CGEventTap タイムアウトでホットキーが死ぬ問題の根治）
    - `health()` / `recover()` による世代管理ウォッチドッグ復旧を実装。ハングした制御スレッドを見捨てて新世代に切り替え、取得済み音声は救出する
    - 停止時はストリームを閉じずに同期 stop するため、発話末尾の取りこぼしが発生しない
    - HUD 用に約 33ms 間隔の音声レベルコールバックを追加
  - `src/core/vad.py` 全面書き換え: torch + MPS 版 VadFilter を廃止し、onnxruntime (CPU) + numpy のみの `SileroVad` に置換
    - torch のトップレベル import（起動遅延の主因の一つ）を排除。VAD ロード約 88ms・推論 4ms/2 秒音声
    - アプリ全体で 1 インスタンスを共有（旧実装はスロットごとに二重ロード）
    - `speech_bounds()` を新設し、録音前後の無音・ノイズ区間をトリミングして API へ送る量と幻覚を削減
  - `src/core/api_transcriber.py` 新設: OpenAI / Groq トランスクライバを統合
    - openai / groq SDK を廃止し httpx の multipart POST に統一（OpenAI 互換 REST）。SDK の import 時間を排除
    - `prewarm()` で録音開始時に TLS 接続を事前確立し、初回 API 呼び出しの往復を短縮
    - 失敗は "Error:" 文字列ではなく `TranscriptionError` 例外で伝達
    - 旧 `openai_transcriber.py` / `groq_transcriber.py` は削除
  - `src/core/audio_utils.py`: MP3 変換（ffmpeg サブプロセス）を廃止し WAV 専用に簡素化
    - import 時に `ffmpeg -version` を実行していた起動ブロック（最大 5 秒）を排除
    - int16 変換前に `np.clip` を追加し、範囲外サンプルのラップアラウンド（轟音ノイズ化）を防止

### Fixed
- **「ほぼ無音＋ノイズ」録音で全く違う内容が出力される問題（幻覚）への対策**
  - `src/core/audio_preprocess.py:normalize_volume`: ゲイン上限 +20dB を新設。従来は上限がなく、ノイズフロアだけの録音がフルボリュームまで増幅されて API が架空テキストを生成する主要因だった
- **API キーの Keychain 読み出しを毎回行っていた問題**
  - `src/utils/secrets.py`: プロセス内キャッシュを追加（set/delete で無効化）。録音のたびに数十 ms の Keychain アクセスが走らなくなった
  - ElevenLabs 用サービス識別子 `SERVICE_ELEVENLABS` を追加

### Technical Details
- **書き換え**: `src/core/audio_recorder.py`, `src/core/vad.py`, `src/core/audio_utils.py`
- **新規**: `src/core/api_transcriber.py`（`ApiTranscriber` 基底 + `OpenAITranscriber` / `GroqTranscriber`）
- **削除**: `src/core/openai_transcriber.py`, `src/core/groq_transcriber.py`
- **編集**: `src/core/audio_preprocess.py`（`MAX_GAIN_DB`）、`src/utils/secrets.py`（キャッシュ）、`src/core/__init__.py`（エクスポート更新）
- スモークテスト: コア層 import 272ms（torch 削除前は約 1.4 秒）、WAV クリップ・ゲイン上限・VAD 無音/ノイズ判定を確認済み

### Changed (続き: アプリ層)
- **`src/app.py` 全面書き換え（`SuperWhisperApp` → `VoicekeyApp`）**
  - リスナーハンドラを完全ノンブロッキング化（キー集合更新とコマンド投函のみ）。macOS CGEventTap タイムアウトによるホットキー死亡を構造的に防止
  - UI 状態は `_emit_state()` の単一発信点に集約（「アイコンが録音中のまま」の根治）
  - 常駐ワーカー 1 本が 正規化 → VAD ゲート/トリム → API → テキスト挿入 を直列処理
  - 左修飾キーのマッチング修正: macOS の pynput は左修飾キーを汎用名（cmd 等）で報告するため、`<cmd_l>` 設定が一致しなかった問題を `_acceptable_names()` で解消
  - toggle モードも低レベル Listener に統一（GlobalHotKeys 廃止。右修飾キー対応＋エッジ検出でキーリピート誤発火を防止）
  - ウォッチドッグ: PortAudio ハング自動復旧（recover）、録音 300 秒上限、リスナースレッド死活監視
  - 起動時に macOS 権限（アクセシビリティ・入力監視）をチェックし、不足時はダイアログでシステム設定へ誘導
  - ホットリロードでリスナー再起動が不要な設計に変更（ハンドラがイベント時にスロット設定を参照）。退役トランスクライバは 30 秒後に遅延 close
  - dev_mode のタイミングファイル出力・引用符ラップを削除（ログ出力に一本化）
- **`src/core/input_handler.py`**: 貼り付け後にユーザーの元クリップボード内容を復元（テキストのみ）

### Technical Details (続き)
- **書き換え**: `src/app.py`
- **編集**: `src/core/input_handler.py`（クリップボード復元）、`src/platform/base.py` / `src/platform/macos/adapter.py`（`check_input_permissions` / `open_permission_settings` 追加）、`src/main.py` / `src/__init__.py`（クラス名変更追従）

## [Unreleased] - 2026-06-01

### Fixed
- **macOS フリーズ後に「毎回 2 秒待たされる」現象を根絶し、録音停止を完全ノンブロッキング化**
  - `src/core/audio_recorder.py:_cleanup_stream`: PortAudio の `stream.stop()/close()` を daemon スレッドへ投げっぱなしにし、呼び出し元は **一切待たない（join しない）** よう変更。従来は `join(timeout=2.0)` で待っており、一度 close がハングするとその後の録音停止が毎回最大 2 秒ブロックしていた
  - 未使用化した `_CLEANUP_TIMEOUT_SEC` 定数を削除
  - トレードオフ: close がハングしたストリームは OS のマイクを掴んだまま残る（マイクインジケーターが消えない）が、録音・文字入力動作は一切遅延しない。残ったインジケーターはアプリ再起動で解消する
- **ダブルタップ連打時に録音を取りこぼす（samples=0）レースを解消**
  - `src/app.py:stop_and_transcribe`: `_recorder.stop()` を `_finalize_recording_async`（別スレッド）から呼んでいたため、停止完了前に次の録音 start が割り込み、状態がズレて空録音になることがあった。close を待たない設計になったため `stop()` を `_recording_lock` 内で同期実行して音声をその場で確定し、`_active_slot` も即クリアするよう変更
  - `src/app.py:_finalize_recording_async`: シグネチャを変更し確定済み `audio_data` を引数で受け取る（内部での `_recorder.stop()` 呼び出しを削除）。担当は音量正規化とキュー投入のみ
  - `src/app.py:start_recording`: `self._recorder.start()` の戻り値を確認し、開始に成功してから `_is_recording` を立てるよう変更。app 側と recorder 側の状態がズレて「録音中のつもりだが録れていない」ゾンビ状態になるのを防止

### Technical Details
- **編集**: `src/core/audio_recorder.py`（`_cleanup_stream` を fire-and-forget 化、`_CLEANUP_TIMEOUT_SEC` 削除）
- **編集**: `src/app.py`（`start_recording` の start 戻り値チェック、`stop_and_transcribe` の同期 stop 化と `_active_slot` 即クリア、`_finalize_recording_async` の引数化）

## [Unreleased] - 2026-05-28

### Changed
- **README.md を AI エージェント向けセットアップに最適化**
  - 目次に「AI エージェント向けセットアップ手順」を追加
  - 前提条件チェックリスト（OS / Python / git / ffmpeg / ネットワーク）と確認コマンドを表形式で明示
  - macOS / Windows それぞれ「クローン → ffmpeg → venv → 依存関係 → 設定ファイル → 起動」を 1 ブロックで完結するコピペ可能なコマンド列に再整理
  - API キーは設定ウィンドウから入力すると OS シークレットストアに保存される旨を明記し、旧来の `.env` 直書き手順から更新
  - 「ポータル経由で配布物をダウンロードする場合」セクションを追加し、`tag v*` push が GitHub Actions を経由して Releases に直リンクされるリリースフローを記載
  - トラブルシューティングに「macOS で録音中・停止後にアプリがフリーズする」項目を追加（PR #9 で根治済み・Force Reset の使い方）
  - よくあるつまずきポイント（権限再起動・`python3` 必須・`pip install` 遅延・GPU 不要・Windows 管理者権限）を AI が事前案内できる形で明文化

## [Unreleased] - 2026-05-05

### Fixed
- **macOS PortAudio 由来のフリーズ問題に対処**
  - `_recorder.stop()` が `_recording_lock` を握ったまま PortAudio (CoreAudio) の `stream.stop()` / `close()` を呼ぶと、CoreAudio がハングした際にロックを巻き込んでアプリ全体が停止する問題があった
  - `src/core/audio_recorder.py:_cleanup_stream`: `stream.stop()` / `close()` を別スレッドへ逃がし、最大 2 秒のタイムアウトで諦めて呼出元へ復帰。`self._stream` は即 `None` に切替えるため後続の start/stop は新ストリーム前提で進める。タイムアウトしても `_collect_audio_data()` でキューから音声を回収するので発話内容はロストしない（ユーザーは 2 秒余分に待つだけで結果が得られる）
  - `src/app.py:stop_and_transcribe`: `_recording_lock` を解放してから `_recorder.stop()` を呼ぶよう修正。lock を巻き込まないためアプリ全体のフリーズを防止
- **PortAudio ゾンビ callback による録音バッファ汚染を解消**
  - 古い stream は `_cleanup_stream` で `_stream = None` にしても、PortAudio の I/O スレッドが close 完了まで callback を呼び続け、共通の `self._queue` に古い音声を流し込み続けるため 2 回目以降の録音が無音判定 (`has_speech=False`) になっていた
  - `src/core/audio_recorder.py`: 録音セッション識別子 `_session_id` を導入。`start()` のたびにインクリメントし、`_make_audio_callback(session_id)` でセッション ID を埋め込んだクロージャを各 `InputStream` に渡す。callback は `if self._session_id != my_session: return` で旧 stream のゾンビ呼出を即弾く
  - これにより `stream.close()` がハング中でも、新セッションの queue は旧 stream の音声で汚染されない
- **ダブルタップ Auto-Enter 検出が PortAudio ハング時に失われる問題**
  - `stop_and_transcribe` が keyboard listener スレッド内で `_recorder.stop()`（最大 2 秒ブロック）まで実行していたため、キーを離した直後の次の press イベントが listener で待たされ、ダブルタップ判定ウィンドウ (400ms) を超えてしまっていた
  - `src/app.py`: `stop_and_transcribe` はフラグ更新のみ同期で行い、`_finalize_recording_async` を別 daemon スレッドで起動して `_recorder.stop()` 以降を実行。listener スレッドは即時に次のキーイベントを処理可能に

### Added
- **Force Reset (Unfreeze) メニューを再導入**
  - 過去に削除されたが、PortAudio ハング時の最終手段として復活。ただし用途が変わり、内部状態リセットではなく **プロセスごとの再起動** で OS のマイクハンドル / 「マイク使用中」オレンジドット / メニューバーアイコンを完全にリセットする
  - `src/app.py:force_reset_recording`: `subprocess.Popen([sys.executable] + sys.argv, start_new_session=True)` で同じコマンドラインの新プロセスを独立起動し、自分は `os._exit(0)` で即時終了。execv 方式だと macOS で NSStatusItem が再登録されない事象があったため subprocess + 新セッション方式に統一
  - `src/ui/system_tray.py`: メニューに `Force Reset (Unfreeze)` 項目と `force_reset` Signal を追加
- **フリーズ再現用デバッグスクリプト** (`scripts/simulate_freeze.py`)
  - `sounddevice.InputStream.stop` / `close` を「指定秒数だけ眠るだけ」のメソッドに monkeypatch してから voicekey 本体を起動。`FREEZE_SEC` 環境変数で待ち時間を制御（既定 30 秒）
  - 上記フリーズ系修正の動作検証を確実に再現できるようにするため
- **録音状態のデバッグログ拡充**
  - `src/core/audio_recorder.py`: `start()` でストリーム ID、`_audio_callback` で各セッション初回の callback 受信、`stop()` で取得した `queue_items` / `samples` / `duration` / `callback_received` をログ出力。フリーズや録音欠損の切り分けに使用

### Technical Details
- **編集**: `src/app.py`（import に `os` / `subprocess` / `sys` を追加、`stop_and_transcribe` の非同期化、`_finalize_recording_async` 新設、`force_reset_recording` を再起動方式へ書き換え、`_tray.force_reset` の signal 接続）
- **編集**: `src/core/audio_recorder.py`（`_session_id` / `_callback_received` 追加、`_make_audio_callback` クロージャ生成、`_cleanup_stream` のタイムアウト化、`stop()` のログ拡充）
- **編集**: `src/ui/system_tray.py`（`force_reset` Signal、Force Reset メニュー項目）
- **新規**: `scripts/simulate_freeze.py`

## [Unreleased] - 2026-05-01

### Added
- **API キーの OS シークレットストア保管 (macOS Keychain / Windows Credential Manager)**
  - 新規モジュール `src/utils/secrets.py`: `keyring` ライブラリを通じて `get_api_key` / `set_api_key` / `delete_api_key` を提供（サービス識別子 `voicekey.Groq` / `voicekey.OpenAI`）
  - 設定ウィンドウの各 Hotkey の API 設定エリアに「API Key」入力欄（パスワードマスク）と Save / Clear ボタンを追加。同じ backend を選んだ Hotkey 間で同じエントリを共有
  - 取得は **Keychain → 環境変数** の優先順。既存の `.env` / `GROQ_API_KEY` / `OPENAI_API_KEY` 利用は維持され、後方互換を保ったまま Keychain に移行可能（自動マイグレーションは行わない）
  - `settings.yaml` には API キーを書き込まない（ConfigManager 側は変更なし）
- **macOS でのメニューバー常駐動作**
  - `python run.py` 起動時に `NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)` を呼んで Dock / Cmd+Tab から非表示化
  - PyInstaller ビルド版は `.app` バンドル化し、`Info.plist` に `LSUIElement: True` / `NSPrincipalClass: NSApplication` / `NSMicrophoneUsageDescription` を含める（`voicekey.spec`）
  - 設定ウィンドウを開く処理を `raise_()` → `activateWindow()` → `NSApp.activateIgnoringOtherApps_(True)` の順で前面化するよう修正（メニューバー → Settings で確実に最前面に出る）

### Changed
- `requirements.txt` に `keyring>=24.0` と `pyobjc-framework-Cocoa>=10.0; sys_platform == "darwin"` を追加
- `PlatformAdapter` に `configure_app_visibility(hide_from_dock)` と `bring_to_front(window)` を追加（既定 no-op）。macOS アダプタでのみ AppKit 経由で実装し、Windows は no-op
- `GroqTranscriber._get_client` / `OpenAITranscriber._get_client` の API キー取得経路を `_resolve_api_key()` ヘルパー経由に統一（Keychain → 環境変数）。エラーメッセージも「設定ウィンドウから保存するか、環境変数を設定してください」に更新
- **メニューバーアイコンの左クリック挙動を変更**: 以前は左クリック / ダブルクリックで設定ウィンドウが直接開いていたが、コンテキストメニューを表示するだけに変更。ユーザーがメニューから「Settings」を選んだ時にのみ設定ウィンドウを開く（`src/ui/system_tray.py` の `_setup_click_handler` / `_on_activated` を削除、`setContextMenu` のみで動作）

### Removed
- **Force Reset 機能を完全削除**
  - トレイメニューの「Force Reset」項目（`src/ui/system_tray.py` の `force_reset` Signal とアクション）を削除
  - `src/app.py` から `force_reset()` メソッド本体、シグナル接続、`_reset_generation` 世代カウンタ、`_queue_processor` / `_process_transcription_task` 内の世代比較による結果破棄ロジックを全て削除
  - 通常運用で連打フリーズ等の根治対応（自動復旧ループ・録音解除等）が既に入っているため、ユーザー手動のリセットボタンは不要と判断
- **Dynamic Island 風オーバーレイ UI を完全廃止** (`src/ui/overlay.py` 削除)
  - 録音 / 文字起こし状態は **トレイアイコンの色だけ** で判別する設計に統一（IDLE 青 / RECORDING 赤 / RECORDING_AUTO_ENTER 紫 / TRANSCRIBING オレンジ）
  - `src/app.py` から overlay 関連の初期化（`_setup_ui_components` 内）、状態反映 (`_update_ui_status`)、音声レベル反映 (`_on_audio_level`)、波形コールバック登録 (`set_level_callback`) をすべて削除
  - `_show_backend_warning` を「オーバーレイにメッセージ表示」から「ログに warning 出力」に変更（API キー未設定時など）
  - `src/ui/__init__.py` から `DynamicIslandOverlay` の export を削除
  - `src/config/constants.py` から UI セクションの `OVERLAY_BASE_WIDTH` / `OVERLAY_BASE_HEIGHT` / `OVERLAY_EXPANDED_WIDTH` / `OVERLAY_EXPANDED_HEIGHT` / `OVERLAY_TOP_MARGIN` / `ANIMATION_DURATION_MS` を削除
  - 副次効果: 起動時にオーバーレイ用の QMainWindow を作らないため、初期化が軽量化

### Security
- **`settings.yaml` を git 追跡対象から除外**
  - `.gitignore:81` に `settings.yaml` が記載されていたが、過去に誤って追跡されていたため `git rm --cached settings.yaml` で解除（ローカルファイルは残存）
  - 新規 `settings.example.yaml` をコミット対象に追加。新規ユーザー / Clone 時はこのファイルをコピーして使う
  - 将来 `settings.yaml` に万一機密情報を書き込んでも誤コミットされないよう予防
- **依存パッケージの完全 lock**
  - 新規 `requirements.lock`: `pip freeze --exclude-editable` で venv 内の全パッケージとバージョンを固定（再現性 / サプライチェーン耐性向上）
  - 既存の `requirements.txt` は人間が読みやすい下限指定の形を維持。lock ファイルは並列に追加するだけで既存インストール手順への影響なし

### Technical Details
- **新規ファイル**: `src/utils/secrets.py` / `settings.example.yaml` / `requirements.lock`
- **編集**: `src/main.py`（QApplication 作成後に `configure_app_visibility(True)` を呼び出し）
- **編集**: `src/app.py`（`_open_settings` を最前面化シーケンスに変更）
- **編集**: `src/platform/base.py` / `src/platform/macos/adapter.py`（可視性制御メソッドを追加）
- **編集**: `src/core/groq_transcriber.py` / `src/core/openai_transcriber.py`（`_resolve_api_key` 追加）
- **編集**: `src/ui/settings_window.py`（API キー入力欄、`_save_api_key` / `_clear_api_key` / `_refresh_api_key_status` 追加、backend 切替時に Keychain ステータス再描画）
- **編集**: `src/ui/system_tray.py`（左クリック直接起動を廃止、メニュー経由のみ）
- **編集**: `voicekey.spec`（macOS 用 BUNDLE と Info.plist）

## [Unreleased] - 2026-04-30

### Added
- **音声前処理パイプライン（音量正規化）**
  - 新規モジュール `src/core/audio_preprocess.py` を追加
  - Peak+RMS ハイブリッド音量正規化：目標 RMS = -20 dBFS、ピーク上限 = -3 dBFS（音割れ防止）
  - 録音直後・API 送信前に適用、numpy のみで <1ms の低レイテンシ
  - 小さい声を底上げして API 文字起こしの精度を向上、大音量はクリッピング防止のため抑え込み
  - ノイズ対策は API モデル側に任せる方針（noisereduce 等は採用せず）
- **Auto Enter Delay スライダーを設定 UI に追加**
  - ダブルタップ Auto-Enter 機能で、テキスト挿入後から Enter 押下までの待機時間を 0〜500ms で調整可能（`src/ui/settings_window.py`）
  - 既定値 50ms。一部アプリが即時 Enter に反応しない問題に対するユーザー調整手段（`src/config/constants.py`）

### Changed
- `DEFAULT_CONFIG` に `audio_preprocess.volume_normalize` キーを追加（既定 True）
- 設定 UI の Advanced タブに音声前処理セクションを追加
- `stop_and_transcribe()` で `recorder.stop()` 直後に `preprocess_audio()` を呼ぶよう変更（`src/app.py`）

### Fixed
- **録音状態の Race Condition 解消（Phase 3）**
  - `_recording_lock` (RLock) を導入し、`start_recording` / `stop_and_transcribe` / `force_reset` の check-then-set を直列化（`src/app.py`）
  - 並列スレッドから start/stop が同時に呼ばれた場合に `_is_recording` と `_active_slot` の整合性が崩れる問題を解消
  - `start_recording` で transcriber 取得失敗時に `_active_slot` をリセットするよう修正（リーク防止）
  - 6 並列スレッドで 600 回の start/stop を実行しても整合性が保たれることを確認
  - ロック順序: `_recording_lock` → `_queue_worker_lock` → `recorder._lock`（逆順は禁止、デッドロック防止）

- **プラットフォーム整合性の向上（Phase 2）**
  - `InputHandler.insert_text` の貼り付けキー操作を `with pressed(...)` から明示的な `try/finally` に変更。`'v'` の release で例外が発生しても修飾キー（Cmd/Ctrl）が確実に解放されるよう改善（`src/core/input_handler.py`）
  - `OpenAITranscriber` / `GroqTranscriber` に `close()` メソッドを追加し、`unload_model()` から呼び出すよう変更。httpx 接続プールを明示的に閉じてリークを防ぐ（`src/core/openai_transcriber.py`, `src/core/groq_transcriber.py`）
  - `_setup_hotkey_slots()` の冒頭で旧 `api_transcriber.close()` を呼び、Hot reload 時に旧クライアントの HTTP 接続が leak する問題を解消（`src/app.py`）
  - `_apply_config_changes()` で slots 変更検出時に `self._listener.stop()` を呼び、自動再起動ループに新設定でリスナーを再立ち上げさせる（`src/app.py`）

- **連打フリーズ問題の根治（マイク占有/キー押下誤認識/Force Reset 効かず）**
  - `force_reset()` で `_pressed_keys` / `_last_hotkey_release_time` / `_last_hotkey_release_slot` をクリアするよう修正。リセット後も「キーが押されたまま」と誤認識される問題を解消（`src/app.py`）
  - キーボードリスナー (`_start_keyboard_listener`) を自動復旧ループ化。例外で死んでも黙って永久停止せず、押下キー状態をクリアして再起動する（`src/app.py`）
  - `_quit_app()` で `listener.stop()` と `recorder.stop()` を明示的に呼ぶよう修正。終了時にマイクが OS にロックされ続ける問題を解消（`src/app.py`）
  - `AudioRecorder` の `start` / `stop` / `_cleanup_stream` を `threading.RLock` で直列化。`stop` 中に `start` が割り込んで旧ストリームが OS 占有のまま捨てられる競合を解消（`src/core/audio_recorder.py`）
  - `_cleanup_stream` で `stream.stop()` と `stream.close()` を独立 try/except で囲み、片方が例外を出しても他方を必ず実行するよう修正（`src/core/audio_recorder.py`）
  - `_queue_processor` の各タスク処理を try/except/finally で囲み、個別タスクの例外でワーカー全体が死なないようにした。`task_done()` も常に呼ぶ（`src/app.py`）
  - `_queue_worker_running` の check-and-set を `_queue_worker_lock` で排他化し、二重ワーカー起動を防止（`src/app.py`）
  - `_handle_key_press` / `_handle_key_release` で `_normalize_key` 失敗時の挙動を改善。debug ログ出力＋永久録音を防ぐ保険として「押下キー無し＋録音中」検出時に自動停止（`src/app.py`）

### Technical Details
- **src/app.py**
  - `_setup_state` に `_queue_worker_lock` (Lock) と `_listener` 参照保持を追加
  - `_start_queue_worker` を `_start_queue_worker_locked` にリネーム（呼び出し側がロック取得済み前提）
  - キーボードリスナー再起動ループにより Hot reload 時のリスナー入れ替えも将来対応可能
- **src/core/audio_recorder.py**
  - `__init__` に `threading.RLock` を追加し、ライフサイクル全パスを保護
  - `start()` 冒頭で残骸ストリームのクリーンアップを実施

---

## [Unreleased] - 2026-04-18

### Added
- **Auto Enter 遅延調整スライダー**
  - ダブルタップ時のテキスト挿入後〜Enter押下までの待機時間をUIから調整可能に
  - Settings の Advanced ページにスライダー（0〜500ms、既定50ms）と現在値ラベルを追加
  - 即時Enterに反応しないアプリ（Slack、一部Webフォーム等）向けに遅延を伸ばせる
  - 新規設定キー `auto_enter_delay_ms` を追加（settings.yaml・ホットリロード対応）

### Technical Details
- **constants.py**: `DEFAULT_CONFIG` に `auto_enter_delay_ms: 50` を追加
- **settings_window.py**: `QSlider` + `QLabel` を Advanced ページに追加、load/save に反映
- **app.py**: `_handle_transcription_result()` のハードコード `time.sleep(0.05)` を `self._config.get("auto_enter_delay_ms", 50)` 参照に置換

---

## [Unreleased] - 2026-04-08

### Added
- **強制リセット機能**
  - トレイアイコンの右クリックメニューに「Force Reset」を追加
  - 録音中・文字起こし中の全処理を強制停止してidle状態に復帰
  - 世代カウンタにより実行中のAPI呼び出し結果も安全に破棄

- **ダブルタップ + ホールドで自動Enterキー送信**
  - ホールドモードでホットキーをダブルタップ（2回目を長押し）すると、文字起こし結果入力後にEnterキーを自動送信
  - チャットアプリでの音声入力→送信をワンアクションで完結
  - ダブルタップ判定ウィンドウ: 400ms

### Technical Details
- **types.py**: `TranscriptionTask` に `auto_enter` フィールドを追加
- **input_handler.py**: `press_enter()` メソッドを追加（pynput Key.enter使用）
- **system_tray.py**: `force_reset` シグナルとメニュー項目を追加
- **app.py**: `force_reset()` メソッド、世代カウンタ `_reset_generation`、ダブルタップ検出ロジック、`text_ready` シグナルを `Signal(str, bool)` に拡張

---

## [Unreleased] - 2026-02-27

### Added
- **Cross-platform 抽象レイヤーを追加**
  - `src/platform/` を新設し、OS差分を `core` から分離
  - `PlatformAdapter` インターフェースと `get_platform_adapter()` ファクトリを追加
  - `windows` / `macos` 向けアダプタ実装を追加

- **入力デバイス選択機能を追加**
  - Settings の Advanced ページでマイク入力デバイスを選択可能
  - `audio_input_device` 設定キーを追加（`default` / デバイスID）
  - 録音開始時に指定デバイスを使用し、失敗時は自動でデフォルトへフォールバック

- **運用ドキュメントの追加**
  - `docs/CROSS_PLATFORM_UNIFICATION_PLAN.md`（統合計画）
  - `docs/CROSS_PLATFORM_TEST_CHECKLIST.md`（検証チェックリスト）
  - `run.sh`（macOS/Linux向け起動スクリプト）

### Changed
- **入力処理を platform 注入方式へ移行**
  - `src/core/input_handler.py` の `sys.platform` 分岐を削除
  - 貼り付け修飾キー（Cmd/Ctrl）を platform アダプタで制御

- **録音設定の動的反映を強化**
  - `settings.yaml` の変更監視で入力デバイス設定の更新を即時適用

- **UI のOS依存ロジックを分離**
  - `src/ui/settings_window.py` のホットキー変換を platform 経由に変更
  - `src/ui/system_tray.py` のアクティベーション判定を platform ポリシー化
  - `src/ui/styles.py` のフォント指定を OS別フォールバック対応に変更

- **アプリ初期化の依存注入を整理**
  - `src/app.py` で platform アダプタを初期化し、
    InputHandler / SettingsWindow / SystemTray / キー正規化に注入

### Technical Details
- **新規追加**
  - `src/platform/base.py`
  - `src/platform/factory.py`
  - `src/platform/common/keymap.py`
  - `src/platform/windows/adapter.py`
  - `src/platform/macos/adapter.py`

- **更新**
  - `src/app.py`
  - `src/core/input_handler.py`
  - `src/ui/settings_window.py`
  - `src/ui/system_tray.py`
  - `src/ui/styles.py`
  - `README.md`

## [Unreleased] - 2026-02-03

### Added
- **起動時プリロード機能の実装**
  - 起動時にVADモデルをバックグラウンドでプリロードし、最初の文字起こしを高速化
  - `preload_on_startup` 設定オプションを追加（デフォルト: true）
  - `app.py` に `_preload_models_async()` を追加

### Fixed
- **VADプリロードのタイミング改善**
  - ホットキースロット初期化後にプリロードを実行するよう調整
  - 起動順序を `setup_state -> start_background_threads -> preload` に整理

### Technical Details
- **src/app.py**
  - `_preload_models_async()` を追加し、設定に応じて非同期プリロードを実行
  - `_preload_vad_model()` を実行ロジック専用に整理
- **src/config/constants.py**
  - `DEFAULT_CONFIG` に `preload_on_startup: true` を追加

## [Unreleased] - 2026-01-30

### Added
- **文字起こしキューイング機能の実装**
  - 文字起こし処理中に新しい録音を開始しても、前タスクを破棄せずキューに追加
  - すべての録音結果を順番に処理して入力
  - `queue.Queue` を使用したスレッドセーフなタスク管理
  - `TranscriptionTask` データクラスを追加

### Changed
- **app.py の文字起こし処理ロジックをキュー方式へ変更**
  - `start_recording()` からキャンセル方式を削除
  - `stop_and_transcribe()` でキュー投入
  - `_start_queue_worker()`, `_queue_processor()`, `_process_transcription_task()` を追加
  - `_handle_transcription_result()` は結果処理専用にし、idle遷移はワーカー管理へ移行

### Technical Details
- **src/config/types.py**
  - `TranscriptionTask` データクラスを追加（audio_data, slot_id, timestamp）
- **src/app.py**
  - `_transcription_queue` / `_queue_worker_running` を追加
  - キュー処理完了時に `idle` へ復帰する制御を追加

## [Unreleased] - 2026-01-15

### Added
- **CONTRIBUTING.md ドキュメント作成**
  - 詳細なバージョニングルール（X=大きな変更、Y=ユーザーが気づく変更、Z=小さな修正）
  - コミットメッセージ規約（type: description形式）
  - 変更記録（CHANGELOG）の運用ルール
  - ブランチ戦略とリリースプロセス
  - プルリクエストのガイドライン
  - プッシュのタイミングとチェックリスト

- **デュアルホットキー機能の実装**
- **2つの独立したホットキー設定**: 固定で2つのホットキースロットを追加
  - 各ホットキーに対して異なるショートカット、モード（hold/toggle）、バックエンド（local/groq/openai）を設定可能
  - APIバックエンド（Groq/OpenAI）の場合、各ホットキーで異なるモデルとプロンプトを指定可能
  - ローカルバックエンドは両方のホットキーで共通の設定を使用（VRAM節約）

- **新しい設定構造**: `settings.yaml` の階層化
  - `hotkey1` / `hotkey2`: 各ホットキーの個別設定
  - `local_backend`: ローカルGPU設定（共通）
  - `language`, `vad_filter` などのグローバル設定

- **自動マイグレーション機能**
  - 旧設定フォーマット（単一ホットキー）を検出し、新形式に自動変換
  - 既存ユーザーの設定を保持しながらアップグレード可能
  - マイグレーション時のログ出力

- **設定UIの刷新**
  - Generalページ: 2つのホットキーを横並びで設定
  - 各ホットキーグループに: ショートカット入力、モード選択、バックエンド選択、API設定
  - Modelページ: ローカル共通設定のみに簡略化
  - API設定の動的表示（バックエンド選択に応じて表示/非表示）

### Changed
- **CLAUDE.md に自動コミットルール追加**
  - AI開発者向けに、機能実装完了時の自動コミットルールを明記
  - コミットのタイミング、必須チェック項目、例外ケースを定義
  - プッシュは手動実行（自動プッシュしない）
  - ユーザーへの報告フォーマットを標準化

- **README.md コントリビューションセクション更新**
  - CONTRIBUTING.md へのリンク追加
  - 開発ガイドラインへのナビゲーション改善

- **app.py の大幅なリファクタリング**
  - `HotkeySlot` データクラスを追加（各スロットの状態管理）
  - `_hotkey_slots` 辞書で複数ホットキーを管理
  - `_local_transcriber` を共有インスタンスとして分離
  - `_active_slot` で現在アクティブなスロットを追跡
  - キーボードリスナーが両方のホットキーを同時監視
  - `start_recording()` にスロットID引数を追加

- **config_manager.py の強化**
  - `_deep_merge()` 関数でネストされた辞書のマージをサポート
  - `_migrate_legacy_config()` メソッドで旧設定を自動変換
  - 深いマージによりデフォルト設定との統合を改善

- **ホットリロード機能の維持**
  - `_apply_config_changes()` が新構造に対応
  - ホットキー設定変更時の自動更新
  - バックエンド変更時のAPI Transcriber再作成
  - ローカル設定変更時のモデルアンロード

### Technical Details
- **types.py**
  - `HotkeySlotConfig` データクラスを追加

- **constants.py**
  - `DEFAULT_CONFIG` を新構造（hotkey1/hotkey2/local_backend）に変更
  - `default_api_models` でバックエンド別のデフォルトモデルを定義

- **settings_window.py**
  - `_create_hotkey_group()` で各スロットのUIを生成
  - `_create_api_settings_widget()` でAPI設定ウィジェットを動的生成
  - `_on_slot_backend_changed()` でバックエンド変更を処理
  - `_load_current_settings()` / `_save_settings()` を新構造に対応

### Fixed
- ホットキー競合時の優先順位（最初に検出されたスロットが優先）
- Hold/Toggle混在時のキーボードリスナー処理

---

## [v2.0.0] - 2026-01-15

### Added
- デュアルホットキースロットとMP3音声サポート
- OpenAIバックエンドとGroqバックエンドのモジュール化
- macOSスタイルの設定UI

### Changed
- プロジェクト構造のリファクタリング
- バックエンドの分離（local/groq/openai）

---

## [Previous Releases]

### [2026-01-05] - LLMプロンプト処理の改善
- LLMプロンプト処理のリファクタリング
- 設定UIの改善

### [2025-12-09] - UI改善とドキュメント更新
- オーバーレイUIの改善
- ホットキー処理の改善
- AI用コメントルールの追加
- README更新

### [2025-12-09] - v2.0.0リリース
- 日本語コメントの追加
- LLM処理ログ表示
- コード整理

### [2025-12-08] - LLM後処理機能
- LLM後処理機能の追加
- GUI設定の追加
- macOSスタイルのUIテーマ
- オーバーレイの改善

### [2025-12-08] - Groqバックエンド統合
- Groq API対応
- VADフィルター統合
- PyInstallerビルド設定更新

### [2025-12-08] - プロジェクト整形
- 全体的なコード整形
- 安定版リリース

### [2025-12-01] - 初期リリース
- プロジェクト名変更（SuperWhisperLike → voicekey）
- GNU GPL v3ライセンス追加
- 無音検出のUIフィードバック
- エラーハンドリング改善
- ビルドアーティファクトのクリーンアップ

---

## Notes

### 変更記録のガイドライン
- すべての機能追加、変更、修正を記録する
- 各エントリには簡潔な説明と影響範囲を含める
- 技術的な詳細は "Technical Details" セクションに記載
- ユーザー影響のある変更は目立つように記載

### バージョニング
- メジャーバージョン: 破壊的変更または大規模な機能追加
- マイナーバージョン: 後方互換性のある機能追加
- パッチバージョン: バグ修正とマイナーな改善
