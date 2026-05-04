# Changelog

voicekeyの変更履歴を記録するファイルです。

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
