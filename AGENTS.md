# Repository Guidelines

## Branch policy (最重要・2026-06-17)
- voicekey は **2 ブランチ運用**。絶対に混ぜない:
  - **`main` = 自分用**: 開発者が自分の API キーで使う版。4 プロバイダーを**実プロバイダー名で表示**（OpenAI / Groq / ElevenLabs / Deepgram）＋モデル名表示・選択可。
  - **`release` = 製品版**: 顧客配布版（配布タグ `v*` はこちらで打つ）。文字起こしは **Deepgram=「高速リアルタイム」/ ElevenLabs=「正確性」の 2 択のみ**、モデル非選択、Groq 整形を裏で固定実行。
- **どのブランチに変更を入れるかは毎回ユーザーが指定する。指定が無ければ必ず聞く**（main / release / 両方）。推測でどちらかに入れない。2 ブランチを勝手に混ぜない（指示なき cherry-pick / merge 禁止）。

## Cross-platform rule (重要)
- voicekey は Mac（`macos/` Swift）と Windows（`src/` Python）の二本立て。**UI 文言・表示名・設定項目・機能挙動など、両 OS に同等に存在する要素を変えるときは両方を同じ作業で反映し 1 コミットにまとめる**（片方だけ変えない）。OS 固有 API（win32 / launchd / CGEventTap 等）でしか存在しない要素のみ片方で完結してよい。両 OS 同時実装は**同一ブランチ内**で行う。
- ユーザーから見える挙動を変えたら **README も同じコミットで更新**する。
- バックエンド表示名はブランチで異なる。`main`（自分用）は実プロバイダー名、`release`（製品版）は 2 択の特徴名（高速リアルタイム / 正確性）。
- **VAD・長文分割・ストリーミング・録音 HUD は両ブランチとも常時 ON 固定**（設定 UI から撤去済み）。Mac は `ConfigStore` で true 固定、Windows は `config_manager._force_always_on` で読込・保存時に矯正。

## Project Structure & Module Organization
- `src/app.py` orchestrates config, audio pipeline, and UI; `src/main.py` boots the Qt app.
- `src/config/` holds enums/defaults and `config_manager.py` for hot-reloadable settings.
- `src/core/` contains audio capture, VAD, transcription backends (local/Groq/OpenAI), LLM text processing, and simulated input.
- `src/ui/` defines the Dynamic Island overlay, settings window, styles, and system tray integration; `src/utils/logger.py` configures logging.
- Runtime config lives in `settings.yaml`; secrets go in `.env` (see `.env.example`). Packaging specs: `voicekey.spec` and `voicekey_debug.spec`; built artifacts land in `dist/` with staging in `build/`.

## Build, Test, and Development Commands
- Create env: `python -m venv venv` then `.\venv\Scripts\Activate.ps1`; install deps with `pip install -r requirements.txt` (install CUDA wheels via the provided PyTorch index if using GPU).
- Run dev app: `python run.py` (reads `settings.yaml` and `.env`, opens system tray + overlay).
- Package: `pyinstaller voicekey.spec --clean --noconfirm` → `dist/voicekey/voicekey.exe`.
- Optional: `python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121` when CUDA wheels are missing.

## Coding Style & Naming Conventions
- Python 3.8+ with 4-space indentation; keep type hints and concise docstrings (existing ones are Japanese—match that tone).
- Use snake_case for variables/config keys, PascalCase for classes, and upper snake for constants (`CONFIG_CHECK_INTERVAL_SEC`).
- Prefer the shared logger (`src/utils/logger.py`) over ad-hoc prints; keep user-facing strings localized as currently written.
- UI follows PySide6; keep signals/slots thread-safe and avoid blocking the Qt event loop.

## Testing Guidelines
- No automated tests exist; validate manually: hotkey start/stop, overlay state changes, local vs Groq/OpenAI backends, VAD behavior, and LLM post-processing fallback.
- Run in `dev_mode: true` when investigating timing; review `dev_timing.log` and console logs for regressions.
- When adding tests, prefer pytest-style functions and name files `test_*.py` alongside target modules.

## Commit & Pull Request Guidelines
- Follow the current history style: short, imperative summaries with optional prefixes (e.g., `docs: update project documentation`, `Improve overlay UI and hotkey handling`).
- Each PR should describe behavior changes, affected settings, and manual test evidence (commands run + observed results); include screenshots/GIFs for UI changes.
- Link related issues, call out config or env var additions (`.env`, `settings.yaml`), and note any migration steps for packagers or release builds.

## Security & Configuration Tips
- Keep API keys in `.env` (GROQ_API_KEY, CEREBRAS_API_KEY, OPENAI_API_KEY); never commit secrets or local `settings.yaml`.
- Verify `ffmpeg` is on PATH and select the correct CUDA wheel for your GPU; on fallback to cloud, ensure network access is available.
- When switching backends, confirm `transcription_backend` and model names in `settings.yaml` align with installed/available services to avoid runtime warnings.

## Documentation
- **`OVERVIEW.md` is the project map** (feature list, architecture map, the 2-branch difference, distribution layout, and an index of all docs). Read it first to grasp the whole project. Whenever you change features, architecture, branch spec, distribution layout, or the doc structure, update the relevant line of `OVERVIEW.md` in the **same commit** — but keep it to links + one-line summaries (details live in README/CHANGELOG/HANDOFF; duplication causes drift). Keep `OVERVIEW.md` **identical on `main` and `release`** (branch differences are written inside it); update both branches together.
- `README.md` is user-facing documentation. Whenever you change behavior users can see — features, usage, settings, supported platforms, or release/distribution status — update the relevant part of `README.md` in the **same commit** as the code change (do not defer it).
- Keep the "🚦 配布ステータス" table at the top of `README.md` current (latest Mac/Windows version, distribution state, and last-updated date). This rule is also recorded in `CLAUDE.md`.
