# Repository Guidelines

## Cross-platform rule (重要)
- voicekey は Mac（`macos/` Swift）と Windows（`src/` Python）の二本立て。**UI 文言・表示名・設定項目・機能挙動など、両 OS に同等に存在する要素を変えるときは両方を同じ作業で反映し 1 コミットにまとめる**（片方だけ変えない）。OS 固有 API（win32 / launchd / CGEventTap 等）でしか存在しない要素のみ片方で完結してよい。
- ユーザーから見える挙動を変えたら **README も同じコミットで更新**する。
- バックエンドは提供元名を伏せ**特徴名で表示**（openai→高精度 / groq→高速 / elevenlabs→多言語 / deepgram→リアルタイム）。保存値は不変。API キー欄のみ提供元名。

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
- `README.md` is user-facing documentation. Whenever you change behavior users can see — features, usage, settings, supported platforms, or release/distribution status — update the relevant part of `README.md` in the **same commit** as the code change (do not defer it).
- Keep the "🚦 配布ステータス" table at the top of `README.md` current (latest Mac/Windows version, distribution state, and last-updated date). This rule is also recorded in `CLAUDE.md`.
