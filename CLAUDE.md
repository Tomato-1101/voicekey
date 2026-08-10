# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**全体像（機能・アーキ・ブランチ・配布の地図）は `OVERVIEW.md` を読む**（プロジェクトが大きくなっても索引から辿れる）。
**進行中の長期作業がある場合はまず `HANDOFF.md` を読む**（ベータ配布計画の現在地・恒久要件が書いてある）。

## 最重要: 2 ブランチ運用（main=自分用 / release=製品版）— 絶対に混ぜない（2026-06-17 ユーザー指示）

voicekey は **2 ブランチに分離**して運用する。**この 2 ブランチは絶対に混ざらないように扱う**。

- **`main` = 自分用**: 開発者が自分の API キーで毎日使う版（既定ブランチ）。
  - 4 プロバイダーすべてを**実プロバイダー名で表示**（OpenAI / Groq / ElevenLabs / Deepgram）＋**モデル名も表示・選択可**。テキスト整形（Groq）はモデル・プロンプトともフル設定可。API キータブ表示。
- **`release` = 製品版**: 顧客配布版（配布タグ `v*` はこのブランチで打つ）。
  - 文字起こしは **「リアルタイム」=Deepgram / 「スタンダード」=Groq の 2 択のみ**（OpenAI は開発用のみ・ElevenLabs はスタンダードのハンズフリー録音時に内部で自動使用）。
  - **モデル非選択**（Deepgram=nova-3 / Groq=whisper-large-v3-turbo / EL=scribe_v1 固定）。**Groq 整形はサーバー実行**（モデルはサーバー固定・自由プロンプト入力なし・オンオフトグル＋**整え方プリセット 4 種**〔標準/そのまま/すっきり/箇条書き・2026-07-03 追加〕・既定はモード別＝スタンダード ON / リアルタイム OFF）。配布ビルドは API キータブ非表示＋埋め込みキー。

**ブランチ運用ルール（厳守）:**
- **どのブランチに変更を入れるかは Claude が内容から判断する**（2026-07-03 ユーザー指示で「毎回確認」を廃止）。
  目安: 製品機能・UI・バグ修正は原則両方（release で実装 → main へ移植）。ブランチ固有仕様（release の認証層、main のモデル選択 UI 等）に関わるものは該当ブランチのみ。判断に迷う特殊ケースだけ確認してよい。
- 2 ブランチを勝手に荒らさない・混ぜない。一方の変更を勝手にもう一方へ持ち込まない（指示なき cherry-pick / merge 禁止）。
- 両 OS 同時実装（下節）は**同一ブランチ内**で行う。リリース配布も対象ブランチ（release）を取り違えない。

## 最重要: コードを変更したら README も更新する（2026-06-14 ユーザー指示）

`README.md` は**ユーザー向けのドキュメント**。機能・使い方・設定項目・対応プラットフォーム・配布状態など、
**ユーザーから見える挙動を変える変更を入れたら、同じ作業の中で README の該当箇所も最新に更新する**。
- 機能追加／変更 → 該当する説明・使い方の節を直す。設定項目が増減 → 設定方法の節を直す。
- バージョンやリリース状態が変わった → 冒頭「🚦 配布ステータス」表と最終更新日を直す。
- README 更新はコード変更と同じ 1 コミットにまとめる。「あとでまとめて」は禁止。
- 同じ趣旨を `AGENTS.md` にも記載（他の AI エージェントでも守らせるため）。

## 最重要: 機能・アーキを変えたら OVERVIEW.md も更新する（2026-06-18 ユーザー指示）

`OVERVIEW.md` は**プロジェクト全体の地図**（機能一覧・アーキ地図・2 ブランチの違い・配布構成・ドキュメント体系の索引）。
プロジェクトが大きくなっても全体像を見失わないために置いている。
- **機能・アーキ・ブランチ仕様・配布構成・ドキュメント構成が変わったら、同じコミットで `OVERVIEW.md` の該当行も直す**（README 更新ルールと同じ精神）。
- ただし詳細は書かない（重複はドリフトの元）。詳細は各専門ドキュメント（README/CHANGELOG/HANDOFF）に置き、OVERVIEW はリンクと 1 行要約に留める。
- `OVERVIEW.md` は **`main` / `release` 両ブランチで同一内容**を保つ（ブランチ差は本文内に明記）。両ブランチ同時に直す。

## 最重要: 両OSに存在する変更は Mac・Windows 同時に実装する（2026-06-16 ユーザー指示）

UI の文言・表示名・設定項目・機能挙動など、**ユーザーから見て両 OS に同等に存在する要素を変えるときは、
Mac（`macos/` Swift）と Windows（`src/` Python）の両方を同じ作業で反映し、1 コミットにまとめる**。片方だけ変えない。
- Mac はビルド＆再起動で、Windows は `py_compile`＋`unittest` で各々検証してから報告。
- Windows 固有（win32・レジストリ自動起動）/ Mac 固有（launchd・SMAppService・CGEventTap）でしか存在しない要素だけ片方で完結してよい。
- リリース配布も常に両 OS 同期（バージョンは Claude が semver で決める。範囲・版番号はユーザーに聞かない）。
- バックエンド表示名は**ブランチで異なる**（上節）。`main`=実プロバイダー名、`release`=特徴名 2 択（高速リアルタイム / 正確性）。
- **VAD・長文分割・ストリーミング・録音 HUD は両ブランチとも常時 ON 固定**（設定 UI から撤去済み）。Mac は `ConfigStore` init で true 固定、Windows は `config_manager._force_always_on` が読込・保存時に矯正する。

## 最重要: Mac 版（macos/ ディレクトリ・Swift）の作業ルール

このリポジトリには Windows 版（下記 Project Overview、Python/PySide6）に加えて
**Mac 版（`macos/`、Swift製メニューバーアプリ）** がある。Mac 版を触るときは以下を厳守:

- `macos/Sources/` を変更したら、**報告前に必ずワンセットで実行する**（ユーザーのビルド忘れ防止・2026-06-10 指示）:
  1. `cd macos && ./scripts/build_app.sh`
  2. `pgrep -x voicekey` で旧プロセスを kill
  3. `open macos/dist/voicekey.app` で再起動
  4. ログ / スクリーンショットで動作確認
- 「コミットした＝反映された」ではない。ビルド+再起動するまで挙動は変わらない（詳細は `deploy-verify` スキル）。
- メニューバーアイコンが見えない時: `defaults read com.voicekey.app "NSStatusItem Preferred Position Item-0"` を確認。
  値が 490 超だと Hidden Bar / ノッチ下で不可視 → 250 程度に再設定して再起動。
- UI インジケーターは「動作速度 > 見た目」。音声入力に待ち時間を足す実装（close() のタイムアウト待ち等）は禁止。

## ライブ字幕（personal ブランチ・Mac・macOS 26 以降）

旧 subglass を voicekey に統合した機能（2026-08-10 ユーザー決定「subglass を voicekey に完全統合し、
最終的に subglass を廃止」のフェーズ 1）。**コードは `macos/Sources/Voicekey/Caption/` に隔離**し、
全型を `@available(macOS 26.0, *)` でゲートする（`Package.swift` の `.macOS(.v14)` は上げない）。

- **ディクテーションのクリティカルパスに 1ms も足さない**: `AppController.caption` は遅延生成。
  字幕を開始するまで何も初期化されない。既存の録音〜貼り付け経路に手を入れない。
- **恒久要件（subglass から引き継ぎ・変更しない）**:
  - タップは自プロセス除外（読み上げ音声を拾い直す TTS ループ禁止）。
  - **確定文は全部訳す**。ロール表示（確定 2 行＋ライブ行）で `max(3秒, 文字数×0.15秒)` の
    最低表示時間を保証する。鮮度優先で捨ててよいのは**ライブ行（部分訳）だけ**。
  - **クラウド（Gemini / Groq）へ送るのは確定文だけ**（高頻度呼び出し禁止）。429/失敗時は
    Apple へフォールバック・**既定エンジンは Apple のまま**。
    ライブ行（部分訳）は、クラウド選択時も **Apple のオンデバイス翻訳**で出す
    （2026-08-10・無料かつローカルなので上の要件に抵触しない。未導入なら原文だけ）。
    このため**クラウド選択時も共有 `AppleTranslator` を `prepareIfInstalled()` で用意する**
    ＝ 429 のときのフォールバック先が実際に効く前提でもある（未準備だと落とし先ごと失敗する）。
  - 読み上げは確定訳のみ・既定 OFF。既定のキャプチャ対象は「最前面のアプリだけ」。
  - **エンジンの切替はユーザー操作のみ**（2026-08-11 Tomato 指示）。特に**課金の Gemini へ
    Claude が勝手に切り替えない**（ベンチ最良でも不可。無料 = Apple / Groq を既定運用とし、
    Groq の 429 は Apple フォールバックで吸収する）。
- **区切りポリシー（2026-08-10 ユーザー指摘「一文が長すぎて翻訳されるまで時間がかかりすぎる」）**:
  調整値は `TranslationCoordinator` の先頭に「なぜその値か」付きで集約する。
  上限 **48 字** / 節区切りを探し始める長さ **32 字** / 無音での強制送出 **0.45 秒**。
  32 字を超えたらカンマ・接続詞（`" and " / " but " / " so " / " because "` 等）の**直後**で送出する。
  **判定順は「長さ → 文末 → 節区切り」**（この順序が肝）。音声認識は 40 語超の 1 文をまるごと
  1 件の確定として渡してくる（実測 199 字）ので、文末判定を先に置くと刻みが一切効かない。
  刻んでも 1 文字も落とさない（`[COVERAGE] status=ok` が回帰判定）。
  回帰テストは `TranslationBreakPolicyTests`。
- **表示位置はピル固定（2026-08-10 ユーザー指示）**: 字幕は voicekey ピルと同じ場所
  （画面下辺中央・`visibleFrame.minY + 8` にパネル下辺）に固定し、行が増えたら**上へ伸びる**。
  ドラッグ移動・位置記憶は持たせない（「ピルが字幕に大きくなっていく感じ」）。
  1 行のときは角丸＝高さの半分でピルと同じカプセル形。透明度は字幕 0.62 / ピル 0.7 で**別のまま**。
  `Hud.swift` のパネル内部には手を入れない（字幕は別 NSPanel のまま・重なりと整列で一体に見せる）。
- **音声入力中は字幕を隠す**: HUD が録音・変換中・通知の間だけ非表示。認識・翻訳は止めない。
  配線は `HudModel.onModeChanged` → `AppController.applyCaptionVisibility` →
  `CaptionService.setDictationActive` の一方向のみ（字幕未生成なら何もしない）。
- **設定の置き場所は設定 UI に集約（2026-08-10 ユーザー指示）**: 字幕の設定項目を増やすときは
  **設定ウィンドウの「ライブ字幕」タブ（`Caption/UI/CaptionSettingsTab.swift`・タブ id=9）**に足す。
  メニューバーのサブメニューは残してよいが、そちらだけにしない。値は `ConfigStore` の
  `@Published` ミラー経由（正本は `CaptionSettings`）。
- **API キー**: 環境変数 → 中央 Keychain（service = 変数名 / account = `shared`・`/usr/bin/security` を
  子プロセスで読む）。**書き込みはしない**（voicekey 本体の Keychain 項目に触らない）。値はログ・UI に出さない。
  設定 UI には**状態だけ**を出す（入力欄は置かない）。`APIKeyStore.load` は子プロセスを起動するので
  SwiftUI の body から呼ばない（`onAppear` で一度だけ）。
- **権限（TCC）**: システムオーディオ収録の許可は「字幕を開始」操作のときだけ発火させる。
  初回起動時は自動開始しない（`captionEverStarted`）＝ voicekey の初回権限プロンプト直列化を壊さない。
- **App Nap**: 字幕動作中は `ProcessInfo.beginActivity(.background)` を張る（行の自主退場・
  無音フェードのタイマーが nap で沈黙する実事故があるため）。
- **HAL を絶対にループで叩かない（2026-08-10 の実事故・最重要）**: Process Tap と Aggregate Device の
  作成・破棄は Mac 全体の既定デバイス再評価を誘発する。ここをループさせると **coreaudiod が詰まり、
  voicekey 本体のマイクを含め Mac の全プロセスでオーディオが死ぬ**（復旧に `killall coreaudiod` が必要）。
  - **「無音＝壊れている」と判定しない**。タップは対象が黙っている間フレームを 1 つも配らない
    （グローバルタップでも同じ。当初の想定と逆で、実測で確認済み）。作り直す前に必ず
    `isAnyTargetEmitting()` で「本当に誰かが鳴らしているか」を見る。
  - **あらゆる再試行・作り直しに上限を置く**（黙死 3 回／構築失敗 5 回・指数バックオフ）。
  - 既定出力デバイスの変更通知は、実際にデバイス ID が変わったときだけ張り替える。
  - 回帰は `--caption-mic-coexist-test`（`[VERDICT] ... rebuilds=` が 0 でなければ FAIL）。
- **画素判定ハーネス**: `VOICEKEY_CAPTION_DISABLE=1` で字幕を丸ごと無効化できる。
  `scripts/dev/fullscreen_helper.swift` を使う計測では必ずこの env を付けて voicekey を起動する。
- **検証**: `macos/scripts/dev/caption_e2e.sh`（`VOICEKEY_CAPTION_TRANSLATOR=mock` で外部通信なし版も）/
  `caption_tts_loop.sh` / `caption_scope.sh` / `caption_bench.sh`（**課金あり・手動のみ**）/
  `--caption-mic-coexist-test`（マイクとの共存）/ `--caption-hud-test`（ピル固定と音声入力中の非表示）。
  後者 2 つは `open -n dist/voicekey.app --args <mode> --log-file <path>` で起動し、
  `[VERDICT] status=ok` を確認する（`--caption-hud-test` は `[PHASE-START]` を出すので、
  それを待ってから `screencapture` すると各状態のスクショが撮れる）。

## Project Overview

voicekey は、ホットキーを押している間だけ音声を録音し、文字起こし結果を**今使っているアプリのカーソル位置へ自動入力**する常駐型の音声入力ツール（Mac=メニューバー / Windows=タスクトレイ）。
**文字起こしはすべてクラウド API**（Deepgram / ElevenLabs / OpenAI / Groq）、**発話区間検出（VAD）だけローカル CPU 実行**（Python=Silero ONNX を onnxruntime、Mac=エネルギー RMS）。ローカル GPU 文字起こし（faster-whisper）は廃止済み＝**CUDA / GPU は不要**。

> 機能一覧・アーキ地図（責務 → ファイル）・2 ブランチの違い・配布構成は **`OVERVIEW.md`** に集約してある。ここでは重複させない（ドリフト防止）。Windows は `src/`（Python / PySide6）、Mac は `macos/Sources/Voicekey/`（Swift）の二本立て。

## Development Commands

### Windows（`src/`・Python）

```bash
# 開発実行
python run.py

# 配布ビルド（PyInstaller） → dist/voicekey/voicekey.exe
pyinstaller voicekey.spec --clean --noconfirm
```

### Mac（`macos/`・Swift）

```bash
cd macos && ./scripts/build_app.sh   # アプリをビルド（配布 DMG は build_dmg.sh）
swift test --package-path macos       # Swift ユニットテスト
```

### Setup（Windows・ソースから）

```bash
python -m venv venv
.\venv\Scripts\Activate.ps1   # Windows PowerShell
pip install -r requirements.txt
```

GPU / CUDA は不要（文字起こしはクラウド API、VAD は onnxruntime の CPU 実行）。`torch` / `torchaudio` は `silero-vad` の依存として入るだけで、実行時には使わない。

## Architecture（要点のみ・全体は OVERVIEW.md）

### 処理の流れ（Windows）

1. **デュアルホットキー検出**（`src/app.py`）: 2 つの独立ホットキーを同時監視（pynput）。
2. **音声録音**（`src/core/audio_recorder.py`）: sounddevice でマイク録音。`audio_preprocess.py` で音量正規化、`vad.py`（Silero ONNX / CPU）で無音圧縮・長文分割。
3. **文字起こし**: REST=`src/core/api_transcriber.py`（Deepgram / ElevenLabs / OpenAI / Groq）、低遅延ストリーミング=`src/core/streaming_transcriber.py`（Deepgram）。
4. **テキスト整形**（任意・`src/core/text_formatter.py`）: Groq の高速 LLM で 1 回だけ整形（失敗時は原文フォールバック）。
5. **テキスト入力**（`src/core/input_handler.py`）: クリップボード経由で前面アプリへ貼り付け、貼付後に原本を復元。
6. **UI 更新**: PySide6 のシグナル/スロットでスレッド安全に HUD（`src/ui/hud.py`）・システムトレイ（`src/ui/system_tray.py`）を更新。

### 中央コントローラ（`VoicekeyApp`・`src/app.py`）

- 2 スロット（`HotkeySlot`）を独立設定（ホットキー / モード hold・toggle / バックエンド / モデル・プロンプト）。`_hotkey_slots: Dict[int, HotkeySlot]`、`_active_slot` が録音中スロット。
- 録音開始時に設定をスナップショットして処理キューへ載せる（処理中の設定変更に影響されない）。
- 常駐デーモンスレッド: キーボードリスナ / 設定ホットリロード監視。

### 設定（`src/config/`）

- `config_manager.py` が `settings.yaml` を読み込み（凍結時は実行ファイルのディレクトリ）、旧フォーマットを自動マイグレーション、mtime 監視でホットリロード。保存はアトミック置換（一時ファイル → `os.replace`）。
- `constants.py` の `APP_VERSION` がバージョンの**単一ソース**（`src/__init__.__version__`・Windows ビルド・`updater.py` が参照）。
- **VAD / 長文分割 / ストリーミング / 録音 HUD は常時 ON 固定**（`_force_always_on` が読込・保存時に矯正）。

### release（製品版）固有のサーバー認証層

- `src/core/auth_client.py` / `backend_client.py` / `login_coordinator.py`: ログイン（deep link）・利用権／無料体験残量の検証・短命トークン／プロキシ取得を担う。`main`（自分用）には存在しない。

## コメントルール（重要）

**すべてのコードに日本語コメントを追加すること。**

### 必須コメント

1. **モジュールdocstring**: 各ファイルの先頭に目的を説明
   ```python
   """
   音声録音モジュール
   
   sounddeviceライブラリを使用してマイクから音声を録音する機能を提供する。
   """
   ```

2. **クラスdocstring**: クラスの役割と主要な属性を説明
   ```python
   class AudioRecorder:
       """
       音声録音を管理するクラス。
       
       Attributes:
           sample_rate: サンプリングレート（Hz）
       """
   ```

3. **メソッド/関数docstring**: Args, Returns, Raisesを明記
   ```python
   def start(self) -> bool:
       """
       録音を開始する。
       
       Returns:
           成功した場合True
       """
   ```

4. **インラインコメント**: 複雑なロジックや意図が不明確な箇所に追加
   ```python
   # float32 [-1.0, 1.0] から int16 に変換
   audio_int16 = (audio_data * 32767).astype(np.int16)
   ```

### コメントのスタイル

- 言語: **日本語**
- 簡潔かつ明確に
- 「何をするか」ではなく「なぜそうするか」を重視
- 明らかなコードには不要なコメントを追加しない

---

## Development Workflow

### 変更記録のルール（重要）

**すべてのコード変更は必ず CHANGELOG.md に記録すること。**

#### 記録のタイミング
- 機能追加、変更、修正を実装した後
- コミット前に変更内容をまとめる
- Pull Request 作成時

#### CHANGELOG.md の更新手順

1. **Unreleased セクションに追加**
   ```markdown
   ## [Unreleased] - YYYY-MM-DD

   ### Added
   - 新機能の説明

   ### Changed
   - 変更内容の説明

   ### Fixed
   - 修正内容の説明
   ```

2. **記録すべき内容**
   - **Added**: 新機能、新しいファイル、新しい設定
   - **Changed**: 既存機能の変更、リファクタリング
   - **Fixed**: バグ修正、エラー対応
   - **Technical Details**: 技術的な詳細（変更したクラス、メソッド等）

3. **記録例**
   ```markdown
   ### Added
   - デュアルホットキー機能の実装
     - 各ホットキーに異なるバックエンドを設定可能
     - APIバックエンドで異なるモデル選択をサポート

   ### Changed
   - app.py の大幅なリファクタリング
     - HotkeySlot データクラスを追加
     - _hotkey_slots 辞書で複数ホットキーを管理

   ### Technical Details
   - **types.py**: HotkeySlotConfig データクラスを追加
   - **constants.py**: DEFAULT_CONFIG を新構造に変更
   ```

#### コミットメッセージの規則

- 変更内容を簡潔に記載
- CHANGELOG.md の内容と一致させる
- フォーマット: `<type>: <description>`
  - `feat`: 新機能
  - `fix`: バグ修正
  - `refactor`: リファクタリング
  - `docs`: ドキュメント更新
  - `style`: コードスタイル変更

例:
```
feat: デュアルホットキー機能の実装

- 2つのホットキースロットを追加
- 各スロットで異なるバックエンドを選択可能
- 設定UIを2ホットキー対応に更新
```

### 自動コミットのルール（AI向け・重要）

**Claude Code は、ある程度コード実装が進んだら、自動的にコミットすること。**

**コミット完了後、通常ブランチへの push まで自動で行ってよい（main / master への直接 push と --force は禁止）。**

#### いつコミットすべきか

以下のタイミングで**自動的に**コミットを行う：

1. **機能実装が完了した時**
   - 新機能の実装が完了し、動作確認が取れた
   - 複数ファイルにわたる変更が完了した
   - 関連するテストが通過した

2. **バグ修正が完了した時**
   - バグの原因を特定し、修正コードを実装
   - 修正後の動作確認が完了

3. **ドキュメント更新が完了した時**
   - README.md, CHANGELOG.md, CLAUDE.md 等の更新
   - 複数ドキュメントの一貫した更新が完了

4. **リファクタリングが完了した時**
   - コードの整理・最適化が完了
   - 動作に影響がないことを確認

#### コミット前の必須チェック

自動コミット前に**必ず以下を実行**：

1. ✅ **CHANGELOG.md を更新**
   - Unreleased セクションに変更内容を記録
   - Added/Changed/Fixed カテゴリを適切に使用

2. ✅ **コミットメッセージを作成**
   - CONTRIBUTING.md の規約に従う
   - `<type>: <description>` 形式
   - Co-Authored-By: Claude Sonnet 4.5 を追加

3. ✅ **動作確認**
   - 明らかなエラーがないことを確認
   - ユーザーに動作確認を促す場合は、コミット前に確認

#### プッシュについて

- **通常ブランチ（feature ブランチ等）への push は自動 OK**
  コミット直後にそのまま `git push origin <branch>` まで実行する
- 以下は手動承認が必要（自動 push しない）：
  - `main` / `master` への直接 push
  - `--force` / `--force-with-lease`
  - secret を含む疑いのあるコミット
- push 後はコミット ID と remote の URL を簡潔に報告

#### 例外

以下の場合は、ユーザーに確認を取る：

- ⚠️ 破壊的変更がある場合
- ⚠️ 設定ファイル構造を変更する場合
- ⚠️ リリースタグを作成する場合

#### 実行例

```bash
# 1. CHANGELOG.md を更新（手動で完了済み）

# 2. ステージング
git add <変更されたファイル>

# 3. コミット
git commit -m "$(cat <<'EOF'
feat: Add Cerebras API backend support

- Implement CerebrasTranscriber class
- Add UI controls for Cerebras settings
- Update documentation

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>
EOF
)"

# 4. push（feature ブランチなら自動 OK、main / master は手動）
git push origin <branch>
```

#### ユーザーへの報告

コミット後は、以下を報告：

```
✅ 変更をコミット & push しました
- コミットID: abc1234
- ファイル: app.py, settings_window.py, CHANGELOG.md
- ブランチ: feature/xxx
- remote: origin/feature/xxx
```

main / master の場合は push せずに以下のように報告：

```
✅ 変更をコミットしました（push は手動で）
- コミットID: abc1234
- ファイル: app.py, settings_window.py, CHANGELOG.md
- ブランチ: main

main への push は手動で実行してください: `git push origin main`
```

### バージョニング規則

セマンティックバージョニング (MAJOR.MINOR.PATCH) を使用:

- **MAJOR**: 破壊的変更（設定ファイル構造の変更等）
- **MINOR**: 後方互換性のある機能追加
- **PATCH**: バグ修正とマイナーな改善

### リリースプロセス

1. CHANGELOG.md の Unreleased セクションを確認
2. バージョン番号を決定
3. Unreleased を新バージョンに変更
4. コミット: `chore: bump version to x.y.z`
5. タグ作成: `git tag vx.y.z`
6. Push: `git push origin main --tags`

---

## Testing Guidelines

### 機能追加時のテスト項目

- **ホットキー1**: 設定したホットキーで録音→文字起こしが動作
- **ホットキー2**: 別のホットキーで異なるバックエンドが使用される
- **設定UI**: 両方のホットキー設定が正しく保存・読み込みされる
- **ホットリロード**: settings.yaml 変更時に自動反映
- **マイグレーション**: 旧設定フォーマットから正しく移行される

### 動作確認コマンド

```bash
# 自動テスト（オフライン・鍵/ネットワーク/モデル DL なし）
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests   # Windows(Python)
swift test --package-path macos                                  # Mac(Swift)

# 開発モードで起動（Windows）
python run.py

# ビルドして実行（Windows）
pyinstaller voicekey.spec --clean --noconfirm
cd dist/voicekey
./voicekey.exe
```
