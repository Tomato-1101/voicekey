# Windows 配布ビルド手順

Windows 実機で voicekey の配布用インストーラ（テスター向け・API キー埋め込み版）を
ビルドする手順。macOS 上では構文・ロジック・UI の検証までしかできないため、
本書のチェックリストは必ず Windows 実機で通すこと。

## 前提ソフトウェア

| ソフト | 用途 | 入手先 |
|---|---|---|
| Python 3.10+ | アプリ本体 | python.org |
| Inno Setup 6 | インストーラ作成（ISCC.exe） | https://jrsoftware.org/isdl.php |

## 初回セットアップ

```powershell
git clone <voicekey リポジトリ> ; cd voicekey
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
```

## API キーの持ち込み（.env.dist）

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

## ビルド

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build\build_windows_dist.ps1 -Version 1.0.0
```

スクリプトが行うこと:

1. `src\config\constants.py` の `APP_VERSION` を指定バージョンに更新（恒久変更・要コミット）
2. `.env.dist` から XOR 難読化した `src\config\embedded_keys.py` を生成
3. PyInstaller（onedir・`noarchive=False` で PYZ 固め）
4. **dist に .py が混入していないか検査**（あれば配布中止）
5. Inno Setup で `dist\installer\voicekey-<Version>-setup.exe` を作成
6. SHA256 を計算して `dist\installer\version.json` を出力
7. `embedded_keys.py` を削除（後始末）

## リリース（順番厳守）

1. `voicekey-<Version>-setup.exe` を voicekey-releases リポジトリの
   GitHub Releases（タグ `v<Version>`）へ添付
2. `dist\installer\version.json` を voicekey-releases の `windows/version.json` に
   上書きコミット → push
   - **version.json のコミットが「全テスターへの更新通知」のトリガー**。
     Releases へのバイナリ添付を必ず先に済ませること（逆順だと DL 404 で全員に失敗通知が出る）
3. voicekey 本体リポジトリで `constants.py`（APP_VERSION）の変更をコミット

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
