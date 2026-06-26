# voicekey 無料ローンチ（アクティベーションキー方式）— 現状と残作業まとめ

作成: 2026-06-26
目的: **課金なし・アクティベーションキーだけで誰でも使える**状態を最短で出す。
（このファイルは長いセッションが切れても残るための引き継ぎメモ。次セッションはまずこれを読む。）

---

## 0. 今回の方針（ユーザー指示・2026-06-26）

- **Stripe／月額課金は一旦なし**（コードは残すだけ・消さない）。
- まず**アクティベーションキーだけで無料で使える版**を最短で出し、早くみんなに使ってもらう。
- お金を払わなくても、配ったキーで使える状態を最優先。
- アプリのブランチ指定 = 「両方(main 先行→release へ同一コミット)」と回答 → ただし §3-A の注意あり。
- メール／SMTP まわりは**別セッション担当**（このまとめの対象外。`voicekey-site` の URL 設定は適用済み・メールは標準テンプレのままローンチ可）。

---

## 1. 現状（2026-06-26 実機確認済み）

### サーバー（voicekey-site）= 100% 実装・本番稼働中
- 認証: `/api/v1/auth/exchange`（ワンタイムコード→セッション）, `/auth/refresh`, `/auth/ephemeral`（Deepgram 短命 JWT 発行）
- アクティベーション: `/api/v1/activation/redeem`（キー消費→entitlement）, `/api/v1/admin/activation/create`（管理発行）
- プロキシ: `/api/v1/transcribe/elevenlabs`, `/api/v1/format`（Groq 整形）
- DB: マイグレーション 14 本（profiles / entitlements / activation_keys / devices / usage_logs / token_grants / login_codes ほか・全 RLS 有効）
- 課金(Stripe)も実装済みだが**今回は使わない**。
- URL 設定済み・メールは Supabase 標準のままでも認証は機能。

### アプリ（voicekey）= release で「段階1〜4」実装済み / main は無し
release ブランチに実装済み（両 OS・**既に配布中の v1.2.0 に含まれている**）:
- バックエンド接続基盤＋クライアント（Mac: `BackendClient.swift` `ServerConfig.swift` / Win: `backend_client.py` `secrets.get_server_base_url`）
- 認証クライアント＋ログイン司令塔（`AuthClient` / `LoginCoordinator`、Win: `auth_client.py` `login_coordinator.py` `deep_link.py`）
- ブラウザ経由ログイン UI ＋ URL スキーム受信（`voicekey://`）
- トークン自動更新（refresh 配線）
- **切替ロジック（併存ガード）**: ログイン済みなら短命キー＋プロキシ経路、未ログインなら埋め込みキーにフォールバック。
  - 実体: `Transcriber.swift` の `if BackendClient.isLoggedIn { fetchEphemeralToken() }`。サーバー先 = `EmbeddedKeys.isDist ? https://voicekey.vercel.app : localhost`（`VOICEKEY_SERVER_URL` で上書き可）。

main ブランチ: 上記は**入っていない**（main = 自分用 = 自分の API キー直叩きなので、ログイン／サーバー連携は元々不要）。

---

## 2. 「無料・キーだけで使える」に足りないもの（残作業）

★サーバーは完成、アプリのログイン基盤も完成。残るのはここだけ。

1. **アプリ内アクティベーションキー入力 UI ＋ redeem 配線（未実装・最重要）**
   - ログインしても entitlement が無いと `/auth/ephemeral` は 403。無料キーを入れて entitlement を得る導線が必要。
   - 設定（or 初回画面）に「アクティベーションキーを入力」→ `/api/v1/activation/redeem` 呼び出し → 成功で利用可。両 OS。
   - サーバー側の redeem RPC は完成済みなので、アプリ側に `redeemActivationKey()` を足すだけ。

2. **未契約時の UX（ゲートの強さ）** → §3-B で方針決定
   - 現状は未ログイン→埋め込みキーにフォールバック＝誰でも使えてしまう（ゲートされない）。
   - 「無料だがキーで管理」を成立させるなら、配布版で埋め込みフォールバックを止めて「ログイン＋有効キー必須」にする必要がある。

3. **E2E 検証**: ログイン→無料キー redeem→短命キー取得→文字起こしが**従来レイテンシ(94-109ms)維持**。キー無し/失効で使用不可。

4. **リリース**: 上記を入れた release を両 OS ビルド→配布物配置→（あなたの GO で）`vercel deploy --prod`。版番号は Claude が semver で決定（後方互換の機能追加なので **v1.3.0 想定**）。

---

## 3. ★着手前に決めること（次セッション冒頭で確認）

### A. ブランチ
商品化（ログイン／キー／短命キー）は今まで **release 専用**で進行。main = 自分用は自分の鍵直叩きで不要。
- **推奨: 本作業は release 単独**。Q2 で「両方」と回答いただいたが、main にログイン／アクティベーションを入れる意味が無いので次セッションで再確認したい。両 OS に共通する小修正だけ必要なら main にも入れる。

### B. ゲートの強さ（無料版での埋め込みキー扱い）
- **案1（最速・緩い／推奨）**: 埋め込みキー併存のまま、アクティベーションキー UI を足すだけ。キー無くても動くが「キーを入れた人」を管理・統計できる。一番早く配れる。
- **案2（本来の姿・強い）**: 配布版は埋め込みキー停止＝ログイン＋有効キー必須。突破耐性が出るが、既存ユーザーに強制ログインが要る。
- 推奨: **まず案1 で早く出し**、落ち着いたら案2（埋め込みキー段階廃止）へ。

### C. サインアップの形
- 現状 = ブラウザでアカウント作成(メール)→アプリにキー入力。**サーバー改修ゼロで最速**。
- 「メール登録なしでキーだけ」はサーバー改修が必要＝遅い。→ **現状のアカウント方式で出す**。

---

## 4. 推奨ビルド順（次セッション・各段階で検証）

- **S1**: アプリに `redeemActivationKey()` を追加（BackendClient / backend_client）。サーバー redeem は完成済み。
- **S2**: 設定 UI に「アカウント / ライセンス」項目（ログイン状態＋キー入力欄＋有効期限表示）。両 OS。
- **S3**: 未契約時の導線（ログイン済み & entitlement 無し → キー入力を促す）。
- **S4**:（案2 採用時のみ）埋め込みフォールバック停止フラグ。
- **S5**: E2E 検証 → release 両 OS ビルド → 配布物配置 → GO で vercel 本番。
- 検証: Mac = ビルド&再起動 / Win = `py_compile` + `unittest`。レイテンシ最優先。

---

## 5. 据え置き（今回やらない）
- Stripe 課金 UI・サブスク・価格設定（コードは残す）。
- メールのブランド化 / カスタム SMTP（別セッション）。
- Apple Developer Program / 公証（販売開始時 = HANDOFF の Phase 7）。

---

## 6. 運用メモ
- このセッションは長くなっている。**新しいセッションを開始**し、まずこのファイルを読んでから §3 の A/B/C を確認 → S1 着手を推奨。
- アクティベーションキーの発行は管理画面（`/admin` または `/api/v1/admin/activation/create`）から。無料配布用に有効期間付きキーを必要数発行 → 配る。
- 関連ドキュメント: ベータ配布の現在地は `HANDOFF.md`、全体地図は `OVERVIEW.md`、商品化計画の全文は `~/.claude/plans/web-ui-stripe-1-toasty-kettle.md`。
