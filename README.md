# Voicekey

<div align="center">

**高速・高精度な常駐型音声入力ツール（Windows / macOS 対応）**

ホットキーを押すだけで音声入力を開始し、文字起こし結果を瞬時にアクティブウィンドウへ自動入力

[![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey.svg)]()
[![License](https://img.shields.io/badge/license-GPL--3.0-green.svg)](LICENSE)

</div>

---

## 🚦 配布ステータス

> このセクションはコード／リリース状態が変わるたびに更新します（最終更新: **2026-06-27**）。
>
> **このブランチ（`release`）は製品版**です。文字起こしは「高速リアルタイム / 正確性」の 2 モード・
> モデル選択なし・テキスト整形は裏で自動（オン/オフのみ）。配布タグ（`v*`）はこのブランチで打ちます。

| プラットフォーム | 最新版 | 配布状態 | 入手先 | 備考 |
|---|---|---|---|---|
| 🍎 macOS | **v1.4.0** | ✅ **配布中** | [voicekey.vercel.app](https://voicekey.vercel.app) | Apple Development 署名。初回起動のみ Gatekeeper 回避手順あり（サイト／DMG に同梱）。自動更新（Sparkle）対応 |
| 🪟 Windows | **v1.4.0** | ✅ **配布中** | [voicekey.vercel.app](https://voicekey.vercel.app) | GitHub Actions（`windows-build.yml`）でビルド。インストーラ（約 268MB）は容量が大きいため公開バイナリ専用リポ（`voicekey-releases`・ソース非公開）の GitHub Releases でホスト。未署名のため初回のみ SmartScreen 回避手順あり。自動更新対応。インストール時にデスクトップ／スタートメニューへショートカット作成 |

> **v1.4.0（2026-06-27）**: 使用実績（レベル・累計・節約時間・連続日数・チャート）を**アカウントに紐付け**、複数端末で合算・再インストール後も引き継げるようにした（ログイン中・未ログインは従来どおりローカル）。詳細は [CHANGELOG](CHANGELOG.md)。
>
> **v1.3.1（2026-06-27）**: 音声入力後に稀に「ログインが必要です」と誤表示される不具合を修正、音声入力の遅延を大幅短縮（サーバーを東京リージョンに固定＋短命トークンのキャッシュ）。

**配布の仕組み**: ダウンロードページは [voicekey.vercel.app](https://voicekey.vercel.app)。Mac の DMG は同サイトから、Windows インストーラは容量が大きいため GitHub Releases（公開バイナリ専用リポ。**ソースは含みません**）から入手します。**v1.3.0 から、配布版はアカウントへのログインと有効なアクティベーションキーが無いと文字起こしできません**（無料配布のアクティベーション制）。自社サーバー（`voicekey.vercel.app`）が利用権を検証し、Deepgram は短命トークンで直叩き・ElevenLabs / Groq はサーバープロキシ経由で処理します。旧版（v1.2.0 以前）の埋め込みキー直叩きは廃止され、**旧版は順次無効化されます**（提供元キーのローテーション）。

---

## 📖 目次

- [特徴](#-特徴)
- [AI エージェント向けセットアップ手順](#-ai-エージェント向けセットアップ手順)
- [必要環境](#-必要環境)
- [インストール](#-インストール)
- [クイックスタート](#-クイックスタート)
- [デュアルホットキー機能](#-デュアルホットキー機能)
- [設定方法](#-設定方法)
- [API 設定](#-api-設定)
- [音声前処理](#-音声前処理)
- [トラブルシューティング](#-トラブルシューティング)
- [開発者向け情報](#-開発者向け情報)

---

## ✨ 特徴

### 🎯 デュアルホットキーで賢く使い分け

**2 つのホットキーに異なるモードを割り当てて使い分けられます。**

- **ホットキー 1**: 高速リアルタイムで日常のメモ・チャット
- **ホットキー 2**: 正確性で重要な議事録

各ホットキーに以下を個別設定可能：

- ショートカットキー（`<F2>`, `<shift_r>`, `<cmd_r>`, `<ctrl>+<space>` 等）
- トリガーモード（`hold`: 押している間録音 / `toggle`: 押して開始/停止）
- 文字起こしモード（**高速リアルタイム** / **正確性**）
- プロンプト（専門用語のヒント）・テキスト整形オン/オフ

### ⚡ その他の主要機能

- **🖥️ Cross-Platform**: Windows / macOS 対応
- **✨ 自動テキスト整形**: 文字起こし結果を読みやすく自動整形（ホットキーごとにオン/オフ可）
- **🎙️ 高精度・低遅延**: 無音スキップ・音量正規化などの処理は常に最適な設定で自動実行
- **🪟 Dynamic Island 風 UI**: モダンなオーバーレイ表示
- **⚙️ GUI 設定**: 設定ウィンドウから簡単に変更。各設定は**開閉できる左サイドバー**から選びます（折りたたむとアイコンだけのレールになり、項目が増えても縦スクロールで全部に届きます）
- **🎯 ダブルタップ Auto-Enter**: 連続入力後に自動で Enter（チャットアプリ向け）
- **🤚 ハンズフリー切替キー**: 切替キー＋ホットキーで「押して開始・もう一度で停止」のトグル録音に
- **⌨️ JIS（日本語）配列対応**: 記号キーや ¥ / かな / 英数 / 無変換 / 変換 などもホットキーに使える
- **🏆 使用実績（統計＋チャート＋レベル）**: 設定の「実績」タブで、使うほど貯まるレベル・経験値に加え、
  **今日／今週／累計の入力量**をカウントアップ表示し、**期間（週・月・年）を切り替えられる棒グラフ**で日ごと・月ごとの入力量を可視化します。
  タイピングと比べた**推定節約時間**・累計文字数・連続利用日数も確認できる（集計は貼り付け後の処理のみ＝音声入力に待ちを足しません）。
  **ログイン中は実績がアカウントに紐付き、複数端末で合算・再インストール後も引き継がれます**（未ログインはこの端末のみのローカル集計）
- **🔄 自動アップデート**: 起動時と 1 日ごとに新バージョンを自動確認（Mac=Sparkle / Win=独自フィード）。
  設定の「バージョン情報」タブで現在のバージョン確認・手動チェックができ、新版が見つかると「今すぐ更新する」ボタンが出ます
- **📖 ユーザー辞書（確定置換）**: 設定の「ユーザー辞書」タブで、よく誤変換される語の置き換えルール（変換元 → 変換先）を登録できる。
  文字起こし・整形が終わった文章を貼り付ける直前にローカルで機械置換するため、音声入力に待ちを足しません

---

## 🤖 AI エージェント向けセットアップ手順

> **このセクションは、Claude Code / Cursor / Codex などの AI に「voicekey をセットアップして」と頼んだときに迷わず動けるよう、必要な情報を 1 か所にまとめたものです。**
> **AI へのプロンプト例（コピペ可）**: 「このリポジトリ（`https://github.com/Tomato-1101/voicekey`）を `~/Project` 直下にクローンして、README の『AI エージェント向けセットアップ手順』に従って起動できる状態にしてください。OS は macOS（または Windows）です。Groq の API キーは後で入力するのでスキップしてください。」

### 前提条件チェックリスト（AI がまず確認すべき項目）

| # | 項目 | 確認コマンド | 期待される結果 |
|---|---|---|---|
| 1 | OS | `uname -s` / `ver` | `Darwin`（macOS）または `Windows_NT` |
| 2 | Python 3.10+ | `python3 --version` | `Python 3.10.x` 以上 |
| 3 | git | `git --version` | バージョンが表示される |
| 4 | ffmpeg | `ffmpeg -version` | バージョンが表示される（未インストールなら手順 5 で導入） |
| 5 | ネットワーク | `curl -s https://api.groq.com` | レスポンスが返る（API 文字起こし用） |

### macOS 完全セットアップ（コピペで完結）

```bash
# 1. クローン
cd ~/Project   # 任意のディレクトリ
git clone https://github.com/Tomato-1101/voicekey.git
cd voicekey

# 2. ffmpeg（未インストールなら）
which ffmpeg || brew install ffmpeg

# 3. venv 作成 & 依存関係インストール
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. 設定ファイルの雛形をコピー（API キーは後で UI から入力）
cp -n settings.example.yaml settings.yaml

# 5. 起動
./run.sh
# もしくは: python run.py
```

初回起動後、macOS の **システム設定 → プライバシーとセキュリティ** で voicekey（または Python）に以下 3 つの権限を付与する：

- **入力監視**（グローバルホットキー）
- **アクセシビリティ**（テキスト挿入）
- **マイク**（音声録音）

### Windows 完全セットアップ（PowerShell）

```powershell
# 1. クローン
cd $HOME\Project   # 任意のディレクトリ
git clone https://github.com/Tomato-1101/voicekey.git
cd voicekey

# 2. ffmpeg は https://ffmpeg.org/download.html から導入し PATH を通す
ffmpeg -version  # 動作確認

# 3. venv 作成 & 依存関係
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

# 4. 設定ファイル
Copy-Item -Path settings.example.yaml -Destination settings.yaml -ErrorAction SilentlyContinue

# 5. 起動
.\run.bat
# もしくは: python run.py
```

### API キーの設定（起動後）

文字起こしは **Groq / OpenAI のどちらか（または両方）** のクラウド API で行うため、API キーが必要：

| サービス | 取得先 | 無料枠 |
|---|---|---|
| Groq | https://console.groq.com/keys | あり（レート制限あり） |
| OpenAI | https://platform.openai.com/api-keys | なし（事前課金） |

**設定方法（推奨）**: トレイ/メニューバーのアイコン → 設定ウィンドウ → API キーフィールドに貼り付けて保存。
キーは **macOS Keychain / Windows Credential Manager** に安全に保存される（`.env` や YAML に書く必要なし）。

> **AI への補足**: API キーは取得・入力ともユーザー本人の操作が必要。AI はこの工程をスキップして「ここで API キーを入力してください」と案内すれば良い。

### よくあるつまずきポイント（AI が事前に説明すべき項目）

1. **macOS で初回起動時に何も反応しない** → 「入力監視」「アクセシビリティ」権限を付与した後、アプリを **完全終了 → 再起動** する必要がある（権限変更は再起動で反映）
2. **`python` が見つからない** → macOS では `python3` を使う。`python` コマンドは旧 Python 2 を指している可能性
3. **`pip install` が遅い／タイムアウトする** → `torch` / `torchaudio` のダウンロードが重いため。`pip install -r requirements.txt --progress-bar on` で進捗確認
4. **GPU は不要** → 文字起こしはクラウド API。ローカル GPU を要求するセットアップ手順は古い情報（v1 系の名残）
5. **管理者権限で動いているアプリにテキストが入らない（Windows）** → voicekey 側も管理者権限で起動する必要がある

---

## 💻 必要環境

| 項目 | 要件 |
|---|---|
| **OS** | Windows 10/11 または macOS 11+ |
| **Python** | 3.10 以上 |
| **ffmpeg** | `PATH` に通っていること（音声変換用） |
| **GPU** | **不要**（VAD は CPU でも動く。Apple Silicon は MPS、NVIDIA は CUDA を自動検出） |
| **API キー** | OpenAI または Groq のいずれか（両方併用も可） |

> **💡 Tip**: 文字起こしはすべてクラウド API なので、GPU 非搭載 PC でも動作します。

---

## 📦 インストール

### 0. ポータル経由で配布物をダウンロードする場合

[myprojects-portal](https://myprojects-portal.vercel.app) のカードから OS を選んで
`voicekey-mac.dmg` / `voicekey-windows.zip` を取得できます（認証不要）。

配布物は GitHub Actions が `tag v*` プッシュをトリガーに macOS / Windows でクロスビルドして
GitHub Releases にアップロードしています（`.github/workflows/release.yml`）。
ポータルのダウンロードボタンは Releases の `latest/download/...` URL を直リンクするだけなので、
voicekey 側にサーバーやパスワードは存在しません。

リリース手順：

```bash
git tag v1.0.0
git push origin v1.0.0
# GitHub Actions が macos-latest / windows-latest でビルドし、
# voicekey-mac.dmg / voicekey-windows.zip を Releases に自動アップロード
```

### 1. リポジトリをクローン（開発者向け）

```bash
git clone https://github.com/Tomato-1101/voicekey.git
cd voicekey
```

### 2. 仮想環境を作成・有効化

**Windows (PowerShell)**:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**macOS / Linux**:

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. 依存関係をインストール

```bash
pip install -r requirements.txt
```

> `torch` / `torchaudio` は両 OS とも標準 wheel が使えます。Apple Silicon は MPS、NVIDIA GPU 搭載機は自動的に CUDA を利用します（VAD のみ）。

### 4. ffmpeg の確認

```bash
ffmpeg -version
```

未インストールの場合：

- **Windows**: [ffmpeg.org](https://ffmpeg.org/download.html) からダウンロードして `PATH` に追加
- **macOS**: `brew install ffmpeg`

### 5. (macOS のみ) 権限を許可

初回起動時、macOS が以下の権限を要求します。**システム設定 → プライバシーとセキュリティ** で許可してください：

- **入力監視 (Input Monitoring)**: グローバルホットキー検出のため
- **アクセシビリティ (Accessibility)**: テキスト挿入のため
- **マイク**: 音声録音のため

---

## 🚀 クイックスタート

### 起動方法

**OS 別ランチャー**:

```bash
# Windows
run.bat

# macOS / Linux
./run.sh
```

**または直接 Python**:

```bash
python run.py
```

### 基本的な使い方

1. システムトレイ（Windows）/ メニューバー（macOS）にアイコンが表示される
2. ホットキーを押す（既定: `<f2>` / `<f3>`）
3. 音声入力する
4. ホットキーを離す（`hold` モード）またはもう一度押す（`toggle` モード）
5. 文字起こし結果がアクティブウィンドウに自動入力される

#### ハンズフリー（トグル）録音

設定で「ハンズフリー切替キー」（例: `<cmd_r>`）を 1 つ決めておくと、
**切替キーを押しながらホットキーを押す**だけで、そのプロバイダーがトグル録音
（1 回で開始・もう一度で停止。キーを押しっぱなしにしなくてよい）になります。
切替キーなしで押せば従来どおりのモード（通常は長押し）です。
※ 先に切替キー（修飾キー）を押してからホットキーを押してください。

**止め方**: 録音を終えるときは**同じホットキーをもう一度押すだけ**です（切替キーは押さなくてよい）。
Mac 版では、ハンズフリー録音中は HUD がティール色の「ハンズフリー」表示になり、
「もう一度押すと停止」のヒントが出るので、いまトグル録音中だと一目で分かります。

### 設定を変更する

トレイアイコンをクリックして設定ウィンドウを開き、各種パラメータを変更後「**Save Settings**」を押す。

---

## 🎯 デュアルホットキー機能

voicekey は **2 つの独立したホットキー** を設定でき、各ホットキーに異なるモードを割り当てられます。

> **文字起こしモードは 2 種類**: 設定画面では「**高速リアルタイム**」と「**正確性**」の 2 つから選びます。
> モデルの選択は不要で、それぞれ最適なモデルを自動で使います。整形（読みやすく整える処理）は
> 裏で自動的に行われ、ホットキーごとにオン/オフできます。

### 使用例

**例 1: 速さと正確さの使い分け**

| スロット | ホットキー | モード | 用途 |
|---|---|---|---|
| ホットキー 1 | `<f2>` (toggle) | 高速リアルタイム | 日常のメモ・チャット（話しながら表示） |
| ホットキー 2 | `<f3>` (toggle) | 正確性 | 重要な議事録・正確さ重視 |

**例 2: 言語切替**

| スロット | ホットキー | プロンプト | 用途 |
|---|---|---|---|
| ホットキー 1 | `<ctrl>+<space>` | `Language: Japanese` | 日本語入力 |
| ホットキー 2 | `<alt>+<space>` | `Language: English` | 英語入力 |

### モード比較

| モード | 速度 | 特徴 |
|---|---|---|
| **高速リアルタイム** | **最速** | 話しながら文字が表示され、離した瞬間に確定。チャット・メモ向き |
| **正確性** | 標準 | 録音後にまとめて変換。長文や正確さ重視の用途向き |

> 無音スキップ・整形などの処理は常に最適な設定で自動的に行われます（設定不要）。

---

## ⚙️ 設定方法

### GUI 設定（推奨）

トレイアイコンをクリックして設定ウィンドウを開く。

#### 📍 General ページ

ホットキー 1 / 2 をそれぞれ：

- **Shortcut**: ホットキー文字列（例: `<f2>`, `<shift_r>`, `<cmd_r>`, `<ctrl>+<space>`）
- **Mode**: `hold` / `toggle`
- **バックエンド**: **高速リアルタイム** / **正確性**（モデルは自動。選択不要）
- **Prompt**: ヒントテキスト（任意）
- **テキスト整形（LLM）**: 結果を読みやすく自動整形するか（オン/オフ）

共通：

- **Language**: 言語コード（`ja`, `en` 等）
- **ハンズフリー切替キー**: このキー＋ホットキーでトグル録音にする修飾キー（空で無効）

> **JIS（日本語）配列**: Shortcut には記号キー（`-` `=` `[` `;` `/` など）や
> 日本語専用キー（`¥` / `かな` / `英数` / `無変換` / `変換` / `半角全角`）も指定できます。
> ただし環境・IME によっては一部の日本語キーがアプリに届かず使えない場合があります。

#### 📍 Advanced ページ

- **Auto Enter Delay**: ダブルタップ Auto-Enter で Enter を打つまでの待機（0〜500ms）。この設定値が実際の待ち時間そのものです（以前は内部に約 0.4〜0.5 秒の固定遅延があり設定を変えても速くなりませんでしたが、2026-06-16 に撤廃。文字入力後ほぼ即時に Enter を送ります）
- **ハンズフリー切替キー**: このキー＋ホットキーでトグル録音にする修飾キー（空で無効）
- **Input Device**: 入力マイクデバイス

> **常に最適な設定で自動動作（設定不要）**: 以下は設定 UI に出さず、常に有効で動作します。
> - **無音スキップ**: 無音部分は変換をスキップ。
> - **長文の分割並列送信**: 長い録音を無音区間で分割し並列送信して待ち時間を短縮。
> - **リアルタイム表示**: 「高速リアルタイム」モードで話しながら文字を表示。
> - **録音中の状態表示（HUD）**。
> - **音量正規化**（Windows）: 録音音声の音量を正規化。

#### 📍 バージョン情報ページ

- **現在のバージョン**: いま動いているアプリのバージョン。
- **アップデートを確認**: 押すと最新版をその場でチェックします（「最新です」/「新しいバージョン X が利用可能です」を表示）。
- **今すぐ更新する**: 新しいバージョンが見つかったときだけ表示されます。押すと自動でダウンロード・インストールしてアプリを再起動します（Mac は Sparkle の更新ダイアログ）。
- 自動チェックは起動時と 1 日ごとに走るので、通常はこのタブを開かなくても新版を知らせます。

#### 📍 ユーザー辞書ページ

- **確定置換ルール**: 「変換元」を「変換先」に置き換えるルールを登録できます（例: よく誤変換される固有名詞・専門用語の修正）。
- **追加 / 削除 / 有効化トグル**: 行ごとに追加・削除でき、チェックを外すと一時的に無効化できます。
- 置換は**文字起こし・整形が終わった文章を貼り付ける直前**にローカルで行います（部分一致・登録順・API を通さないので音声入力に待ちを足しません）。

### 設定ファイル（上級者向け）

`settings.yaml` を直接編集できます：

```yaml
# グローバル設定
language: ja
vad_filter: true
vad_min_silence_duration_ms: 500
audio_input_device: default
auto_enter_delay_ms: 50

# 音声前処理（API 送信前）
audio_preprocess:
  volume_normalize: true   # Peak+RMS ハイブリッド正規化

# ホットキー 1
hotkey1:
  hotkey: <shift_r>
  hotkey_mode: hold
  backend: openai
  api_model: gpt-4o-mini-transcribe
  api_prompt: ""

# ホットキー 2
hotkey2:
  hotkey: <f2>
  hotkey_mode: toggle
  backend: groq
  api_model: whisper-large-v3-turbo
  api_prompt: ""

# 起動時 VAD プリロード（初回文字起こし高速化）
preload_on_startup: true

# その他
dark_mode: false
dev_mode: false
```

> **🔄 ホットリロード**: `settings.yaml` を保存すると自動反映（再起動不要）。

---

## 🔑 API 設定

### API キーの設定

プロジェクトルートに `.env` を作成：

```env
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxx
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

### API キーの取得

| サービス | 料金 | 取得先 |
|---|---|---|
| **Groq** | 無料（レート制限あり） | [console.groq.com/keys](https://console.groq.com/keys) |
| **OpenAI** | 有料（事前課金） | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |

OpenAI 課金: [platform.openai.com/account/billing](https://platform.openai.com/account/billing)
料金詳細: [openai.com/pricing](https://openai.com/pricing)

---

## 🎚️ 音声前処理

API に送る前に、録音音声を **音量正規化（Peak+RMS ハイブリッド方式）** で整えます。

| 項目 | 値 |
|---|---|
| 目標 RMS | -20 dBFS（人声に適した一般値） |
| ピーク上限 | -3 dBFS（音割れ防止のヘッドルーム） |
| 処理時間 | numpy のみで <1ms（5 秒音声でも誤差レベル） |

### 効果

- **小声で録音した音声**：API 文字起こしに十分なゲインまで持ち上げる
- **大声・近距離マイク**：ピーク上限で抑え込み、クリッピングによる文字化けを防ぐ
- **完全無音**：そのまま透過（ゲイン発散の防止）

ノイズ対策（ファン音・背景雑音等）は API モデル（Whisper）側が十分に行うため、ローカルでのノイズリダクションは実装していません。

設定ウィンドウの **Advanced → Audio Preprocess** で ON/OFF を切替可能（既定 ON）。

---

## 🔧 トラブルシューティング

### テキストが入力されない

**Windows**:

- 入力先アプリが管理者権限なら、voicekey も管理者権限で実行する。
- 他のアプリのホットキーと競合していないか確認。

**macOS**:

- システム設定 → プライバシーとセキュリティ → **アクセシビリティ** に voicekey（または Python）を追加。
- 同じく **入力監視** にも追加。

### ホットキーが反応しない

- 他アプリ（IME、ランチャー等）と競合している可能性。設定で別キーへ。
- macOS では **入力監視** 権限を確認。

### マイクが認識されない

- 設定 → Advanced → Input Device で明示的にデバイスを選択。
- 設定の「自動検出」ボタンを押し、**マイクに向かって数秒喋り続ける**と、声が入っているマイクを自動で選びます（Windows では一部のドライバ方式（WDM-KS / ASIO）は安全のため自動検出の対象外）。
- macOS では **マイク** 権限を確認。

### "ご視聴ありがとうございました" 等のハルシネーション

- VAD を有効化（既定で ON）。
- VAD Min Silence を短く（例: 300ms）してこまめに無音区間で区切る。

### API 接続エラー

- 設定ウィンドウの API キーが正しいか確認（Keychain / Credential Manager に保存される）。
- ネットワーク接続を確認。
- レート制限超過の可能性（Groq の場合）。

### macOS で録音中・停止後にアプリがフリーズする

- v1.x の PortAudio スレッド競合に起因する既知のフリーズは v9 (`9c50056`) で根治済み。
- それでも刺さる場合：トレイ/メニューバーから **Force Reset** を選択すると録音セッションを強制リセットできる。

### WinError 1314 (Windows のみ)

- Symbolic Link Privilege のエラー。`huggingface_hub` がモデルキャッシュ作成時に失敗する場合に発生。
- ユーザーディレクトリ（既定）以外を使う場合は環境変数 `HF_HOME` を設定して書き込み可能なパスに。

---

## 👨‍💻 開発者向け情報

### プロジェクト構造

```
voicekey/
├── src/
│   ├── app.py                    # メインアプリ（HotkeySlot 管理 / キュー処理）
│   ├── main.py                   # エントリポイント
│   ├── config/
│   │   ├── types.py              # 型定義（HotkeySlotConfig, TranscriptionTask 等）
│   │   ├── constants.py          # DEFAULT_CONFIG
│   │   └── config_manager.py     # YAML 読込・マイグレーション・hot-reload
│   ├── core/
│   │   ├── audio_recorder.py     # sounddevice ベースの録音
│   │   ├── audio_preprocess.py   # 音量正規化（Peak+RMS）
│   │   ├── audio_utils.py        # WAV / MP3 変換
│   │   ├── vad.py                # silero-vad ローカル VAD
│   │   ├── groq_transcriber.py   # Groq API クライアント
│   │   ├── openai_transcriber.py # OpenAI API クライアント
│   │   └── input_handler.py      # クリップボード経由のテキスト挿入
│   ├── platform/                 # OS 抽象化層
│   │   ├── base.py               # PlatformAdapter 抽象クラス
│   │   ├── factory.py            # sys.platform で実装を選択
│   │   ├── common/keymap.py      # 共通キー正規化
│   │   ├── macos/adapter.py      # Cmd 系修飾キー、メニューバー挙動
│   │   └── windows/adapter.py    # Ctrl 系修飾キー、トレイ挙動
│   ├── ui/
│   │   ├── overlay.py            # Dynamic Island 風オーバーレイ
│   │   ├── settings_window.py    # 設定ウィンドウ
│   │   ├── styles.py             # macOS 風テーマ
│   │   └── system_tray.py        # システムトレイ / メニューバー
│   └── utils/logger.py
├── docs/
│   ├── CROSS_PLATFORM_UNIFICATION_PLAN.md
│   └── CROSS_PLATFORM_TEST_CHECKLIST.md
├── run.py / run.bat / run.sh     # 起動エントリ
├── settings.yaml                 # 設定ファイル
├── voicekey.spec               # PyInstaller spec
├── requirements.txt
├── CHANGELOG.md
└── CLAUDE.md                     # AI 開発者向けガイド
```

### 開発者モード

`settings.yaml` で `dev_mode: true` を設定すると：

- 出力テキストが引用符で囲まれる
- `dev_timing.log` にタイミング情報を記録

### ビルド

```bash
pyinstaller voicekey.spec --clean --noconfirm
```

実行ファイルは `dist/voicekey/` に生成されます（One-Dir モード）。

### 変更履歴

詳細は [CHANGELOG.md](CHANGELOG.md) を参照。

### コントリビューション

1. Fork して feature ブランチを作成（`git checkout -b feature/xxx`）
2. CHANGELOG.md に変更を記録
3. コミット（`git commit -m 'feat: ...'`）
4. Push（`git push origin feature/xxx`）
5. Pull Request を開く

詳細は [CONTRIBUTING.md](CONTRIBUTING.md) を参照。

---

## 📄 ライセンス

GNU General Public License v3.0 — 詳細は [LICENSE](LICENSE) を参照。

---

## 🙏 謝辞

このプロジェクトは以下のオープンソースを使用しています：

- [Silero VAD](https://github.com/snakers4/silero-vad) — ローカル VAD
- [PySide6](https://wiki.qt.io/Qt_for_Python) — GUI フレームワーク
- [pynput](https://github.com/moses-palmer/pynput) — グローバルキーボード制御
- [sounddevice](https://python-sounddevice.readthedocs.io/) — マイク入力
- [OpenAI API](https://platform.openai.com/) — gpt-4o-transcribe
- [Groq API](https://console.groq.com/) — whisper-large-v3-turbo

---

<div align="center">

**⭐ このプロジェクトが役に立ったら、スターをお願いします**

Made by [Tomato-1101](https://github.com/Tomato-1101)

</div>
