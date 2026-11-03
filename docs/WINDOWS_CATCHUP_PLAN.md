# Windows 版を Mac 版に追いつかせる計画（2026-10-18・未着手）

本人の依頼:「Windows 版はやっぱり開発したい。Mac 版と同じ感じでいい」。今はまだやらず計画だけ（本人指示）。
再開の合図が出たら CLAUDE.md の「Windows の開発は一旦停止」を外し、両 OS 同時実装のルールを戻す。

## いまの差（2026-10-18 時点・コードと CHANGELOG で確認）
- 版: Windows は `APP_VERSION` 1.8.0（`src/config/constants.py`）、Mac は 2.3.0。
  Windows の src/ に最後に入ったのは 1ea7e59（2026-10-01、離鍵直前の末尾欠け修正）。
- エンジン: Windows の設定画面は「即時入力（Deepgram nova-3）」と「スタンダード（Groq）」の 2 択だけ。
  Mac は Soniox（ライブ・既定）／Apple ローカル／OpenAI ライブ／gpt-transcribe／MAI-Transcribe-2／ElevenLabs scribe_v2／Groq。
- Windows に無い Mac の機能:
  - 入力まわり: 設定 › API キー（全エンジン・状態表示）、再貼り付けキー、翻訳して入力、1 回だけ翻訳するキー。
  - 表示・記録: API 料金表示、診断ログ `[計測]`、履歴 200 件＋アプリ名（Windows は 10 件）。
  - 外観: 新ロゴ・配色の UI 反映（Windows は icon.ico だけ）。
  - 大きな機能: ライブ字幕、議事録、Meet ボット、Apple ローカル認識。
- 配布・更新: Windows の更新確認は `https://voicekey.vercel.app/windows/version.json` を見ているが置き場が無く、
  実質動かない（推測）。`src/utils/updater.py`・`scripts/build/build_windows_dist.ps1`・`.github/workflows/windows-build.yml`
  には削除済みの voicekey-releases への参照が残る。
- Windows にだけあるもの: Deepgram の即時入力、音量正規化、WDM-KS/ASIO マイク除外、Acrylic、Inno Setup、
  旧製品版のサーバー認証層（personal では未使用）。

## 再開時に本人に聞くこと（先にまとめて 1 回で）
1. Windows も GitHub Releases で配る？（今の CLAUDE.md は「Windows は配布しない・ソースから」）
2. Apple ローカル認識の代わり（Windows 内蔵の認識 or 見送り）は要る？
3. 既定エンジン（Mac に合わせて Soniox？）。エンジンの切り替えは本人が決める。
4. ライブ字幕・議事録・Meet ボットまで持っていく？

## 段階（上から順に。各段階で `py_compile`＋`unittest` を通してから次へ）
### 段階 0: 再開の準備（小）
- Windows 実機か VM でテストが通るか確かめる（`QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests`）。
- 更新・ビルドの voicekey-releases 参照を外す（GitHub Releases へ付け替え）。版番号を Mac と揃えるか決める。
### 段階 1: 毎日の音声入力を Mac と同じにする（低）
- エンジン: Soniox ライブ（WebSocket・接続先の JP/US 自動判定・失敗時に REST で流し直し）、gpt-transcribe、
  MAI-Transcribe-2、scribe_v2。選択肢の並びとモデル選択は Mac に合わせる。
- 設定 › API キー: 全エンジン、状態表示（アプリに保存済み／環境変数／Credential Manager／未設定）、
  キーが無くて失敗したら通知して設定を開く。読む順番は「アプリ保存値 → 環境変数 → Credential Manager」。
- 再貼り付けキー、履歴 200 件＋アプリ名、診断ログ `[計測]`、新ロゴ・配色。
### 段階 2: 便利機能（中）
- 翻訳して入力（Groq / Gemini。Apple 翻訳の代わり）＋1 回だけ翻訳するキー。
- API 料金表示（`macos/Sources/Voicekey/Core/ApiPricing.swift` の単価表を移す）。
- 自動更新を「裏でダウンロード → ボタン 1 回で入れ替え」に（今の Ed25519 署名検証は残す）。
- スクリーンショット添付（[SCREENSHOT_ATTACH_PLAN.md](SCREENSHOT_ATTACH_PLAN.md)。Mac で入れたら同時に）。
### 段階 3: 大きな機能（高・方式を決めてから）
- ライブ字幕と議事録: 音は WASAPI ループバック（アプリ単位なら Process Loopback API）。翻訳は Groq / Gemini / Soniox。
- Meet ボット: Chrome を CDP で動かす部分と `MeetBotScripts.swift` の DOM 処理は移せる。まず Meet 字幕を読む経路だけ。
- Apple ローカル認識の代わり: 質問 2 の答え次第。

## 進め方
- 段階ごとに 1 セッション。実装は Claude のサブエージェント（定型は Sonnet、Soniox ライブ・更新・字幕は Opus）、
  レビューは Codex。課金 API を叩くテストはしない（必要なら回数と金額を出して承認を取る）。
- Mac の実装（`macos/Sources/Voicekey/`）を仕様の正本として読み、Windows は既存の Python / PySide6 の作りに合わせる。
