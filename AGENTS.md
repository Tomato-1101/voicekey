# Repository Guidelines

## Branch policy (最重要・2026-08-23 改訂)
- voicekey は **`personal` ブランチ 1 本だけ**で運用する。旧 `main`（自分用）/ `release`（製品版）/ `voice-agent` は **2026-08-23 に personal へ統合してアーカイブ済み**（GitHub からも削除。バックアップは `~/Project/_archive/voicekey-all-branches-2026-08-23.bundle`）。
  - **`personal` = 開発者本人が毎日使う唯一の版**。4 プロバイダーを**実プロバイダー名で表示**（OpenAI / Groq / ElevenLabs / Deepgram）＋モデル名表示・選択可、さらに**ローカル（Apple）オンデバイス文字起こし**（macOS 26+・キー不要）。API キーは中央 Keychain 直読み（ログイン・課金なし）。
- **新しいブランチを勝手に作らない**。作業は `personal` に直接コミットする。旧ブランチ名を前提にした手順・分岐を新しく書かない（過去の記述は歴史的経緯として読む）。

## Cross-platform rule (重要)
- voicekey は Mac（`macos/` Swift）と Windows（`src/` Python）の二本立て。**UI 文言・表示名・設定項目・機能挙動など、両 OS に同等に存在する要素を変えるときは両方を同じ作業で反映し 1 コミットにまとめる**（片方だけ変えない）。OS 固有 API（win32 / launchd / CGEventTap 等）でしか存在しない要素のみ片方で完結してよい。両 OS 同時実装は 1 コミットにまとめる。
- ユーザーから見える挙動を変えたら **README も同じコミットで更新**する。
- バックエンド表示名は**実プロバイダー名 + モデル名**（personal 一本化により特徴名 2 択の出し分けは廃止）。
- **VAD・長文分割・ストリーミング・録音 HUD は常時 ON 固定**（設定 UI から撤去済み）。Mac は `ConfigStore` で true 固定、Windows は `config_manager._force_always_on` で読込・保存時に矯正。

## Project Structure & Module Organization
- `src/app.py` orchestrates config, audio pipeline, and UI; `src/main.py` boots the Qt app.
- `src/config/` holds enums/defaults and `config_manager.py` for hot-reloadable settings.
- `src/core/` contains audio capture, VAD (`vad.py` — Silero ONNX via onnxruntime on CPU), cloud transcription backends (`api_transcriber.py` for REST: Deepgram / ElevenLabs / OpenAI / Groq, `streaming_transcriber.py` for Deepgram streaming — no local model), LLM text formatting, and simulated input. The Windows side still carries the server-auth layer (`auth_client.py` / `backend_client.py` / `login_coordinator.py`); the Mac personal build bypasses it and reads keys straight from the central Keychain.
- `src/ui/` defines the compact recording HUD (`hud.py`), settings window (`settings_window.py`), styles, feedback dialog, and system tray integration; `src/utils/logger.py` configures logging.
- The Mac app is a separate Swift codebase under `macos/Sources/Voicekey/` (not Python). See `OVERVIEW.md` for the file-by-file Mac/Windows map.
- Runtime config lives in `settings.yaml`; secrets go in `.env` (see `.env.example`). Packaging specs: `voicekey.spec` and `voicekey_debug.spec`; built artifacts land in `dist/` with staging in `build/`.

## Build, Test, and Development Commands
- Create env (Windows): `python -m venv venv` then `.\venv\Scripts\Activate.ps1`; install deps with `pip install -r requirements.txt`. No GPU/CUDA needed — transcription is cloud API and VAD runs on CPU (onnxruntime); `torch`/`torchaudio` are only transitive deps of `silero-vad`.
- Run dev app (Windows): `python run.py` (reads `settings.yaml` and `.env`, opens system tray + recording HUD).
- Package (Windows): `pyinstaller voicekey.spec --clean --noconfirm` → `dist/voicekey/voicekey.exe`.
- Mac: build with `cd macos && ./scripts/build_app.sh` (distribution DMG via `build_dmg.sh`).

## Coding Style & Naming Conventions
- Python 3.8+ with 4-space indentation; keep type hints and concise docstrings (existing ones are Japanese—match that tone).
- Use snake_case for variables/config keys, PascalCase for classes, and upper snake for constants (`CONFIG_CHECK_INTERVAL_SEC`).
- Prefer the shared logger (`src/utils/logger.py`) over ad-hoc prints; keep user-facing strings localized as currently written.
- UI follows PySide6; keep signals/slots thread-safe and avoid blocking the Qt event loop.

## Testing Guidelines
- Automated tests exist and must stay offline (no network, no API keys, no model download). Python (Windows): `QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests` — runs headless and mocks heavy deps. Mac: `swift test --package-path macos`.
- Add Python tests as `tests/test_*.py`; add Mac tests under `macos/Tests/VoicekeyTests/`. Mock keyring / network / transcription so tests never touch real credential stores or services.
- For manual checks, validate hotkey start/stop, HUD state changes, each transcription backend (Deepgram / Groq / ElevenLabs / OpenAI / ローカル(Apple)), VAD behavior, and LLM formatting fallback.

## Commit & Pull Request Guidelines
- Follow the current history style: short, imperative summaries with optional prefixes (e.g., `docs: update project documentation`, `Improve overlay UI and hotkey handling`).
- Each PR should describe behavior changes, affected settings, and manual test evidence (commands run + observed results); include screenshots/GIFs for UI changes.
- Link related issues, call out config or env var additions (`.env`, `settings.yaml`), and note any migration steps for packagers or release builds.

## Security & Configuration Tips
- Keep API keys in the central Keychain (service = variable name / account = `shared`); Windows dev may use `.env` (DEEPGRAM_API_KEY, ELEVENLABS_API_KEY, OPENAI_API_KEY, GROQ_API_KEY). Never commit secrets or local `settings.yaml`. No provider key is ever embedded in a build.
- Verify `ffmpeg` is on PATH; transcription needs network access (cloud API). No GPU/CUDA setup is required.
- When switching backends, confirm `transcription_backend` and model names in `settings.yaml` align with installed/available services to avoid runtime warnings.

## Documentation
- **`OVERVIEW.md` is the project map** (feature list, architecture map, branch policy, distribution layout, and an index of all docs). Read it first to grasp the whole project. Whenever you change features, architecture, branch spec, distribution layout, or the doc structure, update the relevant line of `OVERVIEW.md` in the **same commit** — but keep it to links + one-line summaries (details live in README/CHANGELOG/HANDOFF; duplication causes drift). With a single branch, the `personal` copy of `OVERVIEW.md` is the only source of truth.
- `README.md` is user-facing documentation. Whenever you change behavior users can see — features, usage, settings, supported platforms, or release/distribution status — update the relevant part of `README.md` in the **same commit** as the code change (do not defer it).
- Keep the "🚦 このリポジトリについて" section at the top of `README.md` current (branch/build state and last-updated date). This rule is also recorded in `CLAUDE.md`.
