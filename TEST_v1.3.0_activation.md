# v1.3.0 アクティベーション動作テスト手順

最終更新: 2026-06-26 / 対象: release ブランチ v1.3.0（無料配布＝ログイン＋アクティベーションキー必須）

このバージョンから、**配布ビルドはログインと有効なアクティベーションキーが無いと文字起こしできない**。
埋め込みキー直叩きのフォールバックは配布版では止めてある。サーバー（voicekey.vercel.app）が利用権を検証し、
Deepgram は短命 JWT で直叩き（低レイテンシ維持）、ElevenLabs/Groq はサーバープロキシ。

---

## 0. 事前状態（このセッションで完了済み）

- `/api/v1/me` を本番デプロイ済み（`https://voicekey.vercel.app/api/v1/me` → 未ログインで 401 を確認）。
- アプリ v1.3.0 を両 OS でビルド済み・配布物を配置済み。
- **更新フィードは v1.2.0 のまま据え置き**＝既存ユーザーは自動更新されない（v1.3.0 は手動取得のみ）。

## 1. 無料アクティベーションキーを発行する（あなた＝管理者の作業）

1. ブラウザで `https://voicekey.vercel.app/admin/keys` を開く（`profiles.is_admin = true` のアカウントでログイン）。
2. 期間を選んでキーを発行（例: 30 日 / 365 日）。
3. **発行直後に一度だけ表示される平文キーをコピー**しておく（ハッシュ保存のため後から再表示できない）。

## 2. v1.3.0 アプリを入手する

- **Mac**: `https://voicekey.vercel.app/downloads/voicekey-1.3.0.dmg`
  （ローカルビルドでも可: `macos/dist/voicekey-1.3.0.dmg`）
  - Apple Development 署名・公証なし。初回は右クリック →「開く」→「開く」、または
    システム設定 → プライバシーとセキュリティ →「このまま開く」。
- **Windows**: `https://github.com/Tomato-1101/voicekey-releases/releases/download/v1.3.0/voicekey-1.3.0-setup.exe`
  - 未署名のため初回 SmartScreen は「詳細情報」→「実行」。

## 3. ログイン → キー登録（アカウントに紐付く）

1. アプリの 設定 →「アカウント」タブを開く。
2. ログインボタン → ブラウザが開く → メール/パスワードでログイン →
   `voicekey://auth?...` でアプリに戻る → 「ログイン済み（メールアドレス）」表示になる。
3. 「ライセンス（アクティベーションキー）」欄に手順 1 のキーを貼り付け →「登録」。
4. 状態が **「有効（期限: YYYY/MM/DD）」** に変われば成功。
   - 利用権はアカウント単位（`redeemed_by`）。**別の端末で同じアカウントにログインすれば再登録不要**で使える。

## 4. 文字起こしが動くことを確認（本命）

- ホットキーで録音 → 文字起こし → アクティブウィンドウへ貼り付け。
- 「高速リアルタイム（Deepgram）」で **体感の待ちが無い（実測 94〜109ms 水準）**ことを確認。
  → サーバーが短命 JWT を発行し、Deepgram を直叩きできている証拠。
- 「正確性（ElevenLabs）」も試す → サーバープロキシ経由で結果が返る。

## 5. ゲートが効くことを確認（ネガティブテスト）

- **キー未登録のアカウント**（ログイン済みだが利用権なし）で文字起こし →
  「利用するにはアクティベーションキーの登録が必要です（設定 → アカウント）」等で**ブロックされる**。
- **未ログイン**でも文字起こしはブロックされる（「ログインとアクティベーションキーが必要です」）。

---

## トラブル時の見方

- アプリが本番を見ているか: 配布(DIST)ビルドは `https://voicekey.vercel.app` を使う（開発ビルドは localhost:3000）。
- サーバー側ログ: Vercel のデプロイ（`voicekey` プロジェクト）ログ、または Supabase のログ。
- キー登録が 400「使用済み/無効」: 既に redeem 済みキー、または期限切れキー。新しいキーを発行して再試行。
- 401 が返る: アプリのログインセッションが切れている → 一度ログアウト → 再ログイン。

## まだ「一般公開」ではない（重要）

現状は **ステージ公開**。一般ユーザーがサイトのダウンロードボタンから入れるのは引き続き v1.2.0（埋め込みキー版）で、
更新フィードも v1.2.0 のまま。理由は「self-serve のキー発行導線が未整備」で、いま公開デフォルトを v1.3.0 にすると
キーを持たない新規ユーザーが使えず、既存ユーザーも自動更新で締め出されるため。

**一般公開（公開デフォルトを v1.3.0 に切替）に必要な残作業**:
1. 新規ユーザーが自分でキーを得る導線（サインアップ時に自動付与 or 「無料キーを発行」ボタン）。
2. 公開 pointer の切替: `voicekey-site/public/downloads.json`・`windows/version.json`・`mac/appcast.xml` を 1.3.0 に更新して `vercel deploy --prod`。
   → これを実行すると既存 v1.2.0 ユーザーも自動更新で v1.3.0（要キー）に上がる＝この時点で全員にキーが要る。
3. 旧埋め込みキーの無効化（親キーローテーション）を同時に計画。

### 切替時にそのまま使える確定値（v1.3.0 はビルド・公開済み）

公開リポ `voicekey-releases` の v1.3.0 リリース（setup.exe 添付・DL 200 確認済み）と
Mac の dmg/zip（voicekey-site に配置済み）はすでに用意できている。フィードを差し替えるだけ。

**`public/windows/version.json` をこの内容に置き換える**（CI が生成・SHA256 実測一致を確認済み）:

```json
{
    "notes":  "voicekey 1.3.0",
    "url":  "https://github.com/Tomato-1101/voicekey-releases/releases/download/v1.3.0/voicekey-1.3.0-setup.exe",
    "version":  "1.3.0",
    "sha256":  "5c7f22cf3a9c1ccab8b711e8d2224edad6d153016fc7bf5a9f9270c702494790"
}
```

**`public/downloads.json`**: mac → `{version:"1.3.0", url:"/downloads/voicekey-1.3.0.dmg", size:1958933}`、
windows → `{version:"1.3.0", url:"https://github.com/Tomato-1101/voicekey-releases/releases/download/v1.3.0/voicekey-1.3.0-setup.exe", size:281011424}`。

**`public/mac/appcast.xml`**: `voicekey-1.3.0.zip`（build 9・配置済み）の item を追記。
Sparkle の EdDSA 署名が要るので、再生成するなら `cd macos && ./scripts/build_dmg.sh --version 1.3.0` の
出力 `dist/releases/appcast.xml` を `public/mac/appcast.xml` にコピーする（今回はコピーしていない）。
