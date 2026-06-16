# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**進行中の長期作業がある場合はまず `HANDOFF.md` を読む**（ベータ配布計画の現在地・恒久要件が書いてある）。

## 最重要: コードを変更したら README も更新する（2026-06-14 ユーザー指示）

`README.md` は**ユーザー向けのドキュメント**。機能・使い方・設定項目・対応プラットフォーム・配布状態など、
**ユーザーから見える挙動を変える変更を入れたら、同じ作業の中で README の該当箇所も最新に更新する**。
- 機能追加／変更 → 該当する説明・使い方の節を直す。設定項目が増減 → 設定方法の節を直す。
- バージョンやリリース状態が変わった → 冒頭「🚦 配布ステータス」表と最終更新日を直す。
- README 更新はコード変更と同じ 1 コミットにまとめる。「あとでまとめて」は禁止。
- 同じ趣旨を `AGENTS.md` にも記載（他の AI エージェントでも守らせるため）。

## 最重要: 両OSに存在する変更は Mac・Windows 同時に実装する（2026-06-16 ユーザー指示）

UI の文言・表示名・設定項目・機能挙動など、**ユーザーから見て両 OS に同等に存在する要素を変えるときは、
Mac（`macos/` Swift）と Windows（`src/` Python）の両方を同じ作業で反映し、1 コミットにまとめる**。片方だけ変えない。
- Mac はビルド＆再起動で、Windows は `py_compile`＋`unittest` で各々検証してから報告。
- Windows 固有（win32・レジストリ自動起動）/ Mac 固有（launchd・SMAppService・CGEventTap）でしか存在しない要素だけ片方で完結してよい。
- リリース配布も常に両 OS 同期（バージョンは Claude が semver で決める。範囲・版番号はユーザーに聞かない）。
- 提供元名は伏せ、バックエンドは特徴名で表示する（`openai`→高精度 / `groq`→高速 / `elevenlabs`→多言語 / `deepgram`→リアルタイム。保存値は不変。API キー欄のみ提供元名）。

## 最重要: Mac 版（macos/ ディレクトリ・Swift）の作業ルール

このリポジトリには Windows 版（下記 Project Overview、Python/faster-whisper）に加えて
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

## Project Overview

voicekey (SuperWhisper) is a Windows desktop application for real-time speech-to-text transcription using faster-whisper. It runs as a system tray application with a Dynamic Island-style overlay UI, activated by global hotkeys.

## Development Commands

### Running the Application

```bash
# Development mode
python run.py

# Build executable (creates dist/SuperWhisperLike/SuperWhisperLike.exe)
pyinstaller SuperWhisperLike.spec --clean --noconfirm
```

### Setup

```bash
# Create and activate virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1  # Windows PowerShell

# Install dependencies
pip install -r requirements.txt

# Install PyTorch with CUDA 12.1 (required for GPU acceleration)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
```

## System Requirements

- **CUDA-capable NVIDIA GPU required** - The application uses GPU acceleration exclusively via faster-whisper
- Python 3.8+
- ffmpeg must be installed and available in PATH

## Architecture

### Core Application Flow

1. **Dual Hotkey Detection** (`src/app.py`): Keyboard listener monitors TWO independent hotkeys simultaneously
2. **Audio Recording** (`src/core/audio_recorder.py`): Captures audio using sounddevice when either hotkey triggered
3. **Backend Selection**: Each hotkey can use different transcription backend (local/groq/openai)
4. **Transcription** (`src/core/transcriber.py`): Processes audio with selected backend
5. **Text Input** (`src/core/input_handler.py`): Injects transcribed text into active window using pynput
6. **UI Updates**: PySide6 signals/slots coordinate updates to overlay and system tray

### Key Components

**SuperWhisperApp** (`src/app.py`):
- Central controller integrating all components
- **Dual Hotkey Support**: Manages 2 independent hotkey slots
  - Each slot: hotkey, mode (hold/toggle), backend (local/groq/openai), API model/prompt
  - `HotkeySlot` dataclass holds per-slot configuration
  - `_hotkey_slots: Dict[int, HotkeySlot]` manages both slots
  - `_active_slot` tracks which slot is currently recording
- **Shared Local Transcriber**: `_local_transcriber` instance shared by both slots (VRAM efficient)
- **Per-Slot API Transcribers**: Each slot has its own GroqTranscriber/OpenAITranscriber
- Manages two background daemon threads:
  - Keyboard listener for dual hotkey detection
  - Config monitor for hot-reload support
- Thread-safe communication via PySide6 signals (status_changed, text_ready)
- Handles recording cancellation when new recording starts during transcription

**Transcriber** (`src/core/transcriber.py`):
- Lazy-loads WhisperModel on first transcription or preloads during recording
- Auto-unloads model after configurable delay (release_memory_delay) to free VRAM
- Thread-safe model management with locks and timer cancellation
- Filters transcription segments by no_speech_prob to prevent hallucinations
- **CRITICAL**: Always checks torch.cuda.is_available() and raises RuntimeError if CUDA unavailable

**ConfigManager** (`src/config/config_manager.py`):
- Loads settings.yaml from project root (or executable directory when frozen)
- **Automatic Migration**: Detects legacy config format and auto-converts to new structure
- Monitors file mtime and triggers hot-reload without restart
- Deep-merges user config with DEFAULT_CONFIG from constants.py (nested dict support)
- `_deep_merge()` helper for recursive dict merging

**DynamicIslandOverlay** (`src/ui/overlay.py`):
- Frameless, always-on-top window at screen top-center
- Three states: idle (hidden), recording (red pulse animation), transcribing
- Custom paintEvent for pill-shaped background with pulse effect
- Animates size changes with QPropertyAnimation

### Configuration System

**settings.yaml** controls all runtime behavior with hierarchical structure:

```yaml
# Global settings (shared by both hotkeys)
language: ja
vad_filter: true
vad_min_silence_duration_ms: 500

# Local backend settings (shared)
local_backend:
  model_size: large-v3
  compute_type: float16
  release_memory_delay: 300
  # ... other Whisper parameters

# Hotkey 1 configuration
hotkey1:
  hotkey: <shift_r>
  hotkey_mode: hold
  backend: openai
  api_model: gpt-4o-mini-transcribe
  api_prompt: ""

# Hotkey 2 configuration
hotkey2:
  hotkey: <f2>
  hotkey_mode: toggle
  backend: groq
  api_model: whisper-large-v3-turbo
  api_prompt: ""
```

**Key Configuration Features**:
- **Dual Hotkey**: Fixed 2 slots (`hotkey1` / `hotkey2`), each with independent settings
- **Backend Selection**: Per-hotkey backend (local/groq/openai)
- **Shared Local Settings**: `local_backend` section applies to both hotkeys
- **API Models**: Different models per hotkey for Groq/OpenAI
- **Hot-Reload**: Changes detected automatically by config monitor thread and applied without restart
- **Backward Compatibility**: Old single-hotkey format auto-migrates to new structure

### Threading Model

- **Main Thread**: PySide6 event loop for UI
- **Keyboard Listener Thread**: pynput keyboard listener (daemon)
- **Config Monitor Thread**: Polls settings.yaml mtime every CONFIG_CHECK_INTERVAL_SEC (daemon)
- **Transcription Workers**: Short-lived daemon threads spawned per transcription request
- **Model Preload**: Background thread started during recording to reduce latency

### PyInstaller Packaging

**SuperWhisperLike.spec**:
- One-Dir mode (not One-File) for faster startup
- Bundles settings.yaml into dist
- Collects faster_whisper and ctranslate2 dependencies with collect_all
- Hidden imports for all src modules
- Console=False for windowed app

## Hallucination Prevention

The app implements multiple strategies to prevent Whisper from generating phantom text:

1. **VAD Filter**: Voice Activity Detection removes silent segments before transcription
2. **no_speech_threshold**: Model's internal threshold during inference (default 0.6)
3. **no_speech_prob_cutoff**: Post-processing filter that discards segments with high no_speech_prob (default 0.7)
4. **condition_on_previous_text=false**: Prevents model from continuing previous patterns

Users experiencing hallucinations like "ご視聴ありがとうございました" should:
- Increase no_speech_threshold (0.7-0.8)
- Decrease no_speech_prob_cutoff (0.5-0.6)
- Enable vad_filter if disabled

## Common Issues

### WinError 1314 (Symbolic Link Privilege)
Set `model_cache_dir` in settings.yaml to avoid HuggingFace Hub trying to create symlinks. Example: `D:/whisper_cache`

### Text Not Inserting
- App needs admin privileges to inject text into admin-elevated windows
- Check for hotkey conflicts with other applications

### VRAM Management
- Model unloads after release_memory_delay seconds of inactivity
- Reduce model_size or use int8 compute_type for lower VRAM usage
- Model preloads during recording to minimize transcription delay

## Code Style Notes

- Type hints used throughout (from typing import)
- Enums defined in src/config/types.py (HotkeyMode, ModelSize, ComputeType, AppState)
- Constants in src/config/constants.py (UI dimensions, intervals, default config)
- Logger via src/utils/logger.py (get_logger(__name__))
- PySide6 signals for thread-safe UI updates (never manipulate UI from worker threads directly)

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
# 開発モードで起動
python run.py

# ビルドして実行
pyinstaller SuperWhisperLike.spec --clean --noconfirm
cd dist/SuperWhisperLike
./SuperWhisperLike.exe
```
