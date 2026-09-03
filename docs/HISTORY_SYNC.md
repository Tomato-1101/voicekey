# 履歴同期（Mac ⇄ Windows）

Mac と Windows で音声入力の履歴を共有する機能。**Cloudflare Workers + D1** の 1 Worker・1 テーブル構成で、
開発者本人専用（トークン 1 本を両端末で共有するだけ・ユーザー管理やログインは無い）。

**現在地（2026-09-03）**: Worker・Windows クライアント・Mac クライアントを実装済み。
Mac は `URLProtocol` モックで POST / GET、Windows 形式の受信、統合表示を自動テストしている。

---

## 1. 目的

これまで音声入力の履歴（`history.json`）は各端末のローカルにしか無く、Mac で入力した内容を
Windows から見る手段が無かった。本機能は両端末の履歴を 1 つの D1 テーブルへ送受信し、
履歴ページ上で「自分のディクテーション履歴」として統合表示する。

削除やアプリ設定の同期は対象外（§10「制限」）。あくまで**履歴の閲覧共有**が目的。

## 2. 構成

```
┌────────────┐         HTTPS/Bearer          ┌──────────────────┐
│  Mac 版      │ ─────────────────────────▶  │                    │
│ (実装済み)   │ ◀─────────────────────────  │  Cloudflare Worker │
└────────────┘                               │  (sync-worker/)    │
                                              │  GET  /health      │
┌────────────┐         HTTPS/Bearer          │  POST /history     │──▶ D1: history テーブル
│ Windows 版   │ ─────────────────────────▶  │  GET  /history      │
│ (実装済み)   │ ◀─────────────────────────  │                    │
└────────────┘                               └──────────────────┘
```

Worker は 1 つ、D1 データベースも 1 つ（`voicekey-history`）。認証は共有 Bearer トークン 1 本のみで、
ユーザーアカウントや端末ごとの ID 発行は無い。`sync-worker/` の実装詳細は `sync-worker/README.md` を参照。

## 3. API 仕様

### `GET /health`

認証不要。疎通確認用。

```
200 OK
{"ok": true}
```

### 認証（`/history` 系エンドポイント共通）

`Authorization: Bearer <SYNC_TOKEN>` ヘッダーが必須。

| 状況 | レスポンス |
|---|---|
| ヘッダー無し／トークン不一致 | `401 {"error": "unauthorized"}` |
| Worker 側に `SYNC_TOKEN` シークレットが未設定 | `503 {"error": "token_not_configured"}` |

### `POST /history`

履歴エントリをアップロードする。1 回のリクエストで 1〜200 件。

**リクエストボディ**:

```json
{
  "items": [
    {
      "id": "<uuid、または 64 文字以下の任意文字列>",
      "text": "文字起こし結果の本文",
      "date": "2026-09-03T12:34:56.789Z",
      "device": "windows",
      "app_name": null,
      "characters": 12
    }
  ]
}
```

- `date` はクライアント側の時計による ISO 8601 文字列。
- `device` は `"windows"` または `"mac"`。
- `app_name` は貼り付け先アプリ名（無ければ `null`）。

**レスポンス**:

```json
200 OK
{"accepted": 1, "received_at": "2026-09-03T12:34:57.001Z"}
```

`id` を主キーに `INSERT OR IGNORE` するため、**同じ `id` を再送しても重複登録されない**。
ネットワーク失敗後の再送信は安全（冪等）。

### `GET /history?since=<received_at>&limit=<1..500>`

サーバーに蓄積された履歴を取得する。

- `since`: 省略可。指定すると `received_at > since` のものだけを返す（**サーバー側の受信時刻を
  カーソルにする**。クライアントの時計は信用しない）。
- `limit`: 省略可。既定 200、最大 500。

**レスポンス**:

```json
200 OK
{
  "items": [
    {
      "id": "...", "text": "...", "date": "...",
      "device": "mac", "app_name": "Slack",
      "characters": 8, "received_at": "2026-09-03T12:30:00.000Z"
    }
  ]
}
```

`received_at` の降順（新しい順）で返る。

## 4. D1 スキーマ

```sql
CREATE TABLE history (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  date TEXT NOT NULL,
  device TEXT NOT NULL,
  app_name TEXT,
  characters INTEGER NOT NULL DEFAULT 0,
  received_at TEXT NOT NULL
);
-- received_at に索引（since フィルタ・降順取得の高速化）
```

実体は `sync-worker/schema.sql`。

## 5. トークンの扱い

**このリポジトリは public なので、実際の Worker URL・アカウントのサブドメイン・トークンは
このファイルを含むどのドキュメントにも書かない。** 本書では常にプレースホルダー
`https://voicekey-history-sync.<subdomain>.workers.dev` を使う。実際の URL とトークンは:

- **Windows**: 資格情報マネージャーに `voicekey.SyncToken` として保存（設定 → 履歴 → カードから登録）。
  Mac への持ち出し用に `%LOCALAPPDATA%\voicekey\sync_token.txt`（リポジトリの外・git 管理外）にも平文コピーを置く。
- **Mac**: Keychain の `voicekey.SyncToken` へ保存（設定 → 一般 → 履歴の「共有トークン」）、または中央 Keychain
  （service=`VOICEKEY_SYNC_TOKEN` / account=`shared`。2026-09-04 からの現運用。`security add-generic-password -U -s VOICEKEY_SYNC_TOKEN -a shared -w`）。読み取りは
  アプリ Keychain → 中央 Keychain `VOICEKEY_SYNC_TOKEN` → 同名の環境変数の順。値は UI・ログへ出さない。
  Windows の `sync_token.txt` の中身を手動でコピー＆ペーストする。

トークンは Python の `secrets.token_urlsafe(32)` で生成した。**git へは絶対にコミットしない**
（`sync-worker/.dev.vars` は gitignore 対象）。

## 6. Cloudflare 側の構築手順

### 初回構築（済み・記録として残す）

```
npx wrangler login
npx wrangler d1 create voicekey-history          # database_id を wrangler.toml に反映（これ自体は秘密ではない）
npx wrangler d1 execute voicekey-history --remote --file=schema.sql
npx wrangler deploy
npx wrangler secret put SYNC_TOKEN < token-file   # トークンは画面に打たず、コミットもしない
```

### 再デプロイ（コード変更時）

```
npx wrangler deploy
```

`SYNC_TOKEN` はシークレットとして Cloudflare 側に保持されるため、コード変更だけの再デプロイでは
再設定不要。

### トークンのローテーション

1. 新トークンを生成（`python -c "import secrets; print(secrets.token_urlsafe(32))"`）。
2. `npx wrangler secret put SYNC_TOKEN < 新token-file` で Worker 側を更新。
3. **両端末**の設定で古いトークンを新しいものに保存し直す（Windows は設定 → 履歴 → カードの
   「共有トークン」欄、Mac は Keychain の対応する項目）。
4. `%LOCALAPPDATA%\voicekey\sync_token.txt` の中身も新トークンに置き換える。
5. ローテーション直後は両端末とも `401 トークンが無効です` の警告が一度出るのが正常
   （更新前の状態で最後にリトライしたぶん）。設定保存後は自動的に再開する。

## 7. Mac / Windows での設定手順

### Mac

1. メニューバーアイコン → 設定 → 一般 → 履歴。
2. 「Windows と履歴を共有」をオンにする。
3. 「同期サーバー URL」に `https://voicekey-history-sync.<subdomain>.workers.dev` を入力する。
4. Windows の `%LOCALAPPDATA%\voicekey\sync_token.txt` の中身を「共有トークン」へ貼り付けて保存する。
5. 「送信待ち n 件」「最終同期 hh:mm」を確認する。401 の案内が出た場合はトークンを保存し直す。

URL は https 必須（ローカル開発時だけ `http://127.0.0.1` / `http://localhost` を許可）。
ホームとサイドノッチでは Windows 由来の履歴に `[Windows]` が付く。

### Windows

1. トレイアイコン → 設定ウィンドウ → 「履歴」ページ。
2. 上部のカード「Mac と履歴を共有」を開く。
3. トグルを ON にし、「同期サーバー URL」に Worker の URL（`https://voicekey-history-sync.<subdomain>.workers.dev`）、
   「共有トークン」に `sync_token.txt` の中身を貼り付けて保存。
4. カード下部のステータス行に「送信待ち n 件」「最終同期 hh:mm」が出れば動作中。トークンが誤っていれば
   「トークンが無効です…」が出る（この場合は設定を保存し直すまで再試行しない）。

履歴ページ・ホームの直近履歴には自端末とクラウド経由の履歴がマージ表示され（最大 200 件・日時降順）、
他端末からの行には `[Mac]` の接頭辞が付く。

## 8. 動作確認

```powershell
venv\Scripts\python.exe scripts\sync\check_sync.py [url]
```

- トークンは Credential Manager から読むだけで**画面には出さない**。
- `/health` が 200 を返すこと。
- 無効なトークンで `/history` を叩くと 401 になること。
- 保存済みの実トークンで叩くと 200 になること。
- テスト用に 1 件 `POST /history` し、`accepted` が返ること。
- 直後の `GET /history` に送ったばかりの 1 件が含まれること。

`url` を省略すると設定済みの同期サーバー URL を使う。

行動ログ（`%LOCALAPPDATA%\voicekey\logs\app.log`）には以下の行が出る:

- 起動時: `履歴同期: 有効 (<url>)` または `履歴同期: 無効`
- 同期のたびに: `履歴同期要求 (送信 n 件)` → `履歴同期完了 (送信 n 件, 受信 m 件)`

## 9. 障害時の見方

| 症状 | 原因 | 挙動 |
|---|---|---|
| ステータス行が「トークンが無効です…」 | サーバー側が 401 を返した（トークン不一致、またはローテーション直後） | それ以降は設定を保存し直すまで送受信を試みない。ログに WARNING が 1 回だけ出る |
| ステータス行の「送信待ち n 件」が増え続ける | オフライン、または Worker 側の障害（`503 token_not_configured` 含む） | キューは失われない。10 秒→5 分の指数バックオフで再試行を続ける |
| `sync_outbox.json` が肥大化している | 長時間オフラインだった、または上記の 401/オフラインが続いていた | 接続が回復すれば 200 件ずつのバッチで自動的に送り切る。手動での削除は不要（削除すると未送信分が失われる） |
| クラウド側の履歴が更新されない | `sync_cloud_cache.json` のカーソル（`since`）が進んでいない、または同期スレッドが 401 で停止中 | 上の「トークンが無効です」に該当しないか確認し、設定を保存し直す |

`sync_outbox.json` / `sync_cloud_cache.json` は `settings.yaml` / `history.json` と同じ場所
（インストール版は `%LOCALAPPDATA%\Programs\voicekey\`）に置かれる。

## 10. 制限

- 履歴ページの表示は**最大 200 件**（自端末＋クラウド経由の合算）。
- **削除は同期しない**。片方の端末でローカル履歴を消去しても、もう一方やサーバー側の該当エントリは消えない。
- 「履歴を消去」操作は**常にローカルのみ**に効く（サーバー側の該当データは残る）。
- 認証はトークン 1 本の共有のみで、端末ごとのアクセス制御や監査ログは無い（開発者本人専用の設計のため）。
