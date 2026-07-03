# OVERVIEW — voicekey 全体地図

プロジェクトが大きくなっても全体像を一目で掴むための「地図」。
**最初にここを読めば、何がどこにあるか・どのドキュメントを見ればいいかが分かる**ようにしてある。
詳細は重複させず、各専門ドキュメント（README / CHANGELOG / HANDOFF / CLAUDE.md）へのリンクで示す。

> このファイルは `main` / `release` 両ブランチに置き、**内容は同一**（両ブランチの違いも本文内に書く）。
> 片方だけ更新してドリフトさせない。機能・アーキを変えたら同じコミットで該当行を直す（末尾「更新ルール」）。

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

## 3. 2 ブランチの違い（main = 自分用 / release = 製品版）

**この 2 ブランチは絶対に混ぜない。** どちらに変更を入れるかは Claude が内容から判断する（原則 release 実装 → main 移植。2026-07-03 改訂・詳細は CLAUDE.md）。

| | `main`（自分用） | `release`（製品版・配布タグ `v*` はこちら） |
|---|---|---|
| 文字起こしの選択肢 | 4 プロバイダーを**実名表示**（OpenAI / Groq / ElevenLabs / Deepgram） | **2 モード**：リアルタイム（Deepgram nova-3・短命トークン直叩き＋ストリーミング）/ スタンダード（Groq whisper-large-v3-turbo・プロキシ経由。**ハンズフリー録音時は内部で ElevenLabs scribe_v1 に自動切替**） |
| モデル選択 | あり（フルコントロール） | なし（推奨モデル固定） |
| テキスト整形（Groq） | トグルあり・既定 OFF・モデル/プロンプト選択可 | **モデルはサーバー固定・UI は ON/OFF トグル＋整え方プリセット 4 種**（標準/そのまま/すっきり/箇条書き・自由プロンプト入力なし。全プリセット「発言の削除・要約禁止＋適切な句読点」共通で、`preset_id` としてサーバーへ送る）。既定は**モード別**（スタンダード=ON / リアルタイム=OFF、バックエンド切替で追従）。スタンダードは**サーバーで STT と統合実行**（単発送信は `format=1` の 1 リクエスト、分割送信は結合後にクライアント整形） |
| API キータブ | 表示 | 配布ビルドは非表示。**ログイン必須**。ログインで**無料体験 200 回**、使い切るとアクティベーションキー必須（無料体験は v1.5.0〜・キー必須は v1.3.0〜・埋め込みキーは撤去済み＝完全サーバー経由） |

両ブランチ共通: **VAD・長文分割・ストリーミング・録音 HUD は常時 ON 固定**（設定 UI から撤去済み。Mac は `ConfigStore` で true 固定、Windows は `config_manager._force_always_on` が読込・保存時に矯正）。

仕様の根拠と経緯は `CLAUDE.md` 冒頭「2 ブランチ運用」とメモリ `project_voicekey_branch_split`。

## 4. 機能一覧（両 OS 対応）

| 機能 | 概要 | Mac | Win |
|---|---|:--:|:--:|
| 初回セットアップガイド | 初回起動時にオンボーディングを自動表示（Mac=6 ステップ・権限を順に案内／Win=3 ステップ）。既存ユーザーには出さない・完了フラグで再表示抑止（詳細は CHANGELOG） | ✅ | ✅ |
| デュアルホットキー | 2 スロットを独立設定（キー/モード/バックエンド） | ✅ | ✅ |
| hold / toggle モード | 押している間 / トグルで録音 | ✅ | ✅ |
| ハンズフリー録音 | 切替キーで開始・停止（録音中は HUD 表示） | ✅ | ✅ |
| ダブルタップ Enter | ホットキーを素早く 2 回 → 貼り付け後に Enter（送信） | ✅ | ✅ |
| テキスト整形 | 文字起こし後に Groq で整文（§3 参照・トグルで ON/OFF） | ✅ | ✅ |
| ユーザー辞書（確定置換） | 貼り付け直前に from→to を機械置換（「ユーザー辞書」タブで編集・部分一致・ローカル処理＝遅延ゼロ） | ✅ | ✅ |
| VAD / 長文分割 / ストリーミング / 録音 HUD | 常時 ON 固定（§3） | ✅ | ✅ |
| マイク自動検出 | 入力レベルの分散で使用マイクを推定 | ✅ | ✅ |
| 履歴 | 直近の文字起こし結果を保持（Mac は 200 件＋貼付先アプリ・日時のメタデータ、検索付き） | ✅ | ✅ |
| ホーム画面 / メインウィンドウ統合 | 起動・再オープンで開くダッシュボード（統計・アプリ別使用率・履歴）。サイドバーの「設定」で同一ウィンドウのまま設定へ切替（Mac のみ・詳細は CHANGELOG） | ✅ | ❌ |
| サイドノッチ | 画面左端の履歴スリット → クリックで検索付き履歴パネル（黒基調・クリック透過防止・既定 ON） | ✅ | ❌ |
| 操作音 / メディアダッキング | 録音開始/停止のブリップ音・録音中はメディア音量を自動で下げる（既定 ON） | ✅ | ❌ |
| 最後の入力を再貼り付け | グローバルキー（既定 ⌃⌘V）で直前の入力を再貼り付け | ✅ | ❌ |
| 使用実績（統計＋チャート＋レベル） | レベル/経験値・推定節約時間・連続利用日数に加え、今日/今週/累計の入力量と期間切替（週/月/年）の棒グラフを表示（**Mac はホーム画面**／Win は「実績」タブ・カウントアップ／棒伸びアニメ・集計は貼付後の処理＝遅延ゼロ）。Mac はアプリ別使用状況も集計。**ログイン中はアカウントに紐付き複数端末で合算・再インストール後も引き継ぎ**（`usage_stats`／release 製品版・未ログインはローカルのみ） | ✅ | ✅ |
| 自動更新 | Mac=Sparkle / Win=version.json フィード（バックグラウンドでサイレント自動確認・手動確認は「バージョン情報」タブのみ。**Mac は新版検知時にホーム左上へ「更新する」ピル**） | ✅ | ✅ |
| ログイン起動 | OS のログイン時に自動起動 | ✅ | ✅ |

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
| ホーム画面 / メインウィンドウ統合 | `UI/HomeView.swift` / `MainWindowView.swift`（ホーム＋設定を 1 ウィンドウで切替・Mac のみ） | — |
| サイドノッチ（履歴スリット） | `UI/SideNotch.swift`（スリット＋検索付き履歴パネル・Mac のみ） | — |
| 操作音 / メディアダッキング / 前面アプリ追跡 | `Core/SoundFX.swift` / `MediaDucker.swift` / `FrontAppTracker.swift`（Mac のみ） | — |
| 初回セットアップ（オンボーディング） | `UI/OnboardingView.swift`（起動分岐は `VoicekeyApp.swift`） | `ui/onboarding_window.py`（表示配線は `app.py`） |
| HUD / トレイ | `UI/Hud.swift` / `VoicekeyApp.swift`（メニューバー） | `ui/hud.py` / `system_tray.py` |
| API キー保管 | `Core/Keychain.swift` / `Config/EmbeddedKeys.generated.swift` | `utils/secrets.py` / `.env` |
| 自動更新 | `Core/UpdaterController.swift`（Sparkle） | `utils/updater.py` |
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
- 上の表（機能一覧 §4 / アーキ地図 §5 / ブランチ差 §3）が現状とズレないように保つ。
- ただし詳細は書かない（重複はドリフトの元）。詳細は各専門ドキュメントに置き、ここはリンクと 1 行要約に留める。
- `main` / `release` 両ブランチで同一内容を保つ（両 OS 同時実装と同じ要領で両ブランチ同時に直す）。
