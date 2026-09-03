# voicekey-history-sync

Mac と Windows の voicekey クライアント間で口述履歴を共有するための Cloudflare Worker。
D1（SQLite）に履歴を1テーブルで保存し、各クライアントが新規エントリを送信（POST）・
他端末のエントリを取得（GET）する。認証は全端末共通の Bearer トークン1本。

## エンドポイント

### `GET /health`

認証不要の疎通確認用。トークン設定ミスとネットワーク到達性の問題を切り分けるために用意。

```bash
curl https://voicekey-history-sync.<subdomain>.workers.dev/health
# => {"ok":true}
```

### `POST /history`

新規履歴エントリをまとめて送信する。1リクエストにつき1〜200件。
既に同じ `id` が存在する場合は無視される（べき等なので、送信失敗時の再送も安全）。

```bash
curl -X POST https://voicekey-history-sync.<subdomain>.workers.dev/history \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {
        "id": "uuid-1234",
        "text": "こんにちは",
        "date": "2026-09-02T10:00:00+09:00",
        "device": "windows-desktop",
        "app_name": "Slack",
        "characters": 5
      }
    ]
  }'
# => {"accepted":1,"received_at":"2026-09-02T01:00:00.000Z"}
```

### `GET /history?since=<ISO>&limit=<n>`

他端末で受信した履歴を取得する。`since` は前回同期時に受け取った `received_at`
（サーバ側の受信時刻）を渡す差分取得用カーソル。省略時は全件（`limit` 件まで）。
`limit` は 1〜500、省略時 200。

```bash
curl "https://voicekey-history-sync.<subdomain>.workers.dev/history?since=2026-09-02T00:00:00.000Z&limit=100" \
  -H "Authorization: Bearer $TOKEN"
# => {"items":[{"id":"uuid-1234","text":"こんにちは","date":"...","device":"windows-desktop","app_name":"Slack","characters":5,"received_at":"2026-09-02T01:00:00.000Z"}, ...]}
```

## D1 スキーマ

`schema.sql` 参照。

- `date`: クライアント側での口述時刻（送信された ISO 8601 文字列をそのまま保存）
- `received_at`: サーバ側で記録した受信時刻（ISO 8601 UTC）。クライアント間で
  時計がずれていても差分同期の基準にできるよう、こちらを同期カーソルに使う。

## デプロイ手順

```bash
npx wrangler d1 create voicekey-history          # database_id を wrangler.toml へ
npx wrangler d1 execute voicekey-history --remote --file=schema.sql
npx wrangler deploy
npx wrangler secret put SYNC_TOKEN                # トークンは stdin から渡す。ファイル・コミットに残さない
```

## トークンの保管

トークンは全端末共通の1本。各クライアントでは OS の資格情報ストアに保存する。

- Windows: 資格情報マネージャー（`voicekey.SyncToken`）
- macOS: キーチェーン
