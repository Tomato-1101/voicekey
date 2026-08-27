# OVERVIEW — voicekey 全体地図

プロジェクトが大きくなっても全体像を一目で掴むための「地図」。
**最初にここを読めば、何がどこにあるか・どのドキュメントを見ればいいかが分かる**ようにしてある。
詳細は重複させず、各専門ドキュメント（README / CHANGELOG / HANDOFF / CLAUDE.md）へのリンクで示す。

> ブランチは `personal` 1 本なので、このファイルの内容が唯一の正本。
> 機能・アーキを変えたら同じコミットで該当行を直す（末尾「更新ルール」）。

---

## 1. voicekey とは

ホットキーを押している間だけ音声を録音し、文字起こし結果を**いま使っているアプリのカーソル位置へ自動入力**する常駐型音声入力ツール。
Mac はメニューバー常駐、Windows はタスクトレイ常駐。文字起こしはクラウド API、発話区間検出（VAD）だけローカル実行（Python=Silero ONNX を onnxruntime/CPU、Mac=エネルギー RMS）。

## 2. 構成（プラットフォーム二本立て）

| | Mac 版 | Windows 版 |
|---|---|---|
| 言語/UI | Swift（AppKit メニューバー + SwiftUI 設定画面） | Python（PySide6 / Qt） |
| 置き場所 | `macos/Sources/Voicekey/` | `src/` |
| 設定の保存先 | `UserDefaults`（`ConfigStore`） | `settings.yaml`（`ConfigManager`）+ `QSettings`（フラグ） |
| ビルド | `macos/scripts/build_app.sh` / 配布は `build_dmg.sh` | `pyinstaller voicekey.spec` / 配布は GitHub Actions |

同じ機能でも別コードベース。**UI 文言・表示名・設定項目・機能挙動など両 OS に同等に存在する要素を変えるときは、両方を同じコミットで直す**（OS 固有 API でしか存在しないものだけ片方で完結）。詳細は `CLAUDE.md` / `AGENTS.md`。

## 3. ブランチ運用（personal 一本）

**`personal` ブランチ 1 本だけで運用する。** 旧 `main`（自分用）/ `release`（製品版）/ `voice-agent` は
**2026-08-23 に personal へ統合してアーカイブ済み**（GitHub からも削除。バックアップは
`~/Project/_archive/voicekey-all-branches-2026-08-23.bundle`）。

| 項目 | personal（唯一の版） |
|---|---|
| 文字起こしの選択肢 | 4 プロバイダーを**実名表示**（OpenAI / Groq / ElevenLabs / Deepgram）＋ **ローカル（Apple）オンデバイス**（macOS 26+・キー不要） |
| モデル選択 | あり（フルコントロール） |
| テキスト整形（Groq） | トグルあり・モデル/プロンプト選択可＋整え方プリセット 4 種（標準/そのまま/すっきり/箇条書き）。既定はエンジン別（スタンダード=ON / 即時入力・ローカル=OFF） |
| API キー | 中央 Keychain（service = 変数名 / account = `shared`）から直読み。**ログイン・課金・アクティベーションキーは無し**（サーバー往復ゼロ＝最速） |
| personal 限定機能 | ライブ字幕 / ローカル（Apple）文字起こし / 翻訳して入力（いずれも Mac・macOS 26+） |

共通: **VAD・長文分割・ストリーミング・録音 HUD は常時 ON 固定**（設定 UI から撤去済み。Mac は `ConfigStore` で true 固定、Windows は `config_manager._force_always_on` が読込・保存時に矯正）。

製品版（顧客配布・課金）の運用は終了した。販売まわりのリポジトリ（`voicekey-site` / `voicekey-releases`）は
別リポジトリとして現状維持で、本リポジトリからは切り離す。経緯は `CLAUDE.md` 冒頭「単一ブランチ運用」。

## 4. 機能一覧（両 OS 対応）

| 機能 | 概要 | Mac | Win |
|---|---|:--:|:--:|
| 初回セットアップガイド | 初回起動時にオンボーディングを自動表示。権限・ログインの後ろに**体験ステップ**（例文を実際に声で入力→成功で褒める）を置いた体験型・**左＝説明／右＝画面イメージの 2 ペインに全面再デザイン**。**Mac**＝本体ウィンドウ内の全画面（ようこそ＋権限3〔1つずつ〕＋ログイン＋動作確認＋**マイクテスト**＋**録音キーテスト**＋体験3＋まとめ・完了でホームへ着地）／**Win**＝ようこそ＋ログイン＋**マイクテスト**＋**録音キーテスト**＋体験3＋まとめ（権限ステップ無し）。両 OS とも機能ツアーは実テストに置換。体験は「スキップ」で任意。既存ユーザーには出さない・完了フラグで再表示抑止（Mac はホームの「セットアップガイド」カード／Win は設定「一般」の「セットアップガイドを開く」から再表示可・メニュー/トレイの再表示口は撤去）（詳細は CHANGELOG） | ✅ | ✅ |
| デュアルホットキー | 2 スロットを独立設定（キー/モード/バックエンド） | ✅ | ✅ |
| hold / toggle モード | 押している間 / トグルで録音 | ✅ | ✅ |
| ハンズフリー録音 | 切替キーで開始・停止（録音中は HUD 表示） | ✅ | ✅ |
| ダブルタップ Enter | ホットキーを素早く 2 回 → 貼り付け後に Enter（送信） | ✅ | ✅ |
| テキスト整形 | 文字起こし後に Groq で整文（§3 参照・トグルで ON/OFF） | ✅ | ✅ |
| ユーザー辞書（確定置換） | 貼り付け直前に from→to を機械置換（「ユーザー辞書」タブで編集・部分一致・ローカル処理＝遅延ゼロ） | ✅ | ✅ |
| 数字変換 v2 | 貼付直前に漢数字→半角（全角半角化・裸数字列・位取り〔千二百三十四→1234〕・助数詞つき単独漢数字〔三時→3時〕）。決定的・純関数・冪等（LLM 不使用）。設定「数字入力」で 2 トグル＋保護リスト編集（誤変換を防ぐ語・先頭アンカー照合） | ✅ | ✅ |
| VAD / 長文分割 / ストリーミング / 録音 HUD | 常時 ON 固定（§3） | ✅ | ✅ |
| マイク自動検出 | 入力レベルの分散で使用マイクを推定 | ✅ | ✅ |
| 履歴 | 直近の文字起こし結果を保持（Mac は 200 件＋貼付先アプリ・日時のメタデータ、検索付き） | ✅ | ✅ |
| ホーム画面 / ダッシュボード | 統計 3 カード（累計入力／節約できた時間／この期間）＋最近の履歴のダッシュボード。**Mac** は起動・再オープンで開くメインウィンドウのホーム面（アプリ別使用率・マイクテスト・ガイド再表示・サイドバーで同一ウィンドウ設定切替つき）／**Win** は設定ウィンドウ先頭の「ホーム」ページ（アプリ別使用率は履歴のアプリ情報なしのため非対応）。詳細は CHANGELOG | ✅ | ✅ |
| サイドノッチ | 画面左端の履歴スリット → クリックで検索付き履歴パネル（黒基調・録音中はアクセント点灯・フォーカス非奪取・既定 ON。検索は Mac＝テキスト/アプリ名・Win＝テキスト） | ✅ | ✅ |
| 操作音 / メディアダッキング | 録音開始/停止のブリップ音（同一周波数で合成）・録音中はメディア音量を自動で下げる（既定 ON。Win ダッキングは Core Audio/pycaw・非対応時は無害スキップ） | ✅ | ✅ |
| 最後の入力を再貼り付け | グローバルキー（既定 ⌃⌘V）で直前の入力を再貼り付け | ✅ | ❌ |
| 使用実績（統計＋チャート＋レベル） | レベル/経験値・推定節約時間・連続利用日数に加え、今日/今週/累計の入力量と期間切替（週/月/年）の棒グラフを表示（**Mac はホーム画面**／Win は「実績」タブ・カウントアップ／棒伸びアニメ・集計は貼付後の処理＝遅延ゼロ）。Mac はアプリ別使用状況も集計。実績はこの端末のローカル集計（personal はログイン・アカウント連携なし） | ✅ | ✅ |
| 自動更新 | Mac=Sparkle / Win=version.json フィード（バックグラウンドでサイレント自動確認・手動確認は「バージョン情報」タブのみ。**Mac は新版検知時にホーム左上へ「更新する」ピル**） | ✅ | ✅ |
| ログイン起動 | OS のログイン時に自動起動 | ✅ | ✅ |
| ライブ字幕（personal のみ・macOS 26+） | 再生中のシステム音声を認識 → **翻訳（英→日）／文字起こし（日本語・英語）の 2 モード** → 録音ピルの真上に固定したガラス字幕（ピルが上へ育つ見た目・音声入力中は自動で隠れる）。最前面アプリの音だけが既定・翻訳は Apple/Gemini/Groq・⌥⌘S で開始停止・起動時の自動開始は既定 OFF（統合元は旧 subglass。詳細は CHANGELOG） | ✅ | ❌ |
| 議事録（文字起こしの自動保存・personal のみ・macOS 26+） | 字幕が確定した文字起こしを `~/Documents/voicekey/transcripts/*.md` へ時刻つきで追記（訳文は保存しない・5 分以上あくと別ファイル） | ✅ | ❌ |
| Google Meet 議事録ボット（personal のみ・要 Chrome） | 会議 URL を渡すと専用プロファイルの Chrome を裏で起動して参加し、Meet の字幕を**話者名つき**で議事録へ保存（マイク・カメラは切って参加。Mac の音声経路には触らない） | ✅ | ❌ |
| ローカル文字起こし「ローカル（Apple）」（personal のみ・macOS 26+） | 文字起こしを Apple のオンデバイス音声認識（SpeechAnalyzer）で Mac の中だけで行う。通信・API キーなし＝オフライン可。録音中から並行認識し離した瞬間に確定（実測 92〜155ms）。認識言語は「言語」設定に従い、初回だけモデル DL（進捗は録音 HUD） | ✅ | ❌ |
| 翻訳して入力（personal のみ・macOS 26+） | 文字起こしの最終テキストを訳してから貼り付ける（本命は「日本語で話す→英語が入力される」）。**全体で 1 トグル**・出力言語は 英/中/韓/西/日 から選択・エンジンは Apple オンデバイス（既定）/ Groq の 2 択（Gemini は出さない）。貼り付け直前に 1 回だけ適用し、失敗時は原文フォールバック | ✅ | ❌ |

## 5. アーキ地図（責務 → ファイル）

主要な責務ごとに Mac / Windows のどのファイルを見ればいいか。

| 責務 | Mac (`macos/Sources/Voicekey/`) | Windows (`src/`) |
|---|---|---|
| 起動エントリ | `VoicekeyApp.swift`（AppKit, NSStatusBar） | `main.py` |
| 中央コントローラ | `AppController.swift` | `app.py`（`VoicekeyApp` / `HotkeySlot`） |
| 設定モデル・既定値 | `Config/AppConfig.swift`（`ConfigStore`） | `config/config_manager.py` / `constants.py` / `types.py` |
| ホットキー検出 | `Core/HotkeyMonitor.swift` / `KeyToken.swift` | `app.py` 内 pynput リスナ / `platform/common/keymap.py` |
| 音声録音 | `Core/AudioRecorder.swift` / `AudioDevices.swift` | `core/audio_recorder.py` / `audio_preprocess.py` / `audio_utils.py` |
| 文字起こし（REST） | `Core/Transcriber.swift` | `core/api_transcriber.py` |
| 文字起こし（ストリーミング） | `Core/StreamingTranscriber.swift` | `core/streaming_transcriber.py` |
| テキスト整形（Groq）/ 後処理 | `Core/TextFormatter.swift` | `core/text_formatter.py` / `text_processor.py` / `text_utils.py` |
| 文字入力（貼り付け） | `Core/Paster.swift` | `core/input_handler.py` |
| VAD / マイク自動検出 / 履歴 | `Core/VoiceActivity.swift` / `MicAutoDetector.swift` / `HistoryStore.swift` | `core/vad.py` / `mic_auto_detect.py` / `history.py` |
| 使用実績（統計・レベル） | `Core/StatsStore.swift` | `core/stats.py` |
| 設定画面 UI | `UI/SettingsView.swift` / `HotkeyRecorderView.swift` / `Glass.swift`（ガラス様式の共通部品） | `ui/settings_window.py` / `styles.py` |
| ホーム画面 / ダッシュボード | `UI/HomeView.swift` / `MainWindowView.swift`（ホーム＋設定を 1 ウィンドウで切替・Mac） | `ui/settings_window.py`（先頭「ホーム」ページ） |
| サイドノッチ（履歴スリット） | `UI/SideNotch.swift`（スリット＋検索付き履歴パネル） | `ui/side_notch.py` |
| 操作音 / メディアダッキング / フルスクリーン判定 | `Core/SoundFX.swift` / `MediaDucker.swift` / `FrontAppTracker.swift`（Mac） | `core/sound_fx.py` / `media_ducker.py` / `fullscreen.py`、実ブラーは `platform/windows/acrylic.py` |
| 初回セットアップ（オンボーディング） | `UI/OnboardingView.swift`（起動分岐は `VoicekeyApp.swift`） | `ui/onboarding_window.py`（表示配線は `app.py`） |
| HUD / トレイ | `UI/Hud.swift` / `VoicekeyApp.swift`（メニューバー） | `ui/hud.py` / `system_tray.py` |
| API キー保管 | `Core/Keychain.swift` / `Config/EmbeddedKeys.generated.swift` | `utils/secrets.py` / `.env` |
| 自動更新 | `Core/UpdaterController.swift`（Sparkle） | `utils/updater.py` |
| ライブ字幕（personal のみ） | `Caption/`（`CaptionService` / `Audio` システム音声タップ / `Speech` 認識・読み上げ / `Translation` Apple・Gemini・Groq / `Transcript` 議事録の保存 / `Pipeline` / `UI` 字幕 HUD・メニュー・設定タブ / `CLI` 検証ハーネス） | — （Mac 専用） |
| Meet 議事録ボット（personal のみ） | `Caption/MeetBot/`（`ChromeDevTools` CDP クライアント / `MeetBotService` 参加・字幕ポーリング / `MeetBotScripts` **Meet の DOM 依存はここだけ** / `MeetBotMenu` / `MeetBotTestRunner` 疎通ハーネス） | — （Mac 専用） |
| ローカル文字起こし（personal のみ） | `Core/LocalSpeechTranscriber.swift`（`LiveTranscribing` 実装・認識器は `Caption/Speech/SpeechRecognizer` を共用） / `Core/Transcriber.swift`（1 発フォールバック） / `CLI/DictationTestMode.swift`（検証ハーネス） | — （Mac 専用） |
| 翻訳して入力（personal のみ） | `Core/DictationTranslator.swift`（設定 `DictationTranslation` ＋ Apple/Groq 翻訳） / `AppController.swift`（貼り付け直前の 1 回適用） / `UI/SettingsView.swift`（「翻訳して入力」タブ） | — （Mac 専用） |
| OS 権限 | `AppController.swift`（マイク/入力監視/アクセシビリティ） | — （Windows は OS ゲートなし） |
| OS 抽象化 / ログイン起動 / ログ | ネイティブ API 直 | `platform/` / `utils/autostart.py` / `utils/logger.py` |

## 6. 配布構成（要点のみ・詳細は HANDOFF.md）

- **Mac**: DMG・自動更新フィード（Sparkle appcast）とも Vercel サイト `voicekey.vercel.app`（ソース `/Users/tomato/Project/voicekey-site/`、`vercel deploy --prod`）。
- **Windows**: インストーラ（約 270MB）は公開バイナリ専用リポ `voicekey-releases` の GitHub Releases。更新フィード `version.json` / `downloads.json` は Vercel。
- 配布版は**ログイン必須**。**v1.5.0〜はログインで無料体験 200 回**（文字起こし 1 回＝1 消費・累計一度きり）、使い切ると**アクティベーションキー必須**（課金は未実装＝当面はキー登録のみが解放手段）。文字起こしは自社サーバー（`voicekey.vercel.app`）が利用権と無料体験の残量を検証（`entitlements.free_quota/free_used`・残枠ゼロは 402）し、Deepgram=短命トークン直叩き / ElevenLabs・Groq=サーバープロキシで処理（埋め込みキーは撤去済み＝製品版は完全サーバー経由）。無料枠の消費は録音開始のクリティカルパスから外して録音成立後に確定する（`/api/v1/usage/confirm`）＝有料と同じく録音開始のサーバー往復ゼロ。旧版（v1.2.0 以前）は埋め込み済みの提供元キーが漏洩済みのためローテーションで順次無効化。**ソース非公開**。
- リリースは常に Mac/Windows 両 OS 同期。版番号は semver で Claude が決める。手順の全文は `HANDOFF.md`「リリース手順」。

## 7. ドキュメント体系（どれを見ればいいか）

| ファイル | 役割 | 読むタイミング |
|---|---|---|
| **OVERVIEW.md**（これ） | 全体地図（機能・アーキ・ブランチ・配布の索引） | まず全体像を掴みたいとき |
| `README.md` | ユーザー向け（特徴・インストール・使い方・設定・配布ステータス） | 使い方・対外説明 |
| `HANDOFF.md` | 作業の現在地・恒久要件・フェーズ・リリース手順 | 長期作業の再開時（最初に読む） |
| `CHANGELOG.md` | 変更履歴（時系列） | 「いつ何を変えたか」 |
| `CLAUDE.md` | AI 向け開発ルール（ブランチ運用・両 OS 同時実装・README 更新等） | 実装に入る前 |
| `AGENTS.md` | 他 AI エージェント向けの同趣旨ルール（簡約版） | 同上 |
| `CONTRIBUTING.md` | コミット規約・バージョニング | コミット時 |
| `docs/BUILD_WINDOWS.md` | Windows ビルド/実機チェックリスト | Windows 配布時 |

## 8. 更新ルール（重要・README 更新ルールと同じ精神）

**機能・アーキ・ブランチ仕様・配布構成・ドキュメント構成が変わったら、同じコミットで OVERVIEW.md の該当行も直す。**
- 上の表（機能一覧 §4 / アーキ地図 §5 / ブランチ運用 §3）が現状とズレないように保つ。
- ただし詳細は書かない（重複はドリフトの元）。詳細は各専門ドキュメントに置き、ここはリンクと 1 行要約に留める。
- ブランチは 1 本なので、personal の内容が唯一の正本（両 OS 同時実装のルールは維持）。
