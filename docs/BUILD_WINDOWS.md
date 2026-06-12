# Windows 配布ビルド手順

voicekey の配布用インストーラ（テスター向け・API キー埋め込み版）のビルド手順。
**標準の方法は GitHub Actions（Mac から完結・Windows 実機不要）**。
Windows 実機での手動ビルドはフォールバック（後述）。
いずれの場合も「配布前チェックリスト」の実機確認は Windows 機で通すこと
（ビルドと動作確認は別物）。

## 標準: GitHub Actions でビルド（Mac から完結）

ワークフロー: `.github/workflows/windows-build.yml`（windows-latest ランナー）。
キーは GitHub Secrets → ビルドステップの環境変数として注入する。
`.env.dist` をランナーに置かず、ログにはマスクされて出ない。

### 初回のみ: Secrets 登録（ユーザー本人が実行）

```bash
# Mac 側・リポジトリ直下で（.env.dist が無ければ先に --export-env で生成）
./macos/scripts/generate_embedded_keys.sh --export-env
gh secret set -f .env.dist
# → OPENAI_API_KEY / GROQ_API_KEY / ELEVENLABS_API_KEY / DEEPGRAM_API_KEY が登録される
rm .env.dist   # 登録後はローカルに残さない
```

キーをローテーションしたら同じ手順で再登録する。

### 毎リリース

```bash
gh workflow run windows-build.yml -f version=1.0.0
gh run watch                          # 進捗を見る（約 10〜15 分）
gh run download -n voicekey-windows-installer -D dist/ci/
```

`dist/ci/` に `voicekey-<Version>-setup.exe` と `version.json` が落ちる。
以降は「リリース（順番厳守）」へ。

ビルド内容はローカルと同一（build_windows_dist.ps1 を CI 上で実行）:
constants.py の APP_VERSION 更新 → キー埋め込み生成 → PyInstaller →
.py 混入検査 → Inno Setup → SHA256 + version.json。
注意: CI 上の constants.py 変更はランナー内で消えるため、
**リリース後にローカルでも APP_VERSION を同じ値に更新してコミットする**。

## フォールバック: Windows 実機で手動ビルド

GitHub Actions が使えないとき（Secrets 失効・ランナー障害等）のみ。

### 前提ソフトウェア

| ソフト | 用途 | 入手先 |
|---|---|---|
| Python 3.10+ | アプリ本体 | python.org |
| Inno Setup 6 | インストーラ作成（ISCC.exe） | https://jrsoftware.org/isdl.php |

### 初回セットアップ

```powershell
git clone <voicekey リポジトリ> ; cd voicekey
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
```

### API キーの持ち込み（.env.dist）

埋め込みキーは git 管理外の `.env.dist`（リポジトリ直下）から読む。
Mac 側で書き出して **手動コピー**する（メール・チャット・クラウドに上げない）:

```bash
# Mac 側（リポジトリ直下で）
./macos/scripts/generate_embedded_keys.sh --export-env
# → .env.dist が生成される。USB メモリ等で Windows ビルド機のリポジトリ直下へコピー
```

`.env.dist` の中身は `OPENAI_API_KEY=...` / `GROQ_API_KEY=...` /
`ELEVENLABS_API_KEY=...` / `DEEPGRAM_API_KEY=...` の形式。

**ビルドが終わったら `.env.dist` は削除する**（`src\config\embedded_keys.py` は
ビルドスクリプトが自動削除する）。

### ビルド

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build\build_windows_dist.ps1 -Version 1.0.0
```

スクリプトが行うこと:

1. `src\config\constants.py` の `APP_VERSION` を指定バージョンに更新（恒久変更・要コミット）
2. `.env.dist`（または環境変数）から XOR 難読化した `src\config\embedded_keys.py` を生成
3. PyInstaller（onedir・`noarchive=False` で PYZ 固め）
4. **dist に .py が混入していないか検査**（あれば配布中止）
5. Inno Setup で `dist\installer\voicekey-<Version>-setup.exe` を作成
6. SHA256 を計算して `dist\installer\version.json` を出力
7. `embedded_keys.py` を削除（後始末）

## リリース（順番厳守）

1. `voicekey-<Version>-setup.exe` を Mac の `/Users/tomato/Project/voicekey-site/downloads/` へ、
   `version.json` を `voicekey-site/windows/version.json` へコピー
   （配布はすべて Vercel サイト経由。GitHub はテスターから見えない構成）
2. `voicekey-site/downloads.json` の windows エントリを新バージョンに更新
3. voicekey-site で `vercel deploy --prod`
   - **version.json の公開が「全テスターへの更新通知」のトリガー**。exe と version.json が
     同一デプロイで同時に公開されるため、順序問題（DL 404）は起きない
4. voicekey 本体リポジトリで `constants.py`（APP_VERSION）の変更をコミット

## 配布前チェックリスト（Windows 実機・毎リリース）

### ソース非公開の確認
- [ ] `dist\voicekey\` 以下に `.py` ファイルが 1 つも無い（ビルドスクリプトでも自動検査）
- [ ] `dist\voicekey\voicekey.exe` 等に平文 API キーが無い:
      `findstr /m "sk-" dist\voicekey\*.exe`（XOR 難読化のため検出されないのが正常）

### 機能（クリーン環境＝Credential Manager にキー未登録の Windows で）
- [ ] インストーラが UAC 昇格なしで完走し、`%LOCALAPPDATA%\Programs\voicekey` に入る
- [ ] スタートメニューとスタートアップにショートカットが作られる
- [ ] **API キーを一切入力せず**にホットキー録音 → 文字起こしが通る（埋め込みキー動作確認）
- [ ] 設定画面に「API キー」タブが表示されない（DIST ビルドの判定確認）
- [ ] 履歴・自動 Enter など主要機能が一通り動く
      （voicekey.spec の datas から src を外したため、動的読み込みの欠落がないか重点確認）

### 自動アップデート E2E（初回と、アップデータ周りを触ったとき）
1. 旧バージョン（例 1.0.0）をインストールして起動
2. 新バージョン（例 1.0.1）をビルドし、ローカル HTTP サーバで version.json と setup.exe を配信
   （`src\utils\updater.py` の `VERSION_URL` を一時的に `http://localhost:8000/...` に
   変えたテストビルドを使う）
3. - [ ] 起動 60 秒後にトレイ通知「新しいバージョン 1.0.1 があります」が出る
   - [ ] トレイメニュー「アップデート 1.0.1 をインストール…」→ サイレント更新 → 新版が自動再起動
   - [ ] 更新後のバージョンが 1.0.1 になっている（トレイのフィードバックメール件名等で確認）
4. - [ ] sha256 を故意に壊した version.json で「アップデート失敗」通知が出て、アプリは動き続ける

### 後始末
- [ ] `.env.dist` を削除した
- [ ] `src\config\embedded_keys.py` が残っていない（スクリプトが削除するが念のため）

## トラブルシューティング

- **ModuleNotFoundError が dist 実行時に出る**: voicekey.spec の
  `hiddenimports += collect_submodules('src')` で src 全体を収集している。
  サードパーティ製モジュールの遅延 import が原因なら、そのモジュールを hiddenimports に追加する。
- **インストーラ実行中「voicekey を閉じています」で止まる**: 旧プロセスが応答していない。
  タスクマネージャで voicekey.exe を終了してから再実行。
- **更新通知が来ない**: DIST ビルドのみアップデータが動く（開発実行 `python run.py` では無効）。
  ログ（`%LOCALAPPDATA%\Programs\voicekey\app.log`）で `自動アップデート` の行を確認。
