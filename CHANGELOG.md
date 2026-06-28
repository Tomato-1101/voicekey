# Changelog

voicekeyの変更履歴を記録するファイルです。

## [Unreleased]

### Security
- **配布バイナリへの長期プロバイダーキー埋め込みを撤去（release・両OS）**。
  これまで配布版は開発者の OpenAI / Groq / ElevenLabs / Deepgram のキーを XOR 難読化して
  バイナリに焼き込んでいたが、XOR は暗号化ではなく（マスクも同じバイナリに同梱され復元可能）
  漏洩源だった。製品版は文字起こし・整形をすべて自社サーバー経由（Deepgram=短命 JWT 直叩き /
  ElevenLabs・Groq=サーバープロキシ）で行うため、アプリ側にプロバイダーキーは不要。
  配布物に長期キーを **1 バイトも埋め込まない**ようにした。
  - 生成スクリプトをキーレス化: `scripts/build/generate_embedded_keys.py` と
    `macos/scripts/generate_embedded_keys.sh` は IS_DIST フラグだけの「DIST マーカー」を出力し、
    .env.dist / Keychain / 環境変数からキーを読まない（Mac の `--export-env` も撤去）。
  - キー解決の埋め込みフォールバックを撤去: `src/utils/secrets.get_api_key`（Python）と
    Mac `Keychain.apiKey`（Swift）はキーを keyring/Keychain からのみ取得する。
  - テキスト整形の直 Groq フォールバックに DIST ガードを追加（`text_formatter` / `TextFormatter.swift`）。
    配布ビルドは未ログイン時も直プロバイダーを叩かず、整形せず原文を返す（整形はサーバープロキシ専用）。
  - ビルドパイプラインがプロバイダーキーを扱わない: `.github/workflows/windows-build.yml` から
    API キーの Secrets 受け渡しを削除。ビルド時に `scripts/build/verify_no_embedded_keys.py` で
    生成物にキーの痕跡が無いことを検査してから先へ進む（漏洩回帰の防止・Win/Mac 両パイプライン）。
  - **要対応（コード変更とは別）**: v1.2.0 以前の配布物に焼き込まれた旧キーは**漏洩済み**として
    扱い、各プロバイダー側で**ローテーション（無効化・再発行）が必要**。キー値そのものはここでは扱わない。
- **配布版で接続先・認証情報の汚染を防止（`.env` / `VOICEKEY_SERVER_URL` の無効化・両OS）**。
  配布（DIST）ビルドでは `.env` を読み込まず、環境変数 `VOICEKEY_SERVER_URL` による
  サーバー接続先の上書きも無視するようにした。これまでは作業ディレクトリの `.env` や
  環境変数を汚染されると、Bearer トークン・音声・フィードバックを任意のサーバーへ
  向けられる余地があった。override は開発ビルドの preview 検証用に限定する。
  - 実装: Windows=`src/main.py`（`load_dotenv` を dist でスキップ）/ `src/utils/secrets.py`
    （`get_server_base_url` の override を dist で破棄）、Mac=`ServerConfig.resolveBaseURL`
    （dist では override 分岐に入らず本番固定・テスト可能な純関数へ分離）。
  - テスト追加（dist で override が無視される回帰／`tests/test_secrets_auth.py`）。

## [1.5.1] - 2026-06-28

### Changed
- **製品版の初回録音を高速化（起動時にサーバー接続を暖機／両OS・release）**。
  ログイン後の最初の文字起こしは、サーバー（`voicekey.vercel.app`）への往復に加えて serverless 関数の
  cold start（最大数秒）を初回に払っていた。**アプリ起動時にサーバー接続を 1 回だけ暖機**して
  TLS・接続・認証経路を温めることで、初回録音のサーバー往復を体感から消した。背景での定期取得
  （API の無駄打ち）は行わず、起動時 1 回限り。
  - **無料枠を消費しない設計**: 暖機は `GET /api/v1/me`（無料体験枠を消費しない read）で行う。
    短命トークンの先取り（`/api/v1/auth/ephemeral`）は**有料ユーザーのときだけ**実行する
    （有料は consume 前に return＝消費ゼロ。トークンキャッシュも温まる）。**無料体験ユーザーは
    トークンを取得しない**ため、起動のたびに無料枠を 1 消費してしまう事故が起きない。
  - 記事 https://zenn.dev/catnose99/articles/nani-translate の「起動時プリフライトで cold start を消す」を応用。
  - **実装**: Mac=`AppController.startup()`、Windows=`app.py VoicekeyApp._prewarm_backend`。
    ガード＝ログイン済み（製品版）。トークン先取りの追加条件＝`active`（有料）かつ既定が
    Deepgram ストリーミング。既存の `fetchAccountStatus()`／`fetchEphemeralToken()`
    （60 秒 TTL キャッシュ＋同時取得集約）をそのまま再利用。
  - `main`（自分用）は各プロバイダを直叩きしサーバー往復が無く既に最速のため変更なし。
- **テキスト整形の往復を短縮（暖機先を実際の経路に修正／両OS・release）**。
  録音開始時の `prewarm()` は整形用 TLS を温めていたが、**温める先が `api.groq.com`（直叩き）固定**で、
  製品版（ログイン済み）の整形が通る **サーバープロキシ `/api/v1/format` を温めていなかった**。ログイン時は
  プロキシ側を温めるよう修正し、録音後の整形の往復（特に最初の cold start）を短縮した。
  - 暖機は空テキストの POST で行う（サーバーは空を **400 で短絡**＝Groq を呼ばず lambda・TLS・認証だけ
    温まる。`/format` は `consume:false` なので**無料枠も消費しない**）。
  - **実装**: Mac=`BackendClient.warmFormatProxy()`＋`TextFormatter.prewarm()`、
    Windows=`backend_client.warm_format_proxy()`＋`text_formatter.prewarm()`。未ログイン（`main`/自分用）は
    従来どおり直 Groq を温める。テスト追加（暖機の分岐・空 POST・未ログイン no-op）。
  - 注: 整形の主たる所要時間は Groq の推論（≒355ms・US）で、これは暖機では縮まない。本変更が消すのは
    接続確立・サーバー関数の cold start 分。

### Technical Details
- アプリのバージョンを **1.5.1** に更新（Win=`config/constants.py`、Mac=`Resources/Info.plist`：
  `CFBundleShortVersionString=1.5.1` / `CFBundleVersion=13`）。性能改善（バグ修正レベル）のため PATCH を更新。

## [1.5.0] - 2026-06-27

### Added
- **アカウントごとに無料体験枠を付与（使い切るとアクティベーションキーが必要／両OS・release ＋ サーバー）**。
  これまでは「ログイン＋有効なアクティベーションキー」が無いと一切文字起こしできなかったが、
  **ログインすれば各アカウントにつき無料で 200 回まで文字起こしを試せる**ようにした。無料体験を使い切ると、
  従来どおりアクティベーションキーの登録が必要になる（課金は未実装のため、当面はキー登録のみが解放手段）。
  - **数え方**: 文字起こし 1 回（＝短命トークン発行 or ElevenLabs プロキシ呼び出し 1 回）につき 1 消費。
    **累計で一度きり**（月次リセットなし）。テキスト整形（後処理）は消費しない。
    サーバーだけが見える本物の呼び出し回数で数えるため、クライアント申告の使用量では突破できない。
  - **整合性**: サーバー側で原子的にカウント（`consume_free_quota` RPC＝`free_used < free_quota` のときだけ
    +1 する条件付き UPDATE）。残枠ゼロの呼び出しは **402** を返し、アプリは「無料体験を使い切りました。
    続けて使うにはアクティベーションキーの登録が必要です（設定 → アカウント）」と表示する。
  - **UI（設定 → アカウント）**: 無料体験中は「無料体験中（残り N / 200 回）」、使い切ると
    「無料体験を使い切りました。…キーを入力してください」を表示。無料体験中でも先にキーを登録できる。
  - **サーバー**: `entitlements` に `free_quota`（既定 200）・`free_used` を追加、`consume_free_quota(uuid)` RPC、
    `authorizeUsable(request,{consume})` を追加。`GET /api/v1/me` は `free_quota / free_used / free_remaining` を返す。
    短命トークン発行（Deepgram）と ElevenLabs プロキシは消費あり、テキスト整形（Groq）は消費なし。
  - **実装**: サーバー=`voicekey-site`（`lib/apiAuth.ts`・`app/api/v1/{auth/ephemeral,transcribe/elevenlabs,format,me}`・
    DB マイグレーション `free_quota_count_based`）、Mac=`Core/BackendClient.swift`・`Core/LoginCoordinator.swift`・
    `UI/SettingsView.swift`、Windows=`core/backend_client.py`・`core/login_coordinator.py`・`ui/settings_window.py`。

### Technical Details
- アプリのバージョンを **1.5.0** に更新（Win=`config/constants.py`、Mac=`Resources/Info.plist`：
  `CFBundleShortVersionString=1.5.0` / `CFBundleVersion=12`）。後方互換の機能追加のため MINOR を更新。
- 後方互換: 旧サーバー応答（`free_*` 無し）でも 0 扱いで従来挙動（未契約＝キー要求）になる。

## [1.4.0] - 2026-06-27

### Added
- **使用実績をアカウントに紐付け（端末横断・再インストール後も引き継ぎ／両OS・release）**。
  ログイン中は「実績」タブの累計文字数・回数・レベル/経験値・推定節約時間・連続利用日数、
  および入力量チャート（週/月/年）を、**この端末だけでなくアカウント全体（複数端末の合算）**で表示する。
  別の PC で使った分も合算され、アプリを入れ直しても実績が消えなくなった。未ログイン時は従来どおりローカル集計。
  - **動作**: 音声入力のたびに当日分（この端末の絶対値）をサーバーへ送信（撃ちっぱなし・冪等 upsert なので
    二重計上なし）。ログイン直後・起動時に直近 60 日分を押し上げてから、端末横断の合算を取り込む。
    集計・送信はすべて貼り付け確定「後」のバックグラウンド処理で、**音声→テキストの遅延には一切影響しない**。
  - **下限保証**: 端末横断の値がローカルより小さい場合でもローカルを下限に保ち、実績が下がって見えないようにした。
  - **サーバー**: `usage_stats` テーブル（`user_id, device_id, day` で日次の絶対値を upsert）と
    `POST /api/v1/stats/sync`・`GET /api/v1/stats`（端末横断で合算して日次系列＋累計を返す）。RLS で本人のみ閲覧可。
  - **実装**: Mac=`Core/StatsStore.swift`・`Core/BackendClient.swift`・`Core/LoginCoordinator.swift`、
    Windows=`core/stats.py`・`core/backend_client.py`・`core/login_coordinator.py`・`app.py`。

## [1.3.1] - 2026-06-27

### Fixed
- **音声入力後に「ログインが必要です」と誤表示される不具合を修正（両OS・release）**。
  ログイン＋アクティベーションキー登録済みでも、音声入力のたびにセッションが破棄され再ログインを
  求められることがあった。原因は **同じ refresh_token を同時に複数回使うとサーバー（Supabase GoTrue）の
  トークンローテーションで `refresh_token_already_used` となり、再利用検知で全セッションが revoke** される点。
  - **アプリ側**: トークン更新（refresh）を直列化し、同時に来た更新要求を進行中の 1 本に集約して
    refresh_token を二重使用しないようにした。Mac=`Core/AuthClient.swift`（進行中 Task の共有）、
    Windows=`core/auth_client.py`（`RLock` ＋ `ensure_valid_session` のロック下再確認）。
  - **サーバー側**: 一時的な競合（`refresh_token_already_used`）は 401 ではなく **409（復帰可能）** で返すように変更。
    アプリはこれを「現在のセッションは有効」と解釈し、セッションを破棄しない。`voicekey-site`
    `app/api/v1/auth/refresh/route.ts`。**この修正はデプロイだけで既存版（v1.3.0）のユーザーにも有効**。

### Changed
- **音声入力のレイテンシ（毎回の遅延）を大幅短縮（両OS・release ＋ サーバー）**。
  - **サーバーのリージョンを東京（`hnd1`）に固定**（`voicekey-site/vercel.json`）。従来は Vercel 関数が
    米国東部（`iad1`）で動き、東京の Supabase DB へ**毎回 6 往復の太平洋横断**（各 ~150ms ≒ 約 1 秒）＋
    コールドスタートが発生していた。同一リージョン化で DB 往復が数 ms に短縮される。
  - **短命トークン発行 API の監査書き込みを `after()` でレスポンス後に回す**（`token_grants` 記録・利用ログ・
    デバイス最終アクセス更新）。トークン発行のクリティカルパスから DB 書き込みを外した。`app/api/v1/auth/ephemeral/route.ts`。
  - **アプリ側に短命トークンのキャッシュを追加**。TTL（60 秒）内は録音をまたいで同じトークンを再利用し、
    2 回目以降の録音はトークン取得のネットワーク往復ゼロで開始する。同時取得は 1 本に集約。
    Mac=`Core/BackendClient.swift`、Windows=`core/backend_client.py`。ログアウト時はキャッシュを破棄。

### Fixed (サーバー堅牢性・`voicekey-site`)
- **Stripe Webhook の書き込み失敗を握り潰していた不具合を修正**。`subscriptions` upsert・`profiles` の customer
  紐付け・`entitlements` の再集計（取得/upsert/`recompute_entitlement`）でエラーを throw するようにし、
  失敗時は冪等記録を消して Stripe に再送させる。従来は「課金されたのに利用権が付かない」事故が無言で起こり得た。
  `app/api/v1/webhooks/stripe/route.ts`、`lib/entitlements.ts`。
- **短命トークン API の `device_id` 長さ上限（200 文字）を追加**（巨大値で DB を汚されないように）。
- **コード交換（exchange）でトークン欠落時に空のセッションを返さないよう null チェックを追加**（アプリが
  「ログイン成功」と誤認するのを防ぐ）。`app/api/v1/auth/exchange/route.ts`。
- **利用ログ（`logUsage`）の DB エラーを `console.error` で観測可能に**（`insert` は例外を投げず `{error}` を
  返すため従来は完全に握り潰されていた）。`lib/apiAuth.ts`。
- **Checkout のプロフィール取得を `.single()`→`.maybeSingle()` に変更**（行が無いとき不要なエラーを出さない）。
  `app/api/v1/billing/checkout/route.ts`。

### Technical Details
- アプリのバージョンを **1.3.1** に更新（Win=`config/constants.py`、Mac=`Resources/Info.plist`：
  `CFBundleShortVersionString` 1.3.1 / `CFBundleVersion` 10）。
- 回帰テスト: `tests/test_backend_client.py` にトークンキャッシュのテスト分離（`clear_token_cache`）を追加。
  並行リフレッシュ直列化に伴い先回りリフレッシュのテストを `_perform_refresh` パッチに更新。
  Windows 全 263 テスト通過・Mac ビルド（0 警告）通過。

## [1.3.0] - 2026-06-26

### Added
- **アクティベーションキーの登録 UI ＋「無料配布はアクティベーション必須」ゲートを追加（両OS・release）**。
  Stripe 課金は一旦無しで、**ログイン＋アクティベーションキーだけで誰でも無料で使える**配布形態にするための
  アプリ側実装。設定 → アカウントでログイン後、アクティベーションキーを入力・登録すると、利用権が
  **アカウントに紐付く**（`redeem_activation_key` がサーバー側で `redeemed_by` に記録）。一度登録すれば
  別の端末でログインしても同じライセンスで使える（利用権はアカウント単位）。配布（DIST）ビルドでは
  埋め込みキー直叩きのフォールバックを止め、**ログイン＋有効な利用権が無いと文字起こしできない**ようにした
  （開発ビルドは従来どおり埋め込み/設定キーの並存を維持）。
  - **サーバー契約**: ログイン中アカウントの状態を返す `GET /api/v1/me`（`{email, active, active_until}`・未契約でも
    200 で `active:false`）を `voicekey-site` に追加。キー登録は既存の `POST /api/v1/activation/redeem`
    （`{ok, active_until}` / 失敗は 400 ＋日本語 `{error}`）に配線。
  - **Technical Details**:
    - 接続定数: `API_ME_PATH` / `API_REDEEM_PATH` を追加（Win=`config/constants.py`、Mac=`Config/ServerConfig.swift`）。
    - バックエンドクライアント: `fetch_account_status()`（GET /me）と `redeem_activation_key()`（POST /redeem・
      サーバーの日本語エラー本文を優先表示）を追加。403 の案内文を「サブスクリプションが有効ではありません」→
      「利用するにはアクティベーションキーの登録が必要です（設定 → アカウント）」に変更。
      Win=`core/backend_client.py`（GET 対応・エラー本文を握れる `_send` に拡張）、Mac=`Core/BackendClient.swift`
      （`AccountStatus` / `.message(String)` エラー）。
    - ログイン司令塔に利用権の状態を追加（`unknown/checking/active/none/error` ＋ メール）。ログイン直後に自動確認、
      `redeem()` 成功で `active` に更新。Win=`core/login_coordinator.py`、Mac=`Core/LoginCoordinator.swift`。
    - 設定 UI のアカウント画面に「ライセンス（アクティベーションキー）」セクション（利用権の状態表示・キー入力欄・
      登録ボタン・期限表示）を追加。Win=`ui/settings_window.py`（登録はワーカースレッド＋Signal）、Mac=`UI/SettingsView.swift`。
    - 無料ゲート: 配布版で未ログインなら文字起こしを `_dist_guard`（Win）/ DIST 分岐（Mac）でブロック。Deepgram の
      ストリーミングは配布版で埋め込みキーにフォールバックせず、未ログイン時は REST 経由でゲートのメッセージを出す。
      Win=`core/api_transcriber.py` / `core/streaming_transcriber.py`、Mac=`Core/Transcriber.swift` / `Core/StreamingTranscriber.swift`。
    - 回帰テスト: `tests/test_backend_client.py`（/me・redeem の成功/エラー/本文表示）、`tests/test_login_coordinator.py`
      （利用権確認・キー登録・ログアウトでのクリア）、`tests/test_api_transcriber.py`（DIST ゲートの4ケース）を追加。
      Windows 全 263 テスト・Mac ビルド（`swift build`）ともに通過。
- **実績タブにデザイン重視の使用統計チャートを追加（両OS両ブランチ）**。
  「今日 / 今週 / 累計」の入力量を 0 からカウントアップ表示し、**期間（週・月・年）を切り替えられる棒グラフ**で
  日ごと・月ごとの入力量を可視化する。開いた瞬間に数字がカウントアップし、棒が下から伸びるアニメーションで
  「使うほど貯まる」達成感を出す。集計はすべて貼り付け確定後のローカル処理のみで、音声入力に遅延を足さない。
  - **Technical Details**:
    - データ層（両OS共通の系列メソッド）: 日付ごとの入力量バケット `daily`（`{characters, recording_seconds, sessions}` を
      `yyyy-MM-dd` キーで保持・直近 800 日に剪定）を追加。`daily_series(n)` / `monthly_series(n)` / 直近 n 日合計の
      ヘルパーを実装。Windows=`src/core/stats.py`、Mac=`Core/StatsStore.swift`（`DayStat` / `UsagePoint`）。回帰テスト
      `tests/test_stats.py` に日次・月次系列の 4 ケースを追加。
    - Mac: `UI/SettingsView.swift` の `StatsTab` を Swift Charts（`BarMark`）で再構成。`StatsPeriod`（週/月/年）の
      セグメント Picker、`AnimatedNumber`（`Animatable`）でカウントアップ。依存追加なし（macOS 標準の Charts）。
    - Windows: `ui/settings_window.py` に自前描画ウィジェット `UsageBarChart`（`QPainter`・`QPropertyAnimation` で棒が
      伸びる）を追加。サマリーは `QVariantAnimation` でカウントアップ、期間切替は `QButtonGroup` のセグメント風ボタン。
      QtCharts 等の依存は追加していない。
- **設定 UI を「開閉できる左サイドバー」に変更（両OS両ブランチ）**。
  項目が増えても画面外に溢れないよう、設定タブを縦のサイドバーにまとめ、開閉トグルで「アイコン＋ラベル ⇄ アイコンのみの
  レール」を切り替えられるようにした。ナビは縦スクロール可能で、選択中の項目はアクセント色で塗る。
  - **Technical Details**:
    - Mac: 横並び `TabView`（画面外へ溢れていた）を `HStack { sidebar; Divider(); content }` に置換。`sidebarCollapsed`
      状態で幅 200⇄62 をアニメーション。ナビは `ScrollView` ＋ `NavItem`（SF Symbols アイコン）。
    - Windows: 既存の `QListWidget` サイドバーに開閉トグル（☰）とアイコンを追加。折りたたみ時は幅 184⇄60 を
      `QPropertyAnimation` で変化させ、項目ラベルを消してアイコンのみにする（ホバーのツールチップで名称表示）。
      アイコンは依存追加なしの自前 SVG → `QIcon`（通常色＋選択時白の 2 状態）。`styles.py` にセグメントボタン／
      トグルの QSS を追加。

## [1.2.0] - 2026-06-20

### Added
- **ユーザー辞書（確定置換）機能を追加（両OS両ブランチ）**。
  設定の「ユーザー辞書」タブで「変換元 → 変換先」の置換ルールを追加・編集・削除でき、
  文字起こし・整形が終わった文章を貼り付ける直前にローカルで機械置換する（部分一致・登録順・
  API を通さないので音声入力に遅延を足さない）。行ごとの有効/無効トグル付き。
  - **Technical Details**:
    - Windows: `config/constants.py` の `DEFAULT_CONFIG` に `replacements`（`{from,to,enabled}` のリスト）を追加。
      `app.py` に `_apply_replacements()` を新設し `_insert_and_enter()` の貼り付け直前で適用（履歴にも置換後を記録）。
      `ui/settings_window.py` に「ユーザー辞書」ページ（動的な行の追加/削除・`_collect_replacements()` で保存）を追加。
      回帰テスト `tests/test_replacements.py`（10 ケース）。
    - Mac: `Config/AppConfig.swift` に `ReplacementRule`（Codable/Identifiable）と `ConfigStore.replacements`＋永続化、
      `applyReplacements(_:)` を追加。`AppController.swift` が整形後・貼り付け前に適用。
      `UI/SettingsView.swift` に「ユーザー辞書」タブ（`DictionaryTab`）を追加。
- **設定に「バージョン情報」タブを追加（自動アップデートの可視化・両OS両ブランチ）**。
  現在のアプリバージョンを表示し、「アップデートを確認」で最新版を手動チェック、新しいバージョンが
  見つかったときだけ「今すぐ更新する」ボタンを出す。チェック頻度（起動時＋1 日ごと）・フィード URL・
  署名/インストール処理は変更していない（既存の Sparkle / updater をそのまま利用）。
  - **Technical Details**:
    - Windows: `utils/updater.py` に `up_to_date` シグナルを追加し、`check_now(manual=)`/`_check(manual=)` で
      手動チェック時のみ「最新です」「確認に失敗」を UI へ通知（定期チェックは従来どおりログのみ）。
      `ui/settings_window.py` に「バージョン情報」ページ（`_create_version_page` ＋確認/更新ハンドラ）を新設し、
      `SettingsWindow(updater=)` で updater を受け取って配線。`app.py` は updater を設定ウィンドウより先に生成して渡す。
    - Mac: `Core/UpdaterController.swift` を `ObservableObject` + `SPUUpdaterDelegate` 化し、検知結果を
      `availableVersionString` に publish（既存の `isAvailable`/`checkForUpdates` は維持）。
      `UI/SettingsView.swift` に「バージョン情報」タブ（`AboutTab`）を追加。Sparkle 既定の更新ダイアログ・
      メニュー「アップデートを確認…」導線はそのまま残す。
- **製品版バックエンドへの配線（段階3・並存ガード）を追加**（Mac / Windows 両方・release ブランチ・休眠中）。
  既存の文字起こし／整形プリミティブが「ログイン済みならサーバー経路、未ログインなら従来の
  埋め込み／設定キー直叩き」を自分で切り替えるようにした。ログイン UI（段階4）が入るまでは
  常に未ログイン扱い（`isLoggedIn` が false）のため、**ユーザーから見える挙動は一切変わらない**。
  - **高速リアルタイム（Deepgram）**: ログイン時は録音時にサーバーから短命 JWT を取得し、
    WebSocket ストリーミング・REST フォールバックとも `Authorization: Bearer <jwt>` で**直叩き**
    （低レイテンシ核心を維持）。未ログイン時は従来どおり `Token <キー>`。
  - **正確性（ElevenLabs）**: ログイン時はサーバープロキシ（multipart）経由（バッチは短命キー非対応）。
    プロキシ失敗は `TranscriptionError` に写す。
  - **テキスト整形（Groq）**: ログイン時はサーバープロキシ経由（モデル/プロンプトはサーバー固定）。
    失敗時は従来どおり原文フォールバック（発話を絶対に失わない）。
  - Mac の `StreamingTranscriber` に接続前チャンクの退避バッファ（`pending`）と `cancelled` フラグを
    追加（短命 JWT 取得が非同期なため、接続確立前に届く PCM を取りこぼさない）。
  - **既知のフォローアップ（段階4/5 で対応）**: 現状は文字起こし呼び出しごとに短命 JWT を取得する。
    長文分割（並列）では分割数だけ取得しうるため、録音プリウォーム位置での先取得＋キャッシュは段階4/5 で行う。
  - **Technical Details**:
    - Windows: `core/backend_client.py` に `is_logged_in()` を追加。`core/streaming_transcriber.py`
      （`_run(key, logged_in)` で Bearer/Token 分岐）、`core/api_transcriber.py`（ElevenLabs プロキシ／
      Deepgram `_transcribe_via_jwt`）、`core/text_formatter.py`（整形プロキシ）に配線。
      回帰テスト 13 件追加（`test_backend_client` / `test_streaming_transcriber` / `test_api_transcriber` /
      `test_text_formatter`）。直叩きテストは `is_logged_in` を False 固定し実 keyring に触れない。
    - Mac: `Core/BackendClient.swift` に `isLoggedIn` を追加。`Core/StreamingTranscriber.swift`
      （`connect(auth:)` 抽出＋ JWT 経路＋退避バッファ）、`Core/Transcriber.swift`
      （`transcribeElevenLabsViaProxy` / `transcribeDeepgramViaJWT`／`send`・`encodeAudio` 抽出）、
      `Core/TextFormatter.swift`（整形プロキシ）に配線。
- **アプリ内フィードバック送信フォームを追加**（Mac / Windows 両方・release ブランチ）。
  メニュー／トレイの「フィードバックを送る…」を、これまでの `mailto:`（既定メーラー起動）から
  **アプリ内の入力フォーム → 自社サーバー送信**へ変更した。ログイン済みならアカウントに紐づき、
  未ログイン（無料ベータ・匿名）でも `device_id` + アプリバージョンで送れる（サブスク有効性は問わない）。
  送信成功／失敗を画面に明示する（誤送信防止より「送れた確証」を優先）。受信は管理画面のみ
  （外部通知は付けない）＝サーバーは Supabase の `feedback` テーブルに保存し、`/admin/feedback` で一覧する。
  - サーバー側は別リポ `voicekey-site`（`feedback` テーブル migration 0013 ／ `POST /api/v1/feedback`
    ／ 管理一覧 `/admin/feedback`）。**サーバーのデプロイと Supabase への migration 適用が済むまでは
    実送信は成立しない**（アプリ側のフォーム表示・入力は動作する）。
  - **Technical Details**:
    - Windows: `core/backend_client.py` に `submit_feedback()`（認証は任意）。`ui/feedback_dialog.py`
      （`QDialog` ＋ 送信ワーカー `QThread`）を新規追加し、`ui/system_tray.py` の `_send_feedback`
      を mailto からダイアログ起動へ変更（不要になった `QUrl` / `QDesktopServices` / `APP_VERSION`
      の import を削除）。`constants.API_FEEDBACK_PATH` を追加。`test_backend_client` に送信テスト 3 件追加。
    - Mac: `Core/BackendClient.swift` に `submitFeedback(_:)`（認証は任意）。`UI/FeedbackView.swift`
      （SwiftUI フォーム）を新規追加し、`VoicekeyApp.swift` の `sendFeedback` を mailto から
      フィードバックウィンドウ表示へ変更。`Config/ServerConfig.swift` に `feedbackPath` を追加。
- **ブラウザ経由ログインの認証クライアントを追加（段階4・増分1／Mac・Windows 両方・release・休眠中）**。
  ログイン UI 配線前の土台として、(1) CSRF 用 state 生成、(2) ブラウザで開くログイン URL
  （`/auth/app?state=&device_id=&platform=`）の構築、(3) ワンタイムコード→トークン交換
  （`POST /api/v1/auth/exchange`）、(4) `refresh_token`→トークン更新（`POST /api/v1/auth/refresh`）と
  失効 60 秒前の自動リフレッシュを実装。トークンは URL に乗せず、ワンタイムコードの交換でのみ取得し、
  既存の認証セッション保存（Keychain / Credential Manager）に書き込む。`refresh` も失効（401）したら
  セッションを破棄して再ログインへ誘導する。**URL スキーム登録・deep link 受信・ログイン UI は後続増分**。
  - サーバー側は別リポ `voicekey-site`：トークン更新エンドポイント `POST /api/v1/auth/refresh` を新規追加
    （GoTrue 直叩きをサーバーに閉じ込め、アプリに anon キーを埋め込まない）。`exchange`/`refresh` とも
    `expires_at` を **UNIX 秒(number)** で返す統一契約に変更（アプリは Double/float で保存・parse 不要）。
  - **Technical Details**:
    - Windows: `core/auth_client.py` を新規追加（`make_state` / `make_login_url` / `exchange_code` /
      `refresh` / `ensure_valid_session` / `logout`）。エラー型・HTTP クライアントは `backend_client` と共有。
      `constants` に `AUTH_APP_PATH` / `API_AUTH_EXCHANGE_PATH` / `API_AUTH_REFRESH_PATH` を追加。
      `tests/test_auth_client.py` に 11 件追加（実 keyring・実通信に触れない）。
    - Mac: `Core/AuthClient.swift` を新規追加（同名の API・`AuthError` 列挙）。`Config/ServerConfig.swift`
      に `authAppPath` / `exchangePath` / `refreshPath` を追加。
- **ブラウザ経由ログインの司令塔（deep link 解析・state 照合・交換）を追加（段階4・増分2／両OS・release・休眠中）**。
  ログイン開始（state 生成 → ログイン URL）と deep link 受信（`voicekey://auth?code=&state=` の解析 →
  CSRF 用 state 照合 → コード交換）を 1 か所に束ねた。保留 state はログイン開始〜受信の間だけ保持し、
  戻ってきた state が一致しなければ交換しない／一度使った state は消費して再利用を防ぐ。
  **URL スキーム登録（Info.plist / レジストリ）と OS からの URL 受信配線・ログイン UI は増分3**。
  - **Technical Details**:
    - Windows: `core/login_coordinator.py` を新規追加（`LoginCoordinator`: `begin_login` / `complete_login` /
      `logout` / `parse_auth_url`）。Qt 非依存（ネットワークを伴う `complete_login` は UI 側でワーカー実行する想定）。
      `tests/test_login_coordinator.py` に 14 件追加（解析・state 不一致/消費・交換失敗）。
    - Mac: `Core/LoginCoordinator.swift` を新規追加（`ObservableObject`・`Status` 列挙・`parseAuthURL` 純粋関数）。
- **ログイン UI と URL スキーム受信を追加（段階4・増分3／両OS・release）**。設定に「アカウント」ページを新設し、
  ログイン状態（未ログイン／ブラウザで完了待ち／処理中／ログイン済み／失敗）の表示と
  ログイン・ログアウトボタンを追加。ログインボタンは既定ブラウザでログインページを開き、
  ブラウザ側で完了すると `voicekey://auth?code=&state=` の deep link でアプリに戻ってトークン交換まで自動で進む。
  **ログインの成立にはサーバー（voicekey-site）のデプロイが必要**なため、それまでは UI 表示のみ動作する（休眠）。
  - **Technical Details**:
    - Mac: `Resources/Info.plist` に `CFBundleURLTypes`（`voicekey` スキーム）を追加し、`VoicekeyApp.swift` で
      `NSAppleEventManager`（`kAEGetURL`）を受けて `LoginCoordinator.shared.handleDeepLink` へ渡す。
      `UI/SettingsView.swift` に「アカウント」タブを追加（`LoginCoordinator` を購読）。
      ビルド済み .app で `voicekey:` スキームが LaunchServices に登録され、deep link がアプリへ届くことを実機確認。
    - Windows: `core/deep_link.py` を新規追加（`voicekey://` のレジストリ登録〔win32/凍結ビルドのみ〕＋
      `QLocalServer`/`QLocalSocket` による単一インスタンス化と URL 転送）。`main.py` で 2 つ目以降の起動を
      稼働中インスタンスへ転送、`app.py` の `VoicekeyApp.handle_deep_link`（ワーカースレッドでコード交換）へ配線。
      `ui/settings_window.py` に「アカウント」ページを追加（状態ポーリング＋ログイン/ログアウト）。
      `login_coordinator.shared()`（遅延生成シングルトン）を追加。`tests/test_deep_link.py` を新規追加。
      単一インスタンス判定は失敗しても通常起動するよう安全側に倒している（deep link 転送だけ諦める）。
- **トークン自動更新をバックエンド呼び出しに配線（段階4・増分4／両OS・release・休眠中）**。
  認証付きのサーバー呼び出し（短命 JWT 取得・ElevenLabs プロキシ・整形プロキシ）で、(1) 送信前に
  失効間際なら `ensure_valid_session` で先回りリフレッシュ、(2) それでも `401` が返ったら一度だけ
  `refresh_token` で更新して**同一リクエストを再試行**するようにした。更新も失敗（401）したら
  再ログインを促すエラーに写す。再試行は一度だけ（無限ループ防止）。認証ヘッダの無い呼び出し
  （exchange / refresh / 匿名フィードバック）はリフレッシュ対象外＝再帰しない。これにより
  ログイン済みユーザーは access_token の失効を意識せず使い続けられる（UI からは見えない裏側の改善）。
  - **Technical Details**:
    - Windows: `core/backend_client.py` の `_auth_headers()` が送信前に `auth_client.ensure_valid_session()`
      を呼び、`_post()` に `_allow_refresh` 引数を追加して 401 時に一度だけ `auth_client.refresh()`→再試行。
      `tests/test_backend_client.py` に 401 リフレッシュ再試行・失敗時非再試行・一度きり・先回りリフレッシュの
      4 件を追加（フィクスチャの `expires_at` を未来時刻にして既存テストを no-op 化）。
    - Mac: `Core/BackendClient.swift` の各認証付きメソッドが先頭で `AuthClient.ensureValidSession()` を呼び、
      `send(_:allowRefresh:)` が 401 時に `AuthClient.refresh()`→新 Bearer で一度だけ再試行する。

### Fixed
- **（Windows）2 回目以降の録音でマイク音声が拾えなくなるバグを修正**。永続ストリームの
  audio callback は「ストリームを開いた時点」の `session_id` に固定されるのに、`_do_start` が
  録音のたびに `session_id` を +1 していたため、1 回目は偶然一致して録音できても 2 回目以降は
  session 不一致で callback が受信音声を全部破棄し、無音になっていた。`session_id` は
  「ストリーム世代」を表すよう、ストリームを開くとき（`_open_stream`）にだけ採番する方式へ変更。
  録音の start/stop を繰り返しても callback は有効なまま。leak したゾンビ callback は `recover()`
  時の世代繰り上げで従来どおり弾かれる。`tests/test_audio_recorder.py` に 2 回目録音の回帰テストを追加。
- **（Mac・体感は両 OS）録音開始直後に「マイクの構成が変わったため録音を停止しました」と
  誤って止まるバグを修正**。`AVAudioEngineConfigurationChange` は `engine.start()` 直後や
  フォーマット確定時にもデバイス未変更で頻繁に誤発火するのに、録音中はこれを一律「デバイス切断」
  とみなして即停止していた。エンジンがまだ動いていれば中断せず、本当に停止していたら同じデバイスで
  タップ・変換器を作り直して**録音をシームレスに継続**し、復帰不能（実際の切断等）のときだけ
  停止通知するよう変更（短時間の再起動回数を数えてループも防止）。`start()` の起動処理を
  `installTapAndStart()` に抽出し再開経路と共通化。

## [Mac 1.2.0 / Windows 1.2.0] - 2026-06-18

### Added
- **製品版バックエンド接続の基盤を追加**（Mac / Windows 両方・release ブランチ・まだ未配線）。
  製品版が長期 API キーをアプリに同梱せず、自社サーバー経由で短命トークン／プロキシを使う
  構成（Phase 5 アプリ統合）の土台。現時点では呼び出し経路が未接続のため、ユーザーから
  見える挙動は変わらない（段階的コミットの最初の 1 段）。
  - サーバー接続先の定数（配布 = https://voicekey.vercel.app / 開発 = http://localhost:3000、
    環境変数 `VOICEKEY_SERVER_URL` で上書き可）と API パス（短命 JWT 発行 / ElevenLabs
    プロキシ / Groq 整形プロキシ）。
  - 端末固有 ID（device_id。識別子であって認証子ではない。サーバー側の同時台数上限・
    悪用検知に使う）を Keychain / Credential Manager に生成・保存。
  - Supabase 認証セッション（access_token / refresh_token / expires_at）の保存・取得・削除。
  - **Technical Details**:
    - Mac: `Config/ServerConfig.swift`（新規）、`Core/Keychain.swift` に `deviceId()` /
      `AuthSession` / `saveAuthSession` / `authSession` / `clearAuthSession` を追加。
    - Windows: `config/constants.py` に接続先・パス定数、`utils/secrets.py` に
      `get_server_base_url()` / `get_device_id()` / `get_auth_session()` /
      `save_auth_session()` / `clear_auth_session()` を追加。`tests/test_secrets_auth.py`（9 ケース）追加。
- **製品版バックエンドクライアントを追加**（Mac / Windows 両方・release ブランチ・段階2・まだ未配線）。
  Phase 4 で実装済みの自社サーバー API を叩くクライアント。`Authorization: Bearer
  <Supabase access_token>` ＋ `x-device-id` で認証し、(1) Deepgram 短命 JWT 取得、
  (2) ElevenLabs 文字起こしプロキシ（multipart）、(3) Groq 整形プロキシ（text のみ・
  モデル/プロンプトはサーバー固定）を提供する。非 200（401/403/409/429/503）は
  ユーザー向け日本語メッセージのエラーに写す。既存の文字起こし経路への配線は段階3 で行う。
  - **Technical Details**:
    - Mac: `Core/BackendClient.swift`（新規。`fetchEphemeralToken` / `transcribeElevenLabs` /
      `formatText` ＋ `BackendError`）。
    - Windows: `core/backend_client.py`（新規。`fetch_ephemeral_token` / `transcribe_elevenlabs` /
      `format_text` ＋ `BackendError`）。`tests/test_backend_client.py`（9 ケース・httpx.MockTransport）追加。
- **使用実績（統計＋ゲーミフィケーション）機能を追加**（Mac / Windows 両方・両ブランチ共通）。
  音声入力を使うほど育つ「実績」を設定ウィンドウの新タブ「実績」に表示する。
  - **表示項目**: レベルと経験値（累計文字数 = XP）＋次レベルまでの進捗バー、推定節約時間
    （同じ文章をキーボードで打つ場合との差。タイピング 4.0 字/秒を控えめに仮定し、過大表示を避ける。
    短い入力でマイナスになる分は 0 に丸める）、累計文字数、音声入力した回数、連続利用日数（最長記録付き）。
  - **実績はリセット不可**（ユーザーが消せないよう、リセット操作は提供しない）。
  - **遅延ゼロ設計**: 集計は貼り付け確定「後」のローカル処理のみで、音声 → テキストの経路には
    一切待ちを足さない（ユーザー指示の最重要制約を順守）。
  - **永続化**: アプリ再起動後も残るよう小さな JSON（`stats.json`）に保存。
    Mac は `~/Library/Application Support/voicekey/stats.json`、Windows は `settings.yaml` と同じディレクトリ。
  - レベル/しきい値/連続日数/節約時間の計算式は Mac（Swift）と Windows（Python）で一致させた。
  - **Technical Details**:
    - Mac: `Core/StatsStore.swift`（新規・`StatsData` Codable ＋レベル計算）、`AppController.swift` の
      確定出力 2 箇所（ストリーミング / REST）で `stats.recordSession(...)` を呼ぶ、
      `UI/SettingsView.swift` に `StatsTab`（tag 3）を追加、`VoicekeyApp.swift` で `stats` を受け渡し。
    - Windows: `core/stats.py`（新規・`StatsStore` ＋ `threshold()` / `level_for_xp()`）、`app.py` の
      `_record_stats()` を文字起こし 2 経路で呼ぶ、`ui/settings_window.py` に「実績」ページ
      （サイドバー index 3）を追加。`tests/test_stats.py` を新規追加（計算式・永続化・復帰の 14 ケース）。

## [Mac 1.1.0 / Windows 1.1.0] - 2026-06-18

### Changed
- **（release＝製品版ブランチ）文字起こしを 2 モードに簡素化＋モデル/整形設定を非公開化**（Mac / Windows 両方）。
  顧客が迷わず使えるよう、文字起こしバックエンドを **「高速リアルタイム」（Deepgram nova-3）/
  「正確性」（ElevenLabs scribe_v1）の 2 択のみ**に絞り、モデル選択 UI を撤去（推奨モデルに固定）。
  OpenAI は文字起こしから除外、Groq は裏のテキスト整形専用に。テキスト整形は Groq 固定モデル
  （llama-3.1-8b-instant）＋固定プロンプトで**裏で自動実行**し、モデル/プロンプト選択 UI は撤去・
  **オンオフトグルのみ残置（既定オン）**。
  - Mac: `Backend.label` を製品名（高速リアルタイム/正確性）に、`Backend.selectableCases` を追加して
    バックエンド Picker を 2 択化。スロットのモデル Picker・一般タブの整形モデル/指示 UI を撤去。
    既定スロットを deepgram/elevenlabs に、`SlotConfig` の decode で旧 openai/groq を deepgram へ移行
    （モデルも推奨へ揃える）。整形既定 `formatEnabled` を `true` に。API キータブは deepgram/elevenlabs/groq のみ。
  - Windows: `_TRANSCRIBE_BACKEND_LABELS`（2 択）・`_API_KEY_BACKENDS`（3 種）を追加。バックエンド Combo を
    2 択化、モデル Combo・整形モデル/指示 UI を撤去（不要化した `_BACKEND_LABELS`/`_BACKEND_MODELS`/
    `_model_label` 等を削除）。`constants.py` の既定を deepgram/elevenlabs＋`format_enabled: True` に。
    `config_manager._constrain_release_backends` を追加し、保存済み openai/groq を deepgram へ移行
    （`api_model` を空にして `default_api_models` へフォールバック）。テスト 2 件追加。
- **VAD・長文分割・リアルタイムストリーミング・録音 HUD（＋ Windows の音量正規化）のオンオフを
  設定 UI から撤去し、常時 ON に固定**（Mac / Windows 両方）。これらは「使い分けが難しく常に ON が
  最適」なため、ユーザーが OFF にする手段を持たせない方針に変更。動作（消費側）は従来どおり常に有効。
  - Mac: `ConfigStore` の `vadEnabled / splitParallelEnabled / streamingEnabled / hudEnabled` を
    init で `true` 固定にし、`SettingsView` GeneralSettingsTab から該当 Toggle を撤去。
  - Windows: `settings_window.py` 一般ページから該当ウィジェットと読込/保存バインド・
    ストリーミング診断（`_refresh_streaming_status`）を撤去。`config_manager._force_always_on` を
    追加し、読込（`_load_config`）と保存（`save`）の双方で `vad_filter / split_parallel_enabled /
    streaming_enabled / hud_enabled / audio_preprocess.volume_normalize` を `True` に矯正
    （保存済み settings.yaml に古い `false` が残っていても無視して常時 ON を保証）。
- **2 ブランチ運用に分離**（`main`=自分用 / `release`=製品版・絶対に混ぜない）。分岐ポリシーを
  `CLAUDE.md`・`AGENTS.md`・`HANDOFF.md` に明記（どのブランチに入れるかは毎回指定、未指定なら確認）。
- **バックエンドの表示名を特徴ベース名に変更**（Mac / Windows 両方）。設定 UI の
  バックエンド選択・ユーザー向けエラーメッセージで提供元名（OpenAI / Groq /
  ElevenLabs / Deepgram）を出さず、**「高精度 / 高速 / 多言語 / リアルタイム」**と表示する。
  保存値（`Backend.rawValue` / `settings.yaml` の `backend`）は従来どおり不変なので
  既存設定はそのまま読める。API キー入力欄だけは、どのキーを入れる欄か分かるよう提供元名を
  表示（配布版では API キータブ自体が非表示なので提供元名は開発時のみ露出）。
  - Mac: `Backend.label` を特徴名に変更し、提供元名は `Backend.providerName` に分離。
    `Transcriber` のエラー文（ElevenLabs / Deepgram の応答解析失敗）も `backend.label` に統一
  - Windows: `_BACKEND_LABELS` を特徴名に変更し、提供元名は `_BACKEND_PROVIDER_NAMES` に分離。
    `api_transcriber.py` の各 `display_name` も特徴名に変更（ユーザー向けエラー文に出るため）
- **Windows 設定 UI の補足説明文を Mac と同等に削減**。プロバイダー名・モデル名
  （Groq / Deepgram / llama-... 等）を晒す説明や冗長な解説を撤去し、コントロール名だけでは
  意味が通じない 2 項目（自動 Enter の遅延 / ハンズフリー切替キー）のみ短文を残した。
  ストリーミングのトグル名・状態メッセージからも「Deepgram」表記を除去（「リアルタイム」へ）。
  プライバシー上の保存先注記（履歴・API キーの保存場所）は残置

## [Mac 1.0.2 / Windows 1.0.1] - 2026-06-16

### Added
- **Windows インストーラにデスクトップショートカット作成オプションを追加**。Inno Setup の `[Tasks]`/`[Icons]` に `desktopicon` を追加し、インストール時に「デスクトップにショートカットを作成する」を選べる（既定チェック済み）。従来はスタートメニューとログイン時自動起動のみだった
- **Windows 版 v1.0.0 を初公開配布**。GitHub Actions（`windows-build.yml`）でキー埋め込みインストーラを生成し、公開バイナリ専用リポ `voicekey-releases`（**ソース非公開**）の GitHub Releases でホスト。配布サイト（Vercel）の「Windows 版をダウンロード」を有効化。容量が大きい（約 268MB）ためサイト本体（Vercel）ではなく GitHub Releases から配る構成にした
  - `scripts/build/build_windows_dist.ps1`: version.json の `url` を GitHub Releases のアセット URL に変更（自動アップデータのダウンロード元）

### Fixed
- **Windows でマイク自動検出が OS をクラッシュ（再起動）させる重大バグを修正（WDF エラー）**。`src/core/mic_auto_detect.py` が全入力デバイスに `sd.InputStream` を一斉に同時オープンしていたため、同じ物理マイクが host API ごと（特にカーネルストリーミングの WDM-KS）に重複列挙される Windows では、それらを同時に開いた瞬間に音声ドライバが WDF レベルでクラッシュし OS が再起動していた。次の 3 点で根治した:
  - **同時オープンを廃止し、1 台ずつ順次プローブする**（同時に開く `InputStream` は常に最大 1）。回帰テスト `test_devices_probed_sequentially` で「同時オープンが起きない・プローブ後にストリームが残らない」ことを保証
  - **WDM-KS / ASIO（カーネル直叩き・排他系）と同名重複デバイスを自動検出の対象から除外**（Windows のみ）。カーネルを直接叩く host API を自動検出では触らない
  - 開く前に `sd.check_input_settings` で構成を検証し、非対応・占有中デバイスは実際に開かずスキップ
  - 順次化に伴い、設定 UI の文言を「マイクに向かって喋り続けてください」に変更。`AudioRecorder.list_input_devices` の戻りに host API 名（`hostapi`）を追加
- **Windows 版 GitHub Actions ビルドの文字化け／エンコーディング不具合を修正**（Mac から PC を使わずに Windows 配布物をビルドできるようにする一連の対応）
  - `scripts/build/build_windows_dist.ps1` に UTF-8 BOM を付与。Windows PowerShell 5.1（powershell.exe）が BOM なし UTF-8 を cp1252 と誤読し、日本語を含む行でパースエラー（`The string is missing the terminator`）になっていた
  - `scripts/build/generate_embedded_keys.py` で stdout/stderr を UTF-8 に固定。Windows ランナーの既定 stdout（cp1252）で日本語の進捗 print が `UnicodeEncodeError` になり、鍵生成は成功しているのにスクリプトが落ちていた
  - `.github/workflows/windows-build.yml` のビルドステップに `PYTHONUTF8: "1"` を追加（PyInstaller を含むステップ全体の Python 出力を UTF-8 化）
  - **ソース平文混入チェックを `src/` 配下のみに限定**。従来は dist 内の `.py` を 1 つでも検出すると配布中止していたが、PyInstaller onedir は torch/torchaudio 等 OSS ライブラリの `.py`（公開コードで IP 漏洩に当たらない）を必ず同梱するため 2301 件を誤検知していた。自分の proprietary な `src/` は voicekey.spec で `datas=[]`＋PYZ バイトコード格納なので平文混入しない設計を維持しつつ、チェック対象を `\src\` パスのみに絞った
  - **自動アップデータの version.json パースを BOM 耐性化**。`src/utils/updater.py` を `utf-8-sig` デコードに変更し、ビルドスクリプトは version.json を BOM 無し UTF-8 で書き出すようにした（PS5.1 の `Set-Content -Encoding UTF8` が付ける BOM で `json.loads` が落ち、自動アップデートが静かに効かなくなるのを防ぐ）
- **コードレビューで検出した追加バグを修正**（2026-06-14）
  - **自動アップデータのダウンロードが回線 stall で永久ブロックするのを修正**。`urllib.request.urlretrieve`（タイムアウト不可）から `timeout=30` 付き `urlopen` + ストリームコピーに変更し、回線が固まっても確実に打ち切れるようにした。あわせて version.json に `url` / `sha256` が無い場合は DL せず明示エラーにし、検証不能なインストーラを実行しないようにした（`src/utils/updater.py`）
  - **設定保存（`ConfigManager.save`）が手書きのネストキーを消すのを修正**。浅い `dict.update` を `_deep_merge` に変更し、`default_api_models` などネストした辞書内のカスタムキーが保存時に丸ごと失われないようにした（`src/config/config_manager.py`）
  - **ダーク／ライトのテーマ切替トグルが回転アニメーションしないのを修正**。`ThemeToggleButton.angle` を Python 組み込み `property` から Qt の `Property(float, ...)` に変更し、`QPropertyAnimation` が回転角度を駆動できるようにした（`src/ui/settings_window.py`）
- **ダブルタップ Enter 自動送信と録音中 UI の消去が約 0.5 秒遅れる問題を修正（Mac / Windows 両方）**（2026-06-16）。固定待ちが 2 か所あり、いずれも設定値ではないため「ミリ秒に設定しても効かない」状態だった:
  - **ダブルタップ確定後の離鍵で再び 0.4 秒待っていたのを撤廃**。2 打目の押下で確定済み（auto_enter）なのに、最後の離鍵が録音開始から 0.4 秒以内だと「2 打目待ち」の判定窓に巻き込まれ、短い録音ほど必ず 0.4 秒遅れて停止していた。確定後の離鍵は即座に録音停止するようにした（Mac: `AppController.handleRelease` / Windows: `app._on_release`）
  - **貼り付け後のクリップボード復元待ち（0.3 秒）を呼び出し元から切り離した**。貼り付け先が読み終えるまで 0.3 秒待ってから復元する処理が、Enter 送信と録音中 HUD の非表示を直列にブロックしていた。復元はバックグラウンドに逃がし、貼り付け直後に Enter 送信・UI 非表示へ進むようにした（Mac: `Paster.paste` を別タスク化 / Windows: `InputHandler.insert_text` を `threading.Timer` 化）。Windows の貼り付け前待機も Mac と揃えて 0.1→0.05 秒に短縮
  - 体感の Enter までの待ちは「貼り付け前待機 + Auto Enter Delay（設定値）」だけになり、0.4〜0.5 秒の固定遅延が消えた。回帰テスト `test_double_tap_latency` / `test_input_handler` で検証
- **コードレビューで検出した残りのバグを修正**（2026-06-16）
  - **録音末尾チャンクの取りこぼしを修正**。`AudioRecorder._do_stop` が `stream.stop()` の「前」に録音中フラグを倒していたため、stop() が内部バッファを流し終える間に届く最後の音声が callback で捨てられていた。フラグを stop() の後で倒すようにした（回帰テスト `test_audio_recorder`）
  - **`ConfigManager` をスレッドセーフ化**。config を listener / 設定監視 / UI の各スレッドが同時に read/write しても壊れないよう `RLock` で保護（`get` / `reload_if_changed` / `save`）
  - **設定ウィンドウが `ConfigManager` を二重生成していたのを解消**。本体と同一インスタンスを共有し、保存時にシグナル（`settings_saved`）で設定変更を即時適用するようにした（従来は最大 1 ポーリング周期ぶん反映が遅れていた）（`src/ui/settings_window.py` / `src/app.py`）
  - **自動アップデータのバージョン解析を堅牢化**。`v1.2.3` や `1.2.3-beta` / `+build` 形式でも `ValueError` で更新確認が無言で止まらないようにした（`parse_version`）
  - **ホットキー入力欄の不整合を修正**。クリックしてキーを押さずにフォーカスを外すと「内部状態は空・表示は旧値」になる問題を解消（`HotkeyInput.focusInEvent`）

### Changed
- **設定画面の補足説明文を削減**（Mac）。プロバイダー名・モデル名（Groq / Deepgram /
  llama-... 等）を晒す説明や冗長な解説を削除し、コントロール名だけでは意味が通じない
  2 項目（自動 Enter の遅延 / ハンズフリー切替キー）のみ短い説明に書き直して残した。
  ストリーミングのトグル名からも「（Deepgram）」表記を除去
- **HUD のハンズフリー停止ヒントの文言を「もう一度押すと停止」に変更**（Mac）
- **ハンズフリー録音中の HUD 表示を新設**（Mac）。トグル実効モードの録音中は
  状態ドットとメニューバーアイコンをティール色にし、ピル内に「ハンズフリー」ラベルと
  「もう一度で停止」ヒントバッジを表示。通常録音（赤）・自動送信（紫）と一目で区別できる
  - `AppState.recording` に `handsFree` を追加し、`recordingEffectiveMode == .toggle` を発信
  - `HudView` に `handsFreeAccent`（ティール）と `stopHint` バッジを追加
- **VAD の発話区間計算を共通化**（Windows `_speech_regions` / Mac `speechRegions` 抽出）。
  無音圧縮（analyze/condense）と分割（segment）が、同じ 1 回の推論結果を 2 通りの
  ギャップ閾値（0.5s 保持 / 0.7s 分割）でマージする方式に整理
- **Windows 版 設定 UI の全面再デザイン**（「ダサい・安っぽい」指摘対応。2026-06-13）
  - 上部タブ → **左サイドバーナビゲーション**（macOS System Settings 風。
    ブランド表示 + ページタイトルヘッダー、ウィンドウは 560x640 → 720x600）
  - 設定項目を**カード型セクション**にグルーピング（角丸の面 + 行間ヘアライン区切り。
    一般ページは 基本/音声処理/表示と動作/テキスト整形/起動 の 5 カード構成）
  - チェックボックスを **iOS 風トグルスイッチ**に置換（ToggleSwitch クラス新規。
    QCheckBox 互換 API・スライドアニメーション付き・自前描画）
  - styles.py の**デザイン体系を刷新**: グラデーション全廃のフラットモダン、
    3 層の面構成（サイドバー/コンテンツ/カード）、タイポグラフィ体系
    （タイトル 19px / 本文 13px / 補足 12px）、ライトの成功・警告色を白カード上で
    読めるコントラストに調整
  - **コンボボックス矢印のグリフ崩れを修正**: CSS ボーダートリック → SVG ファイル参照
    （Qt の QSS は data URI 非サポートのため一時ディレクトリへ書き出して url() 参照。
    スピンボックスの上下ボタンも同方式で統一）
  - 補足説明ラベルは objectName("caption") + QSS 化（テーマ切替に自動追従）

### Added
- **ハンズフリー切替キー**（グローバル設定 `handsfree_key`）
  - 切替キー＋既存ホットキーを同時押しすると、そのプロバイダーがトグル録音
    （1 回押して開始・もう一度で停止）になる。既存の長押し（hold）はそのまま維持
  - スロットは 2 個のまま。両プロバイダーをハンズフリー化できる。既定は空＝無効
  - Mac/Windows 両対応。録音中の「実効モード」を新設し release/toggle 停止判定が参照
- **長文の分割並列送信**（`split_parallel_enabled`・**既定オン**）
  - 12 秒超の録音を 0.7 秒超の無音区間で分割し、REST batch API（OpenAI/Groq/
    ElevenLabs）へ並列送信して index 順に結合。待ち時間を最遅セグメント分まで短縮
  - 区切りは無音の中だけなので語の途中では切れず精度への実害なし。一部失敗時は
    全体 1 本送信へフォールバック。VAD 推論は録音 1 回のまま（region 計算を共通化）
  - Deepgram ストリーミングは対象外（既にリアルタイム）
- **JIS（日本語）キーボード配列対応**
  - 記号キー（- = [ ] \ ; ' , . / `）と日本語専用キー（¥ / かな / 英数 / 無変換 /
    変換 / 半角全角）をホットキーに使用可能化（Mac: 物理キーコード、Windows: VK ベース）
  - 環境・IME により一部の日本語キーは pynput に届かず使えない場合がある
- **開発版／ベータ版の起動を 1 コマンド化**（「2 バージョンをはっきり区別したい」要望対応）
  - `macos/scripts/run_dev.sh` 新規: スタブ鍵で再ビルド → `/Applications/voicekey.app` へ
    インストール → 再起動。普段使いは常にこれ（API キータブが表示されるのが開発版の目印）
  - `macos/scripts/run_beta.sh` 新規: dist/ の最新配布 DMG をマウントしてベータ版を一時起動
    （テスターと同一物の動作確認用。終わったら run_dev.sh で戻る）
  - 背景: dist/voicekey.app はビルドのたびに dev/dist で上書きされ、どちらが動いているか
    分からなくなっていた（実際に配布ビルド後、ベータ版を常用し続ける状態が発生していた）
- **Windows 版ビルドの GitHub Actions 化**（PC またぎ・.env.dist 手運びの廃止）
  - `.github/workflows/windows-build.yml` 新規: windows-latest で
    `gh workflow run windows-build.yml -f version=X.Y.Z` → setup.exe + version.json を
    artifact で取得。キーは GitHub Secrets → 環境変数で注入（ランナーにファイルを置かない）
  - `docs/BUILD_WINDOWS.md` を GitHub Actions 標準・実機ビルドはフォールバックに再構成

### Fixed
- **未設定（空ホットキー）スロットが全キーで誤発火しうる潜在バグ**を修正（Windows `_slot_matches`）。
  `required_keys` が空のとき `all([])` が True になり、未設定スロットがどのキー押下でも
  一致していた。空のときは一致しないようガードを追加（ハンズフリー切替キー実装で
  `_slot_matches` を `required_keys` ベースに整理した際に表面化）
- **録音開始遅延の根本修正**（「キーを押してもマイクがすぐオンにならない」報告。
  実測: 押下→マイク実起動が初回 1511ms、2 回目以降 41〜69ms）
  - 原因①: 毎押下で全入力デバイスの HAL 列挙 + AUHAL 再構成をやり直していた
    （既存の prewarm は設定デバイスを見ておらず、初回押下のデバイス切替で
    温めた状態が捨てられる＝構造的に無効化されていた）
  - 原因②: 実 IO（AudioOutputUnitStart）の初回起動コスト（1 秒超）を録音時に支払っていた
  - 原因③: メインスレッドの Keychain 読み（API プリウォーム類）が recorder.start より
    先に走っていた
  - 修正: デバイス適用を「設定が変わったときだけ」に（AudioRecorder に適用済み UID を
    キャッシュ）、起動時 prewarm で設定デバイスを適用しダミータップで IO を一度
    起動・停止して前払い（**起動直後にマイクインジケータが一瞬点灯するのはこの
    ウォームアップ**。音声は記録しない）、beginRecording の順序を recorder.start 最優先に入替
- 入力デバイスを一度指定すると「システム既定」に戻せなかった（エンジンに前回のデバイスが
  固定されたまま残る）→ 既定デバイス ID の明示設定で復帰、既定デバイスの変更にも追従
- 録音中のマイク切断・構成変更でエンジンが静かに止まり、喋り続けても何も入らなかった
  → AVAudioEngineConfigurationChange を監視し、録音を確定（途中までの音声は変換）+ HUD 通知
- 録音開始失敗時に Deepgram ストリーミングセッションと chunkHandler が残留し、
  次の録音（別バックエンドでも）に Deepgram の結果が混ざり得た → 失敗時に後始末
- 長い口述（0.4 秒超）の直後 0.4 秒以内に次の録音を始めるとダブルタップ誤判定で
  auto_enter（Enter 自動送信）になっていた → 短いタップの離鍵だけを 1 打目として記録
- クリップボード復元が、貼り付け待ちの 0.3 秒間にユーザーがコピーした新しい内容を
  上書き破壊していた → changeCount を確認し、書き換わっていたら復元しない
- 連続録音で 1 件目がエラー通知を出すと、2 件目の変換が進行中でも HUD が消えていた
  → 通知の消灯時に変換中なら「変換中…」表示へ戻す
- マイク自動検出: 締め切り後に起動が完了した遅いデバイス（Bluetooth 等）が停止されず
  マイクを掴み続けた → 締め切り後の起動完了は即停止
- ダブルタップ確定（auto_enter 昇格）時に HUD の波形・ライブ字幕が一瞬消えていた
- スレッド競合の修正: AudioRecorder の chunkHandler / recording（audio スレッド vs
  メイン）、Transcriber の model/language/prompt（設定変更 vs 文字起こしタスク）を
  lock で同期
- URLSession の漸増リーク修正: StreamingTranscriber（録音ごとに生成）と
  バックエンド変更で捨てられる旧 Transcriber のセッションを明示破棄

### Technical Details
- **AudioRecorder.swift**: appliedDeviceUID キャッシュ + applyInputDevice() 抽出、
  prewarm の IO 前払い、AVAudioEngineConfigurationChange 監視（deviceChangedHandler）、
  stateLock による同期
- **AudioDevices.swift**: defaultInputDeviceID() 追加（プロパティ 1 回取得・軽量）
- **AppController.swift**: beginRecording の順序入替（デバイス反映 → WS → start →
  プリウォーム）、start 失敗時の streamer 後始末、handleRelease の lastRelease 記録条件変更
- **Paster.swift / Hud.swift / MicAutoDetector.swift / StreamingTranscriber.swift /
  Transcriber.swift**: 上記の各修正

## [1.0.1] - 2026-06-12

Mac 版 v1.0.1 を公開（アプリアイコン追加・DMG レイアウト改善）。
既存テスターには Sparkle の自動アップデート（build 3 → 4、差分配信付き）で届く。

### Added
- **Mac 版アプリアイコンを新規作成**（ユーザー要望。これまではジェネリックアイコン）
  - ダーク角丸スクエア + ブランドの青い波形バー（配布サイトの HUD と同モチーフ）
  - `scripts/dev/make_app_icon.swift` 新規: 1024px マスター PNG のジェネレータ
    （icns 変換コマンドはファイル冒頭コメントに記載）
  - `Resources/AppIcon.icns` をコミットし、`Info.plist` に CFBundleIconFile、
    `build_app.sh` に Resources へのコピーを追加。NSWorkspace 経由で解決確認済み

### Changed
- **Mac 版 DMG のレイアウトを一般的なアプリの形式に改善**（ユーザー要望）
  - 背景画像（660x400pt・2x）にドラッグ&ドロップ誘導の矢印と「右クリック→開く」の
    1 行ガイドを描画。左にアプリ・右に Applications・右上に手順書テキストを配置
  - `scripts/package_dmg.sh` 新規: UDRW で作成 → Finder (AppleScript) でアイコン位置・
    背景を .DS_Store に焼き込み → UDZO 変換 → 署名。`build_dmg.sh` の DMG 作成部を置換
  - `scripts/dev/make_dmg_background.swift` 新規: 背景画像ジェネレータ
    （生成物 `scripts/assets/dmg_background.png` はコミット対象）
  - 配布中の v1.0.0 DMG を同一バイナリ（build 3・CDHash 一致確認済み）のまま
    新レイアウトで再パッケージし、サイトへ再デプロイ（downloads.json のサイズも更新）

### Fixed
- **インストール手順を macOS 15 (Sequoia) の実挙動に合わせて修正**（実テスターからの報告）。
  Sequoia では DMG を開く段階で「マルウェアが含まれていないことを検証できませんでした」が
  出る（アプリ初回起動時だけではない）。同梱手順書と配布サイトに
  「完了で閉じる（ゴミ箱に入れない）→ プライバシーとセキュリティ →このまま開く」
  「警告は DMG とアプリ初回起動の最大 2 回」を明記
- `generate_embedded_keys.sh`: コピペで `--` が – / —（en/em ダッシュ）に化けた引数も
  受け付けるようにした（`–export-env` で引数なし扱い→スタブ生成になる事故が実際に 2 回発生）。
  あわせて不明な引数は黙ってスタブを生成せずエラー終了に変更（「できたつもり」事故防止）

## [1.0.0] - 2026-06-12 (無料テスト版・ベータ初回リリース)

Mac 版 v1.0.0 を公開。配布物（DMG・更新用 zip・appcast・version.json）はすべて配布ページ
https://voicekey.vercel.app（Vercel）から配信し、GitHub はテスターから一切見えない構成。
Windows 版は実機ビルド待ち。

### Changed（同日追記: 配布を Vercel に一本化）
- 当初 GitHub Releases + raw URL で公開したが、「ダウンロード URL からリポジトリの存在が
  見えるのを防ぎたい」というユーザー要望で、配布物をすべて Vercel サイト内
  （/downloads/・/mac/・/windows/）へ移設。サイトの最新版表示も GitHub API から
  サイト内 downloads.json 参照に変更
- `Info.plist` SUFeedURL / `updater.py` VERSION_URL / `build_dmg.sh` の appcast URL /
  `build_windows_dist.ps1` の version.json URL を voicekey.vercel.app 配下へ変更し、
  Mac 版を再ビルド（1.0.0 build 3。strings 平文キー 0 件・全配布 URL 200 を確認）
- voicekey-releases リポジトリは private 化（バイナリ置き場としての役目を終了）

### Added
- ベータ配布計画の開始（HANDOFF.md にゴール・フェーズ・恒久要件を記録）
- `.gitignore` に配布ビルド用キー生成物を追加（`.env.dist` / `src/config/embedded_keys.py` /
  `macos/Sources/Voicekey/Config/EmbeddedKeys.generated.swift`）。API キーは git に絶対コミットしない
- 配布用 `beta` ブランチを新設（開発は main、リリース時に main → beta マージで dist ビルド）
- **Mac 版 API キー埋め込み機構（テスター向け配布ビルド用）**
  - `macos/scripts/generate_embedded_keys.sh`: `--dist` で Keychain の現行キー4件を抽出し、
    ランダム 32 バイトマスクで XOR 難読化した `EmbeddedKeys.generated.swift`（git 管理外）を生成。
    引数なしはスタブ生成（isDist=false・キーなし、通常開発用）
  - `Keychain.swift`: キー解決を「Keychain → 環境変数 → 埋め込みキー」の3段フォールバックに拡張
  - `SettingsView.swift`: DIST ビルドでは API キータブを非表示（テスターの混乱防止）
  - `build_app.sh`: 生成ファイル未存在時のスタブ自動生成 + 署名 identity の環境変数
    `VOICEKEY_SIGN_IDENTITY` 対応（Developer ID への後日切替用）
  - 検証済み: XOR 復号ラウンドトリップ（swiftc 単体テスト）/ DIST バイナリの strings に平文キーなし /
    スタブビルドで通常起動
- **Mac 版 Sparkle 2 自動アップデート + 配布パイプライン（ベータ配布 Phase 2）**
  - `Package.swift` に Sparkle 2.9.3 を追加。`build_app.sh` が Sparkle.framework を
    Contents/Frameworks へ同梱し rpath を追加（SPM 手組みバンドルのため手動埋め込み）
  - `UpdaterController.swift`: DIST ビルド かつ .app バンドル実行時のみ Sparkle を起動
    （開発ビルドに更新ダイアログが出るのを防止）
  - メニューに「アップデートを確認…」（DIST のみ）と「フィードバックを送る…」（mailto）を追加
  - `Info.plist`: SUFeedURL（voicekey-releases の appcast）/ SUPublicEDKey / 自動チェック 24h
  - `build_dmg.sh` 新規: バージョン更新 → キー埋め込み（終了時に必ずスタブへ復元）→ ビルド →
    開発機 rpath 除去 → hardened runtime で inside-out 署名（ad-hoc はエラー終了）→
    DMG（/Applications リンク + 手順書同梱）→ Sparkle 用 zip → appcast 生成。
    `--identity` / `--notarize` は Developer ID 加入後にそのまま使えるパラメータ化済み
  - `voicekey.entitlements` 新規（hardened runtime 下のマイク使用に必須）
  - `dmg_readme.txt` 新規: Gatekeeper 回避手順（macOS 14/15 別）+ 音声が外部 STT API に
    送信される旨のプライバシー注意
  - 検証済み: 偽キーで v0.0.1 → v0.0.2 の DMG/zip/appcast を実ビルドし、ローカル HTTP サーバの
    appcast 経由で旧版が新版を検知 → バイナリ差分 DL → 終了時に自動インストールされ
    バンドルが 0.0.2 に置き換わる E2E を確認。codesign --verify --deep --strict 通過
- **Windows 版 配布一式（キー埋め込み・自動アップデート・インストーラ、ベータ配布 Phase 4）**
  - `scripts/build/generate_embedded_keys.py` 新規: `.env.dist` → 環境変数の順でキーを取得し、
    XOR 難読化した `src/config/embedded_keys.py`（git 管理外）を生成。keyring は意図的に不使用
    （macOS 検証時の実 Keychain ダイアログ事故防止）
  - `src/utils/secrets.py`: キー解決を「keyring → 埋め込みキー」フォールバックに拡張 +
    `is_dist_build()` 追加（未登録キャッシュ・keyring 例外・keyring 不在の全経路で埋め込みへ落ちる）
  - `src/utils/updater.py` 新規: 自前自動アップデータ。voicekey-releases の version.json を
    起動 60 秒後 + 24 時間ごとにチェック（DIST のみ）→ トレイ通知 + メニュー項目（モーダル禁止）→
    SHA256 検証付き DL → Inno `/VERYSILENT /CLOSEAPPLICATIONS` でサイレント更新 → 新版自動再起動
  - `src/ui/system_tray.py`: 更新通知（showMessage + 動的メニュー項目）と
    「フィードバックを送る…」（mailto・バージョン入り件名）を追加
  - `src/app.py`: Updater を SystemTray と配線（検知 → 通知、インストール → 終了要求 → _quit_app）
  - `src/ui/settings_window.py`: DIST ビルドで API キータブ非表示
  - `installer/windows/voicekey.iss` 新規: Inno Setup 6。AppId GUID 固定 /
    `PrivilegesRequired=lowest` + `{localappdata}\Programs\voicekey`（UAC なしでサイレント更新可能）/
    日本語 UI / スタートアップ登録 / 更新後の自動再起動
  - `scripts/build/build_windows_dist.ps1` 新規: APP_VERSION 更新 → キー埋め込み → PyInstaller →
    dist への .py 混入検査 → ISCC → SHA256 → version.json 出力 → embedded_keys.py 自動削除
  - `macos/scripts/generate_embedded_keys.sh` に `--export-env` 追加（Keychain から `.env.dist` を
    書き出し、Windows ビルド機へ手動コピーする受け渡し用。chmod 600）
  - `docs/BUILD_WINDOWS.md` 新規: Windows 実機ビルド手順 + 配布前チェックリスト
    （.py 非混入 / キー入力なしで動く / 自動更新 E2E / リリース順序: バイナリ添付 → version.json）
  - `tests/test_updater.py`・`tests/test_secrets_embedded.py` 新規（22 テスト。
    バージョン比較・SHA256 不一致時の非実行・キー解決チェーン・生成スクリプトのラウンドトリップ）

- **マイク自動検出（Mac 版・Windows 版、ベータ配布 Phase 5）**
  - 設定の入力デバイス行に「自動検出」ボタンを追加。押した後に喋ると、全入力デバイスを
    約 4 秒間同時監視し、声が入っているマイクを自動選択する（どのマイクか分からない人向け）
  - 判定: 30ms フレームの RMS を集め `score = p90 − p10`。喋り声は変動大・定常ノイズや
    ループバック系は変動小のため、単純な最大 RMS より誤選択が少ない（しきい値 0.005）
  - Mac: `MicAutoDetector.swift` 新規（デバイスごとに独立 AVAudioEngine を同時起動・使い捨て。
    録音用エンジンには触れない。開けない/ハングするデバイスはスキップ）+
    `SettingsView.swift` にボタンと「自動検出中… マイクに向かって喋ってください」進捗表示
  - Windows: `src/core/mic_auto_detect.py` 新規（使い捨てスレッドで全デバイスに sd.InputStream を
    同時オープン。AudioRecorder の永続ストリームには触れない）+ `settings_window.py` に
    ボタン・進捗ラベル（完了は Qt Signal でメインスレッドへ）
  - 検証: スコアロジックの単体テスト（Swift 5 ケース / Python 12 テスト・偽 sounddevice での
    同時監視統合テスト含む）、オフスクリーン UI スモーク、Mac ビルド+再起動。
    実マイクでの最終確認は要ユーザー操作（設定 → 自動検出 → 一言喋る）

- **Windows 版 UI 再デザイン（Mac 版の質感へ統一、ベータ配布 Phase 6）**
  - `src/ui/styles.py` をデザインシステム化: フォント定数（タイトル 18 / 本文 14 / キャプション 12px）、
    角丸定数（コントロール 8px / パネル 12px）、ボタン・選択タブの qlineargradient、
    macOS システムカラー追加（SUCCESS / WARNING / アクセントグラデ）、入力欄 hover、
    QSlider スタイル新規（4px groove + 18px 白グラデハンドル）、QCheckBox hover、
    ラベル用ヘルパー 5 種（title/caption/status_ok/status_warn/status_muted）
  - `src/ui/settings_window.py`: インラインスタイル 7 箇所を styles.py ヘルパーへ集約
  - `src/ui/system_tray.py`: トレイアイコンを原色ベタ塗り → macOS システムカラー
    （待機 #8E8E93 / 録音 #FF453A / 変換中 #FF9F0A / 自動 Enter #BF5AF2）+
    放射グラデーションのハイライト + 細い縁取りで立体感を付与
  - `src/ui/hud.py`: ピル背景を縦グラデーション化（.ultraThinMaterial 風）+ 白 10% ボーダー +
    paintEvent 内 3 層自前影（QGraphicsDropShadowEffect は透過窓で効かないため）。
    ドット色を Mac 版と統一（録音 #FF453A / 自動 Enter #BF5AF2）。表示タイミングは不変更（速度最優先）
  - `scripts/dev/preview_ui.py` 新規: オフスクリーンで設定全タブ（ダーク/ライト）・HUD 4 状態・
    トレイ 4 状態を PNG 出力する目視確認ツール（secrets/keyring はモック）
  - 検証: preview_ui.py のスクショ目視（全タブ × 2 テーマ + HUD + トレイ）、py_compile、
    unittest 111 件全通過

### Changed
- `voicekey.spec`: **`datas=[('src','src')]` を削除し `noarchive=False` 化**
  （配布物に Python ソース平文が同梱されていた問題の根治）。代わりに
  `collect_submodules('src')` で全モジュールを PYZ アーカイブへ収集
- **Apple Developer Program 不加入を恒久決定（2026-06-12 ユーザー確定）**。
  配布は Apple Development 署名 + 右クリック→開く手順書同梱が恒久形となり、
  Phase 7（Developer ID + 公証切替）を中止。`dmg_readme.txt` の
  「ベータ版のため公証が未対応（正式版で対応予定）」という文言を
  「この警告は配布方式によるもので、アプリ自体の安全性に問題はない。初回に
  一度開けば以後はダブルクリックで起動できる」に変更（HANDOFF.md も同時更新）

### Fixed
- 設定画面が DIST ビルドで起動時クラッシュするバグ（API キータブを作らないのに
  `_load_current_settings` が `_api_key_status` を参照していた。オフスクリーンスモークで発見し、
  タブ生成前の空辞書初期化で修正）
- 設定画面の一般タブに横スクロールバーが常駐するバグ（自動検出ボタン追加でデバイス行が
  窓幅 560px を超過。コンボを最小 200px + 伸縮に変更し、タブのスクロールを縦専用化）
- リアルタイムストリーミングが「表示されない」とき理由が分からない問題（Windows 版）。
  動かない構成（websockets 未導入 / Deepgram キー未設定 / どちらのホットキーも
  バックエンドが Deepgram でない）では警告ログだけ残して REST へ無言フォールバック
  していたため、設定画面のチェックボックス直下に阻害要因を診断表示するようにした。
  バックエンド変更・キー保存/削除・チェック切替で即時更新される。
  検証: チェーン全段の再現スクリプト（偽 WebSocket → 受信スレッド → シグナル →
  HUD 字幕描画）でコード経路の健全性を確認 + 診断ラベル 4 シナリオのオフスクリーン
  テスト + unittest 111 件全通過。
  注: 既定構成はホットキー 1=OpenAI / 2=Groq のため、チェックを入れても Deepgram
  バックエンドに変えない限りストリーミングは発動しない（これが診断表示で分かるようになった）
- マイク自動検出が「検出中…」のまま固まりうる問題（Windows 版・バグレビューで発見）。
  `mic_auto_detect.py` の検出後処理がストリーム破棄 → スコア集計の順だったため、
  WASAPI 排他デバイス等で `stop()`/`close()` がハングすると結果通知（on_done → UI 復帰）
  まで道連れになっていた。スコア確定と best 決定を先に行い、ストリーム破棄は
  別デーモンスレッドへ分離（ハングしても結果には影響しない）。unittest 111 件全通過

## [Unreleased] - 2026-06-10 (voicekey for Mac)

### Added
- **macOS ネイティブ版 `macos/` を新規作成（Swift / SwiftUI）**
  - 方針転換: 「voicekey for Mac（Swift ネイティブ）」と「voicekey for Windows（既存 Python 版がベース）」をそれぞれ最適な技術で開発する。本リポジトリはモノレポ構成とする
  - メニューバー常駐（`MenuBarExtra`、Dock 非表示）。アイコン色で状態表示（待機=テンプレート / 録音=赤 / 自動送信録音=紫 / 変換中=オレンジ）
  - **HotkeyMonitor**: CGEventTap（listen-only）によるグローバルキー監視。修飾キーの左右をデバイス依存ビットで厳密判定し、`tapDisabledByTimeout` 受信時は即時再有効化（pynput で起きていた「ホットキー永久無反応」を OS レベルで根治）
  - **AudioRecorder**: AVAudioEngine 録音 + 16kHz モノラル変換。エンジン操作は専用シリアルキューで直列化
  - **VoiceActivity**: 音量正規化（ゲイン上限 +20dB）+ Apple SoundAnalysis のオンデバイス ML 分類器による発話判定（フォールバックはエネルギーベース）+ 無音トリミング
  - **Transcriber**: OpenAI / Groq REST（WAV multipart、URLSession）。録音開始時の TLS プリウォーム付き
  - **Keychain**: API キー保存。サービス名は Python 版（keyring）と互換で、保存済みキーをそのまま読める
  - **Paster**: クリップボード貼り付け + 元内容の自動復元 + ダブルタップ自動 Enter
  - **HUD**: 録音中のみ画面下部中央に音声レベル連動の波形ピル（NSPanel、クリック透過、フルスクリーン上にも表示）。変換中スピナー / エラー・無音通知（2 秒）
  - **設定ウィンドウ**: 一般（言語・VAD・HUD・自動 Enter 遅延・ログイン時起動）/ ホットキー 1・2（キャプチャ式レコーダー・モード・バックエンド・モデル・プロンプト）/ API キー
  - **AppController**: 文字起こしパイプラインの直列チェーン化（録音順の挿入保証）、録音 300 秒上限の保険、起動時の権限チェック（マイク・入力監視・アクセシビリティ）とシステム設定への誘導
  - ビルド: SwiftPM + `scripts/build_app.sh`（.app バンドル組み立て + ad-hoc 署名）。`swift build` 警告ゼロ、起動スモークテスト済み

### Added (2026-06-10 追記)
- **ElevenLabs / Deepgram バックエンドを追加（Mac 版）**
  - ElevenLabs Scribe（`scribe_v1` / `scribe_v1_experimental`）: multipart + `xi-api-key` 認証。音声イベントタグ（笑い声など）は音声入力に不要なため無効化
  - Deepgram（`nova-2` 既定 / `nova-3`）: WAV 生バイト POST + `Token` 認証。`smart_format` 有効。言語未指定時は自動判定、nova-3 + 非英語は多言語モードに自動切替
  - `Transcriber` をバックエンド別のリクエスト構築/応答解析に再構成（`MultipartForm` ヘルパー新設）。Keychain サービス名は Python 版と互換（`voicekey.ElevenLabs` / `voicekey.Deepgram`）。設定 UI の API キータブ・バックエンド/モデル選択は全 4 バックエンド対応
- **ログイン時自動起動（Mac 版）**
  - 初回起動時に `SMAppService.mainApp.register()` を自動実行（Mac を開けば voicekey が必ず起動する）。設定画面の「ログイン時に起動」トグルでオフにすれば再登録しない
- **文字起こしベンチマーク基盤 `benchmark/` を新規追加**
  - 同一の日本語音声（短文 6.8s / 長文 41.7s、`say` で合成）を 4 バックエンドの各モデルに送り、レイテンシと CER（文字誤り率）を測定する `run_benchmark.py`
  - API キーは環境変数 / `.env` → Keychain（アプリと共用）の順で取得（中身は非表示）。`make_audio.sh` で音声生成、原稿 `.txt` が CER 採点の正解を兼ねる
  - 初回計測（OpenAI / Groq / ElevenLabs、Deepgram はキー待ち）: ElevenLabs scribe_v1 が最高精度（CER 0.0%/0.4%）だが長文 3.5s と低速、Groq whisper-large-v3-turbo が最速（385ms/742ms）で精度も良好（2.7%/5.4%）、OpenAI gpt-4o-transcribe は中速・長文 CER 7.6%

### Added (2026-06-10 追記 3)
- **各社の実在モデルを API から取得する `benchmark/list_models.py` を追加**
  - 推測でなく `/models` エンドポイントから最新モデルを確認。OpenAI は `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` / `gpt-4o-transcribe-diarize` / `gpt-realtime-whisper` / `whisper-1`、Deepgram は `nova-2` / `nova-3` を確認。ElevenLabs STT は `scribe_v1` / `scribe_v1_experimental` のみ（`/v1/models` は TTS 用で STT v2 は存在しない）
- **バッチ比較を全 4 社・全モデルに拡充（`run_benchmark.py`）。Deepgram 込みの確定計測**
  - レイテンシ（最速値）/ CER（短文・長文）:
    - Groq turbo: 330ms / 695ms、CER 2.7% / 5.4%（REST 最速）
    - Deepgram nova-3: 1084ms / 1475ms、CER 0.0% / 4.0%（長文でもレイテンシが伸びない・精度安定）
    - ElevenLabs scribe_v1: 1106ms / 3682ms、CER 0.0% / 0.4%（短文・長文とも最高精度だが長文は低速）
    - OpenAI gpt-4o-mini-transcribe: 861ms / 1937ms、CER 2.7% / 8.0%
  - 注意点: Deepgram は日本語出力に単語間スペースを挿入する（CER は空白除去後のため低いが、貼り付け時はスペース除去が必要）。`gpt-4o-transcribe-diarize` は長文で `chunking_strategy` 必須エラー（話者分離モデルはディクテーション用途では不適）
- **リアルタイムストリーミング速度測定 `benchmark/stream_benchmark.py` を追加（WebSocket）**
  - 同一音声を 1 倍速で WS 送信し、TTFB（喋り出して最初の字が出るまで）と確定レイテンシ（送信完了＝ホットキーを離した相当から最終確定まで）を測定
  - OpenAI Realtime は GA 仕様に対応（`session.update` で `session.type="transcription"`、`OpenAI-Beta` ヘッダ廃止）。**入力 PCM は 24kHz 以上必須**のため 16kHz テスト音声を `audioop.ratecv` で 24kHz にアップサンプルして送信
  - 結果（TTFB / 確定 / CER、短文・長文）:
    - Deepgram nova-3: 1049ms / **61ms** / 0.0%（短）、1089ms / **125ms** / 1.3%（長）。真のストリーミング（発話中に逐次確定）で離した瞬間ほぼ確定。最良
    - OpenAI gpt-realtime-whisper: 1051ms / 951ms / 0.0%（短）、1192ms / 668ms / 2.7%（長）。真のストリーミングだが確定は Deepgram より遅い
    - OpenAI gpt-4o-transcribe / mini: TTFB が音声長と同じ（発話中は字が出ず commit 後に一括）。確定は離してから 0.4〜2.8s。ライブ字幕は不可
  - デバッグ用 `benchmark/debug_openai_rt.py`（OpenAI Realtime の生イベント確認）も追加
  - 結論: HUD にライブ表示するなら Deepgram nova-3 が最適（確定 60〜170ms・精度安定・多言語）。OpenAI で揃えるなら gpt-realtime-whisper。ホールド入力で離してから一括でよいなら REST の Groq turbo が最速

### Added (2026-06-10 追記 4)
- **アプリ本体にリアルタイムストリーミング + ライブ字幕を実装（Mac 版）**
  - 新規 `StreamingTranscriber.swift`: Deepgram WebSocket（`URLSessionWebSocketTask`、外部依存なし）。録音中の 16kHz PCM を逐次送信し、暫定（interim）/確定（is_final）を受信
  - `AudioRecorder`: 逐次チャンク通知 `chunkHandler` を追加。全バッファ蓄積とは独立して送るため、ストリーミングが失敗しても同じ録音バッファで REST にフォールバックできる
  - `AppController`: backend=Deepgram かつストリーミング有効のとき WS 経路。ホットキーを離した瞬間に確定テキストを貼り付け、空・失敗時は REST フォールバック。録音破棄時は `cancel()`
  - HUD: 録音中に**ライブ字幕**を表示（発話しながら文字が伸び、最新の語尾が見えるよう頭を省略表示）。HUD 幅を 460pt に拡張（ピル自体は内容に追従）
  - 日本語スペース除去: Deepgram は日本語に単語間スペースを挿入するため、「前後どちらかが CJK 文字」のスペースのみ除去（英単語間スペースは維持）
  - 設定の一般タブに「リアルタイムストリーミング（Deepgram）」トグルを追加（既定 ON）
  - 既定モデル更新: Deepgram を **nova-3** に（ストリーミング最良）、ElevenLabs に **scribe_v2** を追加
- **ベンチに最新モデルを追加（検索＋API 実測で見落としを洗い出し）**
  - ElevenLabs **Scribe v2**（バッチ）: 短文 CER 0.0%（1255ms）だが**長文は 5.8% と scribe_v1 の 0.4% より精度後退**。利用可能 STT は scribe_v1 / scribe_v1_experimental / scribe_v2 の 3 つ（`probe_stt_models.py` で確定）
  - Deepgram **Flux**（`flux-general-multi`、会話向け新モデル、`/v2/listen`）: TTFB 885〜1107ms、確定 407〜546ms、CER 0.0%/2.7%。ターン検出ぶん確定が遅くホールド入力には nova-3 が上
  - ElevenLabs **Scribe v2 Realtime**（`/v1/speech-to-text/realtime` WebSocket、`scribe_v2_realtime`）: 短文 確定 266ms/CER 0.0% だが TTFB ~2.2s と高め、長文で確定取得に失敗（要調整）。公称 150ms は本計測では未再現
  - `debug_realtime_new.py` で Flux / ElevenLabs Realtime の生プロトコル（TurnInfo/event、partial/committed_transcript）を確定してから実装
  - **ストリーミング最終結論**: 離して→確定が最速・最精度なのは **Deepgram nova-3（確定 94〜109ms、CER 0.0%/1.3%）**。ライブ字幕の既定として最適。OpenAI 統一なら gpt-realtime-whisper（確定 ~800ms）。Flux は会話エージェント向け、ElevenLabs Realtime は現状 nova-3 に及ばず
  - 新規キーが要る有力候補（未計測・要サインアップ）: Soniox（日本語 WER 8.7% 主張・$0.12/h）、Speechmatics（月 40h 無料）、Mistral Voxtral Realtime（$0.006/min）

### Added (2026-06-10 追記 5) — Windows 版を Mac 版と同等まで刷新
- **ElevenLabs / Deepgram バックエンドを追加（Windows/Python 版、全 4 バックエンド対応）**
  - `TranscriptionBackend` 列挙型に `ELEVENLABS` / `DEEPGRAM` を追加（従来は groq/openai のみ）
  - `ElevenLabsTranscriber`（REST、`scribe_v1` 既定）: `xi-api-key` 認証 + multipart（`model_id` / `language_code`）。応答 JSON の `text` を採用。実 API で短文 CER 0.0% を確認
  - `DeepgramTranscriber`（REST、`nova-3` 既定）: `Token` 認証 + WAV 生バイト POST、`punctuate` / `smart_format` 有効。nova-3 は日本語単言語非対応のため `language=multi` に自動切替。日本語の単語間スペースを除去。実 API で短文 CER 0.0% を確認
  - `ApiTranscriber` 基底に `_auth_headers()` / `_raise_for_status()` / `_post()` を抽出し、4 バックエンドで共通化（重複削減）。Keychain サービス名は Mac 版と互換（`voicekey.Deepgram` 追加）
- **Deepgram リアルタイムストリーミング + HUD ライブ字幕を実装（Mac 版と同挙動）**
  - 新規 `src/core/streaming_transcriber.py`: Deepgram WebSocket（`websockets` 同期クライアント、受信は専用スレッド。アプリのスレッドベース設計に合わせ asyncio 不使用）。Mac 版 `StreamingTranscriber.swift` の移植
  - `AudioRecorder` に `chunk_callback` を追加。REST 用バッファ（`audio_q`）とは独立に録音中の生 PCM を逐次送出。ストリーミング失敗時は同じ録音バッファで REST に自動フォールバック
  - `app.py`: backend=Deepgram かつストリーミング有効のとき WS 経路。離した瞬間に確定テキストを貼り付け、空/失敗時は REST フォールバック。設定ホットリロード・ハング復旧・終了時に接続を確実に破棄
  - 接続確立前に `finish()` が呼ばれた短い発話向けに接続猶予（最大 1 秒）を実装し、取りこぼしを防止
  - 実 API 結合テスト: 短文はリアルタイム送信で完全一致（離して→確定 1.4s）、長文も全文取得を確認
- **録音中 HUD を新規実装（`src/ui/hud.py`、UI 刷新）**
  - 画面下部中央の小型ピル。録音中は音声レベル連動の波形バー、ストリーミング時はライブ字幕（頭省略で最新の語尾を表示）、変換中は「変換中…」、通知は 2 秒表示
  - **フォーカスを絶対に奪わない**設計（`WindowDoesNotAcceptFocus` + `WA_ShowWithoutActivating` + 入力透過）。貼り付け先ウィンドウのフォーカスを維持。タスクバー非登録
  - これまで表示先のなかった `notice` シグナル（エラー・無音検出）も HUD で可視化
- **設定 UI を全 4 バックエンド対応に拡張（`settings_window.py`）**
  - バックエンド選択に elevenlabs / deepgram を追加。モデル一覧をバックエンド別マップ（`_BACKEND_MODELS`）で差し替え、保存済みモデルが非対応なら既定（先頭）へ
  - Advanced タブに「リアルタイムストリーミング（Deepgram）」「録音中の HUD を表示」トグルを追加（保存・ロード対応）
- **既定値 / 依存 / パッケージング**
  - `constants.py`: `streaming_enabled` / `hud_enabled`（既定 ON）、`default_api_models` を全 4 バックエンド（deepgram=nova-3 / elevenlabs=scribe_v1）に拡張。`config_manager.py` の `API_BACKENDS` を 4 種へ
  - `requirements.txt` に `websockets>=13.0`、`voicekey.spec` に `collect_submodules('websockets')`（遅延 import のため明示収集）
- **ユニットテスト基盤を新規追加（`tests/`、これまで皆無）**
  - text_utils（CJK スペース除去）/ streaming_transcriber（メッセージ解析・PCM 変換・接続前 finish）/ api_transcriber（認証ヘッダー・言語解決・ステータス検査）/ config_manager（マイグレーション・正規化・既定）/ audio_utils（WAV ヘッダ・クリップ）の 38 テスト。ネットワーク/実機非依存で全パス

### Technical Details (追記 5)
- **types.py**: `TranscriptionBackend` に ELEVENLABS/DEEPGRAM、`TranscriptionTask` に `streamer` フィールド
- **app.py**: `_BACKEND_CLASSES` マップ、`interim_text` シグナル、`_active_streamer` 状態、`_insert_and_enter()` ヘルパー、ストリーミング優先＋REST フォールバックの `_process_task`
- **検証**: 全変更ファイル `py_compile` OK、オフスクリーン結合スモーク OK、実 Deepgram/ElevenLabs API で REST・ストリーミングとも一致、ユニットテスト 38 件パス。Windows GUI/ホットキー/トレイ/マイクの実機確認はユーザー側で実施（開発は macOS のため）

### Added (2026-06-11 追記 6) — 入力デバイス選択（Mac）とログイン時起動（Windows）
両プラットフォームに「相手側が既に持っていた機能」を追加し、parity を揃えた。

- **Mac 版に入力デバイス選択を追加**（Windows 版は既存）
  - Core Audio (HAL) で入力チャンネルを持つマイクを列挙する `AudioDevices`（新規）を追加
  - 設定の「一般」タブに入力デバイスのピッカーと更新ボタンを追加（「システム既定」+ 接続中のマイク。未接続の保存済みデバイスも選択を保持して表示）
  - 選択は安定した UID で永続化し（`AudioDeviceID` は再接続で変わるため）、録音開始のたびに `AVAudioEngine` の inputNode へ `setDeviceID` で適用
- **Windows 版にログイン時自動起動を追加**（Mac 版は既存 = SMAppService）
  - レジストリ Run キー（`HKCU\...\CurrentVersion\Run`）で管理する `autostart`（新規）を追加。`sys.platform` ガードで非 Windows では安全に no-op
  - 設定の「Advanced」に「ログイン時に起動」チェックボックスを追加（非 Windows では無効化＋注記）。状態はレジストリが真実なので settings.yaml には保存しない

### Technical Details (追記 6)
- **macos/Core/AudioDevices.swift**（新規）: `inputDevices()` / `deviceID(forUID:)`。`kAudioHardwarePropertyDevices` 列挙 + `kAudioDevicePropertyStreamConfiguration`（入力スコープ）でチャンネル判定
- **macos/Core/AudioRecorder.swift**: `inputDeviceUID` を追加し `start()` 内で `input.auAudioUnit.setDeviceID()` を実行
- **macos/Config/AppConfig.swift**: `@Published inputDeviceUID` を UserDefaults に永続化
- **macos/UI/SettingsView.swift** / **AppController.swift**: ピッカー追加、録音開始前に `recorder.inputDeviceUID = config.inputDeviceUID`
- **src/utils/autostart.py**（新規）: `is_supported()` / `is_enabled()` / `set_enabled()` / `_launch_command()`（凍結 exe か pythonw+run.py を引用符付きで組み立て）
- **src/ui/settings_window.py**: `_autostart_check` 追加（load=レジストリ実状態、save=`set_enabled`）
- **検証**: Mac `swift build` 成功、Windows `py_compile` OK・ユニットテスト 41 件パス（autostart 3 件追加）・オフスクリーンで設定ウィンドウ構築と非対応時の無効化を確認。Windows 実機でのレジストリ登録・自動起動はユーザー側で確認（開発は macOS のため）

### Added (2026-06-11 追記 7) — LLM テキスト自動整形（Mac / Windows）
文字起こし確定テキストを貼り付け直前に Groq の高速 LLM（既定 `llama-3.1-8b-instant`、Chat Completions）で 1 回整形する機能。主流ディクテーションアプリ（Wispr Flow / Superwhisper 等）と同等の後処理。

- **ホットキーごとにオン/オフ**（既定オフ）。片方は raw 高速・もう片方は整形済み、という使い分けができる
- **整形モード 6 種**（識別子・プロンプト文言は Mac / Windows で完全一致）: 自動クリーン（フィラー除去・句読点整形）/ 箇条書き / 丁寧（敬語）/ カジュアル / メール調 / カスタム（自由プロンプト。空なら自動クリーンにフォールバック）
- **発話を絶対に失わない**: 空入力は API 非呼出、キー未設定・タイムアウト（10 秒上限）・HTTP 非 200・応答不正・空応答・あらゆる例外は警告ログ + 原文をそのまま貼り付け。整形失敗で例外が貼り付け経路へ漏れることはない
- 整形モデルは設定（一般 / Advanced）で変更可能。ストリーミング確定・REST の両経路に適用

### Added (2026-06-11 追記 8) — 整形モード「おまかせ（自動判断）」と UI 改善
- **「おまかせ（自動判断）」モードを追加し既定に**（Mac / Windows、識別子 `auto`）
  - ユーザーが毎回モードを選ばなくても、LLM がテキストの内容から整形方法を自動判断する（フィラー除去は常時。列挙・手順なら箇条書き、それ以外は自然な文章。文体は元の発言を維持）
  - 自動判断の指示（システムプロンプト）は設定で自由に編集可能（Mac: 一般タブ「おまかせ整形の指示」+ 既定に戻すボタン / Windows: Advanced「Auto Format Prompt」）。空欄なら既定の指示を使用
- **整形モデルを自由入力からリスト選択に変更**（Mac / Windows 共通リスト）: llama-3.1-8b-instant（既定・最速）/ llama-3.3-70b-versatile / openai/gpt-oss-20b / openai/gpt-oss-120b。保存済みのリスト外モデルも選択を保持
- Technical: Mac `FormatMode.auto` + `defaultAutoPromptBody` + `TextFormatter.knownModels`、`ConfigStore.autoFormatPrompt`（UserDefaults 永続化）。Windows `DEFAULT_AUTO_PROMPT` / `KNOWN_FORMAT_MODELS`、`format_auto_prompt` 設定（空 = 既定で保存し既定文の将来更新に追従）。`build_system_prompt` / `format_text` に `auto_prompt` 引数追加。テスト 5 件追加（全 58 件パス）。Mac は実機でモード選択 UI・一般タブの編集欄・既存スロット設定の温存を確認

### Added (2026-06-11 追記 9) — モデル選択リストに「（推奨）」表記
- **音声（文字起こし）・文字（整形）両方のモデル選択リストで、推奨モデルの表示名に「（推奨）」を付けた**（Mac / Windows）
  - 表示ラベルだけの変更で、保存値・API へ送る値はモデル識別子のまま（Mac: Picker tag / Windows: QComboBox userData）
  - 推奨 = ベンチ実測 2026-06-10 に基づく各バックエンドの既定: OpenAI `gpt-4o-mini-transcribe` / Groq `whisper-large-v3-turbo` / ElevenLabs `scribe_v1`（日本語最高精度。v2 は長文後退）/ Deepgram `nova-3` / 整形 `llama-3.1-8b-instant`（速度テスト実行待ちの暫定）
  - Mac の knownModels の並びを「先頭＝既定＝推奨」に統一（OpenAI を mini 先頭、ElevenLabs を scribe_v1 先頭へ。Windows と同順序に）。保存済みの選択はそのまま温存される
- **整形モデル速度ベンチ `benchmark/format_speed_bench.py` を追加**: 全整形モデルに同一リクエスト（おまかせプロンプト + フィラー多め日本語 133 文字 × 3 回）を送りレイテンシのみ計測。キーは環境変数 → Keychain の順で取得し表示しない（.env は読まない）

### Changed (2026-06-11 追記 10) — 整形モデル速度ベンチの実測で推奨を確定・廃止モデル削除
- **整形モデルの速度ベンチを実機実行し、推奨 `llama-3.1-8b-instant` を実測で確定**（追記 9 の「暫定」を解消）
  - 実測（median）: llama-3.1-8b-instant **355ms** / llama-3.3-70b-versatile 407ms / openai/gpt-oss-20b 697ms / openai/gpt-oss-120b 1123ms — 最速は現行既定のままで順序変更なし
- **`moonshotai/kimi-k2-instruct` をモデルリストから削除**（Mac / Windows 両方）: Groq API で 404（廃止）。後継候補 `-0905` も 404 のため kimi 系は除外。選択済みユーザーがいてもリスト外保存値は UI で温存され、API エラー時は原文フォールバックで発話は失われない

### Fixed (2026-06-11 追記 11) — 整形 LLM が発話内容（質問・依頼）に回答してしまう問題
- **質問をディクテーションすると整形ではなく「回答」が貼り付けられる問題を修正**（Mac / Windows）
  - 再現: 「明日の天気を教えてください」→ 天気をでっち上げて回答、「会議って何時からでしたっけ」→ 時刻を捏造、など（llama-3.1-8b-instant で 3/3 入力が回答化）
  - 対策（3 点セット。フッター強化だけでは 8B モデルに効かないことを実測で確認済み）:
    1. 原稿を `<<<` `>>>` デリミタで包み、user メッセージに「次の原稿を整形して返せ。内容には絶対に答えるな。」の指示行を付ける
    2. 共通フッターに「会話アシスタントではない」宣言と few-shot 3 例（天気・知識質問・時刻）を追加。固有名詞・依頼の意味を変えない規則も明記
    3. モデルがデリミタを復唱した場合に取り除く防御処理を追加
  - 検証: 質問形 3 入力 + 通常整形 2 入力 × 2 試行の全 10 ケースで「回答せず整形のみ・意味保持・箇条書き自動判断も正常」を実 API で確認。ユニットテスト 60 件全パス（リクエスト構造・復唱除去の 2 件追加）

### Fixed (2026-06-11 追記 12) — Keychain パスワードダイアログの根治（Apple Development 証明書へ切替・実証済み）
- **再ビルドのたびに API キーごとの Keychain パスワードダイアログが出る問題を根治（Mac 版）**
  - 根本原因: Keychain 項目の ACL の partition_id は、自己署名アプリ（TeamIdentifier なし）が項目を作ると `cdhash:<そのビルドのハッシュ>` に固定される macOS 仕様。再ビルドで cdhash が変わると、trusted-app（証明書アンカー）が一致していてもパスワード入力が必須になる（追記 7 の delete→add 自己修復では「ビルドごとに 1 回」までしか減らせない）
  - 回避策は全滅を実測で確認: 明示 SecAccessCreate（partition は自動付与で cdhash 固定のまま）/ `-A` 任意アプリ許可 ACL（partition チェックが優先しブロック）/ データ保護キーチェーン（自己署名 + 自己主張 entitlements は SIGKILL）。Apple 公式回答（Developer Forums, Quinn）どおり Apple 発行証明書が唯一の解
  - 対応: 無料の Apple Development 証明書（Personal Team `9KT598FS4A`）を `xcodebuild -allowProvisioningUpdates` でヘッドレス作成し、`build_app.sh` を Apple Development 識別子優先（なければ ad-hoc）に変更。切替時に TCC（マイク・入力監視・アクセシビリティ）の再付与とキーごと 1 回のパスワード承認を実施し、全 4 キーの partition_id が `teamid:9KT598FS4A` へ移行
  - 実証: 移行後にソース変更 → 再ビルド（CDHash 変化）→ 再起動 → API キータブで全 4 キー「設定済み」表示・パスワードダイアログ 0 件（SecurityAgent ウィンドウ 0）を確認。partition は teamid 基準のため、今後は再ビルドでも年 1 回の証明書更新でもダイアログは出ない

### Fixed (2026-06-11 追記 13) — 整形で「やることは…」のような導入文が出力から消える問題
- **列挙を話すと箇条書きだけが貼り付き、導入文が捨てられる問題を修正**（Mac / Windows）
  - 再現: 「今日やることは二つあります。一つ目はご飯を食べる。二つ目は顔を洗う」→「- ご飯を食べる / - 顔を洗う」だけが出力され、何のリストか分からない
  - 対策: auto / bullets のプロンプトに「導入・前置きの文は削除せず、箇条書きの前の行にそのまま残す」規則と具体例（「持ち物は三つです。えーと、財布と、鍵と、あと定期」→ 導入文 + 箇条書き）を追加。共通フッターにも「フィラー語以外の情報（導入・前置きの文を含む）を省略しない」を明記
  - **Mac 版の「既定プロンプト改善が反映されない」隠れバグも修正**: 既定の auto プロンプトが UserDefaults にそのまま永続化されており、アプリ更新で既定文を改善しても古い文が使われ続けていた。Windows 版と同じ「既定文と同じ内容は保存しない」方式に変更し、未編集ユーザーには常に最新の既定文が使われるようにした（保存済みの旧既定文は削除済み）
  - 検証: 実 API（llama-3.1-8b-instant）で「導入文 + 列挙（auto / bullets）」「緩い導入（買い物リストなんだけど）」「質問に答えない」「通常文を箇条書きにしない」の 5 ケース × 2 試行すべて期待どおりを確認。ユニットテスト 60 件全パス

### Changed (2026-06-11 追記 14) — Windows 版 UI を Mac 版と同等に刷新
- **設定ウィンドウを Mac 版と同じ 4 タブ構成（一般 / ホットキー 1 / ホットキー 2 / API キー）に再構成**（`settings_window.py`）
  - サイドバー 2 ページ（General / Advanced）構成を廃止し、QTabWidget でタブ化。全ラベル・補足説明文を Mac 版 SettingsView.swift と同一の日本語文言に統一（「押している間 / トグル」「無音を自動スキップ（VAD）」「専門用語や固有名詞のヒントを入力」等）
  - **API キータブを新設**: 4 バックエンド（OpenAI / Groq / ElevenLabs / Deepgram）のキーを 1 か所で保存・削除。「設定済み / 未設定」を色付きで表示し、従来ホットキー設定の中に埋もれていたスロット別キー入力を廃止
  - 言語は自由入力から「日本語 / 英語 / 自動判定」の選択式に変更（空文字 = API 側の自動判定。全バックエンド対応済みの既存挙動）
  - 各タブは縦スクロール対応。保存値がリスト外でも選択を保持する既存ポリシーは言語・整形モデルにも適用
  - Windows 版固有設定（VAD 最小無音時間・音量正規化・ダークモード切替・自動 Enter スライダー）は一般タブに統合して維持
- **HUD を Mac 版 Hud.swift と同寸・同表現に刷新**（`hud.py`）
  - 460×56 / 波形バー 24 本（旧 360×48 / 20 本）。ピルが Mac 版カプセル同様に内容の幅へ縮む
  - 変換中の回転スピナー（約 30fps、表示中のみタイマー駆動）と、自動 Enter 録音時の紫 ⏎ バッジを追加
- **システムトレイの表記を voicekey に統一**（`system_tray.py`）: ツールチップの旧称 SuperWhisper を「voicekey - 待機中 / 録音中 / 録音中（自動 Enter）/ 変換中」へ、メニューを「設定… / 強制リセット（フリーズ復帰）/ 終了」の日本語表記（Mac 版メニューバーと同文言）へ変更
- **Technical Details**: `styles.py` に QTabWidget/QTabBar・QPlainTextEdit・QScrollArea スタイルを追加（不要になったサイドバー用 QListWidget スタイルは削除）。設定値の保存形式（settings.yaml のキー・値）は不変のため後方互換。検証: ユニットテスト全パス + offscreen スモーク（4 タブ構築・バックエンド切替でモデル候補差し替え・保存 dict 整合・HUD 全状態の描画）+ スクリーンショット目視確認

### Changed (2026-06-11 追記 15) — 整形モード撤去・プロンプト全面刷新（すべておまかせに一本化）
- **整形モード選択（自動クリーン / 箇条書き / 丁寧 / カジュアル / メール調 / カスタム / おまかせ）を廃止**（Mac / Windows）
  - 整形は常に「LLM の自動判断」一本に。スロット設定はオン/オフのトグルだけになり、モード Picker とスロット別カスタムプロンプト欄を削除（整形指示の編集は一般タブ「整形の指示」に集約、既定に戻すボタンは維持）
  - 設定の後方互換: 保存キー（Windows `format_auto_prompt` / Mac `autoFormatPrompt`・`formatEnabled`）は維持。旧 `format_mode` / `format_custom_prompt`（Mac: `formatMode` / `formatCustomPrompt`）は読み捨てされるだけで設定リセットは起きない
- **既定プロンプトを市販音声入力アプリの調査に基づき全面書き直し**（約 870 → 約 490 文字に半減。プロンプト長は prefill 時間に直結するため速度改善）
  - 調査対象: Wispr Flow / superwhisper / VoiceInk / Aqua Voice / Dragon 等の整形機能（フィラー除去・自己訂正の解決・句読点と段落・リスト自動整形・数字や日付の表記正規化・文体維持・質問に答えないガード）
  - 新プロンプトの構成: フィラーと無意味な繰り返しの削除 / 言い直しは最終発言のみ残す（例付き）/ 句読点・改行・段落と数字・日付・時刻の表記 / 列挙・手順はリスト化してよい（緩い指定）+ 導入文は残す / 文体維持・要約禁止
  - 共通フッター（質問に回答しない・`<<< >>>` デリミタ・出力形式）は文言を圧縮しつつ全ガードを維持（追記 11・13 の回帰なし）
- **Technical Details**: `text_formatter.py` / `TextFormatter.swift` から `_MODE_PROMPTS` / `FormatMode` を削除し `DEFAULT_FORMAT_PROMPT`（Mac: `TextFormatter.defaultPrompt`）に統一。`build_system_prompt(prompt)` / `format_text(text, model, prompt)` にシグネチャ変更。`HotkeySlot` / `HotkeySlotConfig` / `SlotConfig` から mode/custom フィールド撤去。テスト 16 件を新仕様に書き直し全 57 件パス。実 API で「列挙→導入文+リスト」「質問に回答しない」を確認

### Fixed (2026-06-11 追記 16) — 録音開始の高速化とダブルタップ・短音声の取りこぼし修正
- **ダブルタップ（自動 Enter）の 1 打目から録音が途切れないように**（Mac / Windows）
  - 旧挙動: 1 打目の離鍵で録音停止（短すぎて破棄）→ 2 打目で録音を再開、のためタップと同時に話し始めた文頭が欠けていた
  - 新挙動: hold モードで押下から 0.4 秒未満の離鍵では録音を止めず、2 打目を待つ（2 打目が来たらそのまま録音継続 + auto_enter 化、来なければ通常確定）。1 打目のタップ中・タップ間の音声もすべて録音される
  - 通常のホールド入力（0.4 秒以上）は従来どおり離した瞬間に確定（待ち時間の追加なし）
  - 誤タップ（無音の短いタップ）は「音声が検出されませんでした」を出さず静かに破棄
- **短い発話が「音声が検出されませんでした」になる問題を修正**（Mac 版）
  - 原因: SoundAnalysis 分類器の解析窓が 1 秒のため、1 秒未満の音声では分類結果が一度も出ず、その場合に「声なし」と誤判定していた（エネルギー判定へのフォールバックが効いていなかった）
  - 修正: 約 1.2 秒未満の音声は最初からエネルギー判定を使用 + 分類結果ゼロ件は「判定不能」としてエネルギー判定にフォールバック
- **録音開始そのものを高速化**（Mac 版）: 起動時と録音停止直後に CoreAudio 入力ユニットの初期化と `engine.prepare()` を済ませておき、ホットキー押下から実際に音が録れ始めるまでの遅延を最小化（`AudioRecorder.prewarm()` 新設。マイク自体は起動しないため常時録音やインジケータ点灯はない）
- **Technical Details**: AppController に `pendingTapFinish` / `recordingStartedAt`、`finishRecording(quietIfNoSpeech:)`。app.py に `_pending_tap_timer`（threading.Timer、_state_lock 保護）、`TranscriptionTask.quiet_if_no_speech`。`VoiceActivity.SpeechObserver` に `resultCount`。Windows 版 VAD（Silero、32ms フレーム）は短音声に強いため変更なし

### Changed (2026-06-11 追記 17) — 文字起こしまでの処理時間を総合削減（精度は不変）
- **発話間の長い無音を圧縮してから送信**（Mac / Windows、REST 経路のみ）
  - 従来は前後の無音しか切っておらず、話の途中で考え込んだ無音はすべて API に送られていた。各発話区間の前後 250ms の余白を残し、区間間の無音を最大 0.5 秒まで保持して圧縮する（元の無音が約 1 秒以下なら切らない）。長考した分だけアップロードと API 処理が丸ごと縮む
  - ポーズは句読点・文区切りの推定材料になるため 0.5 秒残す設計（完全には消さない）。語頭・語尾は余白で保護。**リアルタイム（Deepgram ストリーミング）経路は対象外**（録音中に逐次送信済みのため圧縮しても速くならない）
- **FLAC ロスレス圧縮でアップロードを約 4 割削減**（Mac 版、全 4 バックエンド）
  - WAV の代わりに FLAC（16bit、量子化は WAV と同一）で送信。実測で WAV 比 61%。可逆圧縮のため精度への影響はゼロ（ラウンドトリップ検証で maxDiff = 量子化誤差 4.6e-5）。エンコード失敗時は WAV に自動フォールバック
  - 実 API 検証: 無音圧縮 + FLAC の音声を 4 社に送信し、Groq CER 2.6% / OpenAI 2.6% / ElevenLabs 2.6% / Deepgram 0.0%（WAV ベースラインと同等、発話 2 区間とも完全保持）
  - Windows 版は標準ライブラリで FLAC を生成できないため見送り（依存追加が必要。実機検証とセットで別途）
- **VAD 自体の処理時間を削減**（精度・判定結果は同一）
  - Mac: SoundAnalysis 分類器に 0.5 秒ずつ流し、speech 検出時点で打ち切る早期終了を追加。判定は「どこかに speech があるか」の OR なので結果は全量解析と同一。実測 6.8s+25s 無音の音声で 33ms（発話が冒頭にあるほど・録音が長いほど効く）
  - Windows: has_speech と speech_bounds が同じフレーム推論を 2 回実行していたのを `analyze()` 1 回に統合（VAD 時間が半減）。無音圧縮も同じ推論結果を共用
- **整形 LLM の接続を使い回し + 録音中に事前確立**（Mac / Windows）
  - Windows: 整形のたびに httpx.post が TCP+TLS を張り直していた（毎回 100〜300ms 上乗せ）のを keep-alive 付き共有クライアントに変更
  - 両 OS: 録音開始時（整形が有効なスロットのみ）に整形 API への接続も温める（文字起こし API の prewarm と同パターン）。停止後の整形リクエストはハンドシェイク済みの接続で即送信される
- **Technical Details**: `VoiceActivity.condense()`（speechBounds を置換・吸収）/ `FlacEncoder.swift`（新規、AVAudioFile + CoreAudio 内蔵エンコーダ）/ `Transcriber.EncodedAudio`（FLAC/WAV の filename・contentType を保持）/ `TextFormatter.prewarm()`。Windows は `SileroVad.analyze()`（has_speech / speech_bounds を置換）/ `text_formatter._get_client()` + `prewarm()`。検証: ユニットテスト 68 件全パス（VAD は実 Silero ONNX で新規 7 件）、Swift 実装は実コードをリンクした検証ハーネスで 11 項目全パス、実 API 4 社で CER 劣化なしを確認、Mac ビルド成功・新ビルド起動確認済み

### Added (2026-06-12 追記 18) — 音声入力履歴（直近 10 件をクリップボードへ再コピー）
- **Mac 版: 音声入力の履歴を最大 10 件保存し、設定画面から再コピーできる「履歴」タブを追加**
  - 文字起こし（整形後）のテキストを貼り付けのたびに自動で履歴へ記録（貼り付け失敗時の救出にもなるよう貼り付け前に記録）。ストリーミング・REST 両経路に対応
  - 設定ウィンドウに「履歴」タブを新設（一般 / ホットキー 1・2 / 履歴 / API キー の 5 タブ構成）。行をクリックでクリップボードにコピーし「コピーしました」を 1.5 秒表示。各行に日時、下部に「履歴を消去」ボタン
  - 履歴は `~/Library/Application Support/voicekey/history.json` に保存（アプリ再起動後も残る・この Mac の外には出ない）。11 件目以降は古いものから自動削除
- **Technical Details (Mac)**: `Core/HistoryStore.swift`（新規、`@MainActor ObservableObject`・iso8601 JSON 永続化・atomic write）、`AppController.history` + 両貼り付け経路で `history.add(output)`、`SettingsView` に `HistoryTab`/`HistoryRow`（行全体クリック領域・lineLimit 2）。検証: swift build / build_app.sh 成功、新ビルド起動、空状態と 3 件表示をスクリーンショットで確認
- **Windows 版: 同等の「履歴」タブを追加（5 タブ構成に）**
  - 貼り付けの単一地点 `_insert_and_enter` で履歴に自動記録（ストリーミング・REST 両経路をカバー）。履歴は settings.yaml と同じディレクトリの `history.json` に保存
  - 履歴タブ: 行クリックで全文をクリップボードにコピーし「コピーしました（n 文字）」を 1.5 秒表示。80 文字超は省略プレビュー（全文はツールチップ）、各行に日時、「履歴を消去」ボタン、空時はプレースホルダー。タブ切替・ウィンドウ表示のたびに最新化
- **Technical Details (Windows)**: `src/core/history.py`（新規、`HistoryStore`・threading.Lock・一時ファイル経由の atomic 置換・壊れた JSON は空で復帰）、`app.py` に `self._history` + `SettingsWindow(history=...)`、`settings_window.py` に `_create_history_tab` / `_refresh_history` / `_copy_history_item` / `_clear_history`、`styles.py` に QListWidget テーマ。検証: ユニットテスト 77 件全パス（履歴 9 件新規）、offscreen スモークでタブ構成・クリック→クリップボード一致・消去・ストアなし時の安全動作を確認

### Fixed (2026-06-12 追記 19) — API キーのパスワード承認ダイアログ再発を根治（自己修復 write を撤去）
- **再発原因**: 読み取り時の「自己修復移行 write」（追記 7）が残っていたこと。起動のたびに鍵項目を delete→add で作り直すため、(a) 他プロセス（テスト用 python の keyring 等）に与えた承認が毎回消えてダイアログが再発、(b) ad-hoc 署名の実行（debug ビルド・検証ハーネス等）が一度でも鍵を読むと項目所有が cdhash 固定に退行し、次の正規ビルドでパスワード要求が再発する退行ベクトルになっていた。Apple Development 証明書移行（追記 12）の実行装置としては役目を終えていた
- **修正**: `Keychain.apiKey()` の自己修復 write を撤去（保存経路 `setApiKey` の delete→add は維持）。承認ダイアログが再発した場合は設定画面からキーを 1 回再保存すれば現アプリ所有で作り直される
- **検証**: 4 項目の partition_id が `[teamid:9KT598FS4A, cdhash:...]` であることを ACL メタデータの per-item 診断で実測（秘密値は読まない）→ 修正版を再ビルド（CDHash 変化）→ 再起動 → API キータブで 4 キーとも「設定済み」表示・ダイアログ 0 件・鍵項目の更新日時が不変（起動毎の作り直しが停止）

### Fixed (2026-06-11 追記 7)
- **API キー使用のたびに Keychain の承認ダイアログが出る問題（Mac 版）**
  - 原因: Python 版 keyring や旧 ad-hoc 署名ビルドが作成した Keychain 項目は ACL 上の所有者が「別アプリ」のため、現在の署名アプリの読み取りで毎回承認を求められていた
  - 修正 1: 保存処理を `SecItemUpdate` から **`SecItemDelete` → `SecItemAdd`** に変更し、項目を常に現アプリが新規作成して所有権を取る
  - 修正 2: 読み取り成功直後に同じ値で書き直す**自己修復移行**を追加（環境変数フォールバック経路では行わない）。既存ユーザーは各キーにつき**次回の読み取りで 1 回だけ承認すれば以後ダイアログが出なくなる**（キーの再入力は不要）

### Technical Details (追記 7)
- **macos/Core/TextFormatter.swift**（新規）: `FormatMode` enum（clean/bullets/polite/casual/email/custom + 日本語ラベル + システムプロンプト）と `TextFormatter`（ephemeral URLSession、リクエスト 10 秒、temperature 0.2、失敗時は原文返しで throws しない）
- **macos/Core/Keychain.swift**: `write()` を delete→add 化、`apiKey(for:)` に自己修復移行を追加
- **macos/Config/AppConfig.swift**: `SlotConfig` に `formatEnabled` / `formatMode` / `formatCustomPrompt`（既定値付き）。手書き `init(from decoder:)` を extension に実装し、既存ユーザーの保存スロットを decodeIfPresent + 既定値で後方互換読み込み（設定リセット防止）。`ConfigStore` に `@Published formatModel`
- **macos/UI/SettingsView.swift**: スロットタブに整形トグル + モード Picker +（custom 時）プロンプト欄、一般タブに整形モデル欄
- **macos/AppController.swift**: ストリーミング / REST 両経路の `Paster.paste` 直前で `formatter.format()` を適用
- **src/core/text_formatter.py**（新規）: `build_system_prompt` / `format_text`（httpx、Keychain → `GROQ_API_KEY` 環境変数の順でキー解決、失敗時原文）
- **src/config/constants.py / types.py**: `format_model`（グローバル）と hotkey1/2 の `format_enabled` / `format_mode` / `format_custom_prompt` を追加
- **src/app.py**: `_maybe_format` ヘルパーを追加し、ストリーミング確定・REST 完了の両経路で `_insert_and_enter` 直前に適用
- **src/ui/settings_window.py**: 各ホットキーに整形チェックボックス + モード Combo +（カスタム時のみ表示の）プロンプト欄、Advanced に Format Model 欄
- **検証**: Mac `swift build` エラー/警告 0 → `build_app.sh` → 実機再起動し、スロットタブの整形 UI（トグル → モード Picker → カスタム欄の段階表示）と既存設定の後方互換読み込み（ホットキー・バックエンドがリセットされないこと）をスクリーンショットで確認。Windows `py_compile` OK・ユニットテスト 53 件（新規 13 件含む）全パス・オフスクリーンで設定 UI 構築を確認。Groq への実呼び出しとKeychain 承認ダイアログ消滅は実ディクテーションでの確認待ち

### Fixed (2026-06-10 追記 2)
- **ホットキーがほとんど反応しない問題（Mac 版）**
  - 原因 1: ウィンドウを 1 つも持たないメニューバーアプリは App Nap の対象になり、イベントタップのコールバックが遅延 → OS にタイムアウト無効化されてホットキーが死ぬ。`beginActivity` と Info.plist `NSAppSleepDisabled` で App Nap を無効化し、タップ監視スレッドの QoS を `.userInteractive` に引き上げ
  - 原因 2: ad-hoc 署名はビルドごとに署名が変わり、入力監視の TCC 許可と不一致になる（タップは作成できるが OS が無効化し続ける）。ビルドスクリプトを自己署名証明書 `voicekey-codesign` があればそれで署名するよう変更（証明書の信頼登録はユーザー操作が必要）
  - 防御策: タップスレッドに 5 秒間隔のウォッチドッグを追加し、無効化通知の取りこぼしでも自動復旧。無効化理由（timeout/userInput）のログも追加
  - **根本原因（確定）**: ad-hoc 署名はビルドごとに署名 ID が変わり、入力監視の TCC 許可と不一致になるため OS がタップを無効化し続けていた（5 秒ごとに発生）。自己署名証明書 `voicekey-codesign` で署名を固定し、`tccutil reset` 後に 1 回だけ許可し直すことで完全解決。再ビルド→再起動でもタップ無効化 0 回・権限再要求なしを実機で確認
  - 署名固定の手順は `macos/README.md` の「署名について」に記載
- **設定画面の API キー・プロンプト入力欄が見えない問題（Mac 版）**
  - グループ化フォーム内の SecureField / TextField は枠が描画されず、プレースホルダがただのテキストに見えて入力欄と認識できなかった。`.textFieldStyle(.roundedBorder)` で明示的に枠付きに変更（API キー 4 欄・プロンプト・自動 Enter 遅延）
  - `fixedSize()` による高さ潰れの可能性も排除し、設定ウィンドウを明示サイズ（480×520）に変更
  - デバッグ用の設定ウィンドウ自動表示フラグ（`VOICEKEY_OPEN_SETTINGS`、一回限り）を追加し、スクリーンショットでの実機検証を可能にした

### Fixed
- **メニューバーアイコンが表示されない問題（Mac 版）**
  - 原因 1: SwiftUI `MenuBarExtra` がラベルの `NSImage` を正しく描画できない（青い円になる）
  - 原因 2: アイテム位置の永続化値（`NSStatusItem Preferred Position`）が不可視領域（ノッチ下・Hidden Bar の隠し領域）を指すと二度と表示されない。ユーザー環境では Hidden Bar が新規アイテムを隠し領域に配置していた
  - 対策: `MenuBarExtra` を廃止し AppKit `NSStatusItem` 直接管理へ書き換え（`VoicekeyApp.swift`）。エントリポイントを SwiftUI App から `NSApplicationDelegate` ベースに変更し、`startup()` をラベルの `.task` から `applicationDidFinishLaunching` へ移動。ドラッグでの取り外しを禁止（`behavior = []`）、設定ウィンドウは `NSHostingController` で自前管理。位置記録を可視領域にリセットして復旧

## [Unreleased] - 2026-06-10

### Changed
- **コア層の全面刷新（大規模バグ調査の結果に基づく Phase 1）**
  - `src/core/audio_recorder.py` 全面書き換え: 永続ストリーム方式へ移行
    - PortAudio の open/close を録音のたびに行わず、ストリームを開いたまま start/stop だけで録音を切り替える（close は 30 秒アイドル時・デバイス変更時・終了時のみ）
    - 専用の AudioControl スレッドが PortAudio 呼び出しをすべて直列実行。公開 API（`start_async` / `stop_async`）は完全ノンブロッキングで、pynput リスナースレッドを一切ブロックしない（macOS の CGEventTap タイムアウトでホットキーが死ぬ問題の根治）
    - `health()` / `recover()` による世代管理ウォッチドッグ復旧を実装。ハングした制御スレッドを見捨てて新世代に切り替え、取得済み音声は救出する
    - 停止時はストリームを閉じずに同期 stop するため、発話末尾の取りこぼしが発生しない
    - HUD 用に約 33ms 間隔の音声レベルコールバックを追加
  - `src/core/vad.py` 全面書き換え: torch + MPS 版 VadFilter を廃止し、onnxruntime (CPU) + numpy のみの `SileroVad` に置換
    - torch のトップレベル import（起動遅延の主因の一つ）を排除。VAD ロード約 88ms・推論 4ms/2 秒音声
    - アプリ全体で 1 インスタンスを共有（旧実装はスロットごとに二重ロード）
    - `speech_bounds()` を新設し、録音前後の無音・ノイズ区間をトリミングして API へ送る量と幻覚を削減
  - `src/core/api_transcriber.py` 新設: OpenAI / Groq トランスクライバを統合
    - openai / groq SDK を廃止し httpx の multipart POST に統一（OpenAI 互換 REST）。SDK の import 時間を排除
    - `prewarm()` で録音開始時に TLS 接続を事前確立し、初回 API 呼び出しの往復を短縮
    - 失敗は "Error:" 文字列ではなく `TranscriptionError` 例外で伝達
    - 旧 `openai_transcriber.py` / `groq_transcriber.py` は削除
  - `src/core/audio_utils.py`: MP3 変換（ffmpeg サブプロセス）を廃止し WAV 専用に簡素化
    - import 時に `ffmpeg -version` を実行していた起動ブロック（最大 5 秒）を排除
    - int16 変換前に `np.clip` を追加し、範囲外サンプルのラップアラウンド（轟音ノイズ化）を防止

### Fixed
- **「ほぼ無音＋ノイズ」録音で全く違う内容が出力される問題（幻覚）への対策**
  - `src/core/audio_preprocess.py:normalize_volume`: ゲイン上限 +20dB を新設。従来は上限がなく、ノイズフロアだけの録音がフルボリュームまで増幅されて API が架空テキストを生成する主要因だった
- **API キーの Keychain 読み出しを毎回行っていた問題**
  - `src/utils/secrets.py`: プロセス内キャッシュを追加（set/delete で無効化）。録音のたびに数十 ms の Keychain アクセスが走らなくなった
  - ElevenLabs 用サービス識別子 `SERVICE_ELEVENLABS` を追加

### Technical Details
- **書き換え**: `src/core/audio_recorder.py`, `src/core/vad.py`, `src/core/audio_utils.py`
- **新規**: `src/core/api_transcriber.py`（`ApiTranscriber` 基底 + `OpenAITranscriber` / `GroqTranscriber`）
- **削除**: `src/core/openai_transcriber.py`, `src/core/groq_transcriber.py`
- **編集**: `src/core/audio_preprocess.py`（`MAX_GAIN_DB`）、`src/utils/secrets.py`（キャッシュ）、`src/core/__init__.py`（エクスポート更新）
- スモークテスト: コア層 import 272ms（torch 削除前は約 1.4 秒）、WAV クリップ・ゲイン上限・VAD 無音/ノイズ判定を確認済み

### Changed (続き: アプリ層)
- **`src/app.py` 全面書き換え（`SuperWhisperApp` → `VoicekeyApp`）**
  - リスナーハンドラを完全ノンブロッキング化（キー集合更新とコマンド投函のみ）。macOS CGEventTap タイムアウトによるホットキー死亡を構造的に防止
  - UI 状態は `_emit_state()` の単一発信点に集約（「アイコンが録音中のまま」の根治）
  - 常駐ワーカー 1 本が 正規化 → VAD ゲート/トリム → API → テキスト挿入 を直列処理
  - 左修飾キーのマッチング修正: macOS の pynput は左修飾キーを汎用名（cmd 等）で報告するため、`<cmd_l>` 設定が一致しなかった問題を `_acceptable_names()` で解消
  - toggle モードも低レベル Listener に統一（GlobalHotKeys 廃止。右修飾キー対応＋エッジ検出でキーリピート誤発火を防止）
  - ウォッチドッグ: PortAudio ハング自動復旧（recover）、録音 300 秒上限、リスナースレッド死活監視
  - 起動時に macOS 権限（アクセシビリティ・入力監視）をチェックし、不足時はダイアログでシステム設定へ誘導
  - ホットリロードでリスナー再起動が不要な設計に変更（ハンドラがイベント時にスロット設定を参照）。退役トランスクライバは 30 秒後に遅延 close
  - dev_mode のタイミングファイル出力・引用符ラップを削除（ログ出力に一本化）
- **`src/core/input_handler.py`**: 貼り付け後にユーザーの元クリップボード内容を復元（テキストのみ）

### Technical Details (続き)
- **書き換え**: `src/app.py`
- **編集**: `src/core/input_handler.py`（クリップボード復元）、`src/platform/base.py` / `src/platform/macos/adapter.py`（`check_input_permissions` / `open_permission_settings` 追加）、`src/main.py` / `src/__init__.py`（クラス名変更追従）

## [Unreleased] - 2026-06-01

### Fixed
- **macOS フリーズ後に「毎回 2 秒待たされる」現象を根絶し、録音停止を完全ノンブロッキング化**
  - `src/core/audio_recorder.py:_cleanup_stream`: PortAudio の `stream.stop()/close()` を daemon スレッドへ投げっぱなしにし、呼び出し元は **一切待たない（join しない）** よう変更。従来は `join(timeout=2.0)` で待っており、一度 close がハングするとその後の録音停止が毎回最大 2 秒ブロックしていた
  - 未使用化した `_CLEANUP_TIMEOUT_SEC` 定数を削除
  - トレードオフ: close がハングしたストリームは OS のマイクを掴んだまま残る（マイクインジケーターが消えない）が、録音・文字入力動作は一切遅延しない。残ったインジケーターはアプリ再起動で解消する
- **ダブルタップ連打時に録音を取りこぼす（samples=0）レースを解消**
  - `src/app.py:stop_and_transcribe`: `_recorder.stop()` を `_finalize_recording_async`（別スレッド）から呼んでいたため、停止完了前に次の録音 start が割り込み、状態がズレて空録音になることがあった。close を待たない設計になったため `stop()` を `_recording_lock` 内で同期実行して音声をその場で確定し、`_active_slot` も即クリアするよう変更
  - `src/app.py:_finalize_recording_async`: シグネチャを変更し確定済み `audio_data` を引数で受け取る（内部での `_recorder.stop()` 呼び出しを削除）。担当は音量正規化とキュー投入のみ
  - `src/app.py:start_recording`: `self._recorder.start()` の戻り値を確認し、開始に成功してから `_is_recording` を立てるよう変更。app 側と recorder 側の状態がズレて「録音中のつもりだが録れていない」ゾンビ状態になるのを防止

### Technical Details
- **編集**: `src/core/audio_recorder.py`（`_cleanup_stream` を fire-and-forget 化、`_CLEANUP_TIMEOUT_SEC` 削除）
- **編集**: `src/app.py`（`start_recording` の start 戻り値チェック、`stop_and_transcribe` の同期 stop 化と `_active_slot` 即クリア、`_finalize_recording_async` の引数化）

## [Unreleased] - 2026-05-28

### Changed
- **README.md を AI エージェント向けセットアップに最適化**
  - 目次に「AI エージェント向けセットアップ手順」を追加
  - 前提条件チェックリスト（OS / Python / git / ffmpeg / ネットワーク）と確認コマンドを表形式で明示
  - macOS / Windows それぞれ「クローン → ffmpeg → venv → 依存関係 → 設定ファイル → 起動」を 1 ブロックで完結するコピペ可能なコマンド列に再整理
  - API キーは設定ウィンドウから入力すると OS シークレットストアに保存される旨を明記し、旧来の `.env` 直書き手順から更新
  - 「ポータル経由で配布物をダウンロードする場合」セクションを追加し、`tag v*` push が GitHub Actions を経由して Releases に直リンクされるリリースフローを記載
  - トラブルシューティングに「macOS で録音中・停止後にアプリがフリーズする」項目を追加（PR #9 で根治済み・Force Reset の使い方）
  - よくあるつまずきポイント（権限再起動・`python3` 必須・`pip install` 遅延・GPU 不要・Windows 管理者権限）を AI が事前案内できる形で明文化

## [Unreleased] - 2026-05-05

### Fixed
- **macOS PortAudio 由来のフリーズ問題に対処**
  - `_recorder.stop()` が `_recording_lock` を握ったまま PortAudio (CoreAudio) の `stream.stop()` / `close()` を呼ぶと、CoreAudio がハングした際にロックを巻き込んでアプリ全体が停止する問題があった
  - `src/core/audio_recorder.py:_cleanup_stream`: `stream.stop()` / `close()` を別スレッドへ逃がし、最大 2 秒のタイムアウトで諦めて呼出元へ復帰。`self._stream` は即 `None` に切替えるため後続の start/stop は新ストリーム前提で進める。タイムアウトしても `_collect_audio_data()` でキューから音声を回収するので発話内容はロストしない（ユーザーは 2 秒余分に待つだけで結果が得られる）
  - `src/app.py:stop_and_transcribe`: `_recording_lock` を解放してから `_recorder.stop()` を呼ぶよう修正。lock を巻き込まないためアプリ全体のフリーズを防止
- **PortAudio ゾンビ callback による録音バッファ汚染を解消**
  - 古い stream は `_cleanup_stream` で `_stream = None` にしても、PortAudio の I/O スレッドが close 完了まで callback を呼び続け、共通の `self._queue` に古い音声を流し込み続けるため 2 回目以降の録音が無音判定 (`has_speech=False`) になっていた
  - `src/core/audio_recorder.py`: 録音セッション識別子 `_session_id` を導入。`start()` のたびにインクリメントし、`_make_audio_callback(session_id)` でセッション ID を埋め込んだクロージャを各 `InputStream` に渡す。callback は `if self._session_id != my_session: return` で旧 stream のゾンビ呼出を即弾く
  - これにより `stream.close()` がハング中でも、新セッションの queue は旧 stream の音声で汚染されない
- **ダブルタップ Auto-Enter 検出が PortAudio ハング時に失われる問題**
  - `stop_and_transcribe` が keyboard listener スレッド内で `_recorder.stop()`（最大 2 秒ブロック）まで実行していたため、キーを離した直後の次の press イベントが listener で待たされ、ダブルタップ判定ウィンドウ (400ms) を超えてしまっていた
  - `src/app.py`: `stop_and_transcribe` はフラグ更新のみ同期で行い、`_finalize_recording_async` を別 daemon スレッドで起動して `_recorder.stop()` 以降を実行。listener スレッドは即時に次のキーイベントを処理可能に

### Added
- **Force Reset (Unfreeze) メニューを再導入**
  - 過去に削除されたが、PortAudio ハング時の最終手段として復活。ただし用途が変わり、内部状態リセットではなく **プロセスごとの再起動** で OS のマイクハンドル / 「マイク使用中」オレンジドット / メニューバーアイコンを完全にリセットする
  - `src/app.py:force_reset_recording`: `subprocess.Popen([sys.executable] + sys.argv, start_new_session=True)` で同じコマンドラインの新プロセスを独立起動し、自分は `os._exit(0)` で即時終了。execv 方式だと macOS で NSStatusItem が再登録されない事象があったため subprocess + 新セッション方式に統一
  - `src/ui/system_tray.py`: メニューに `Force Reset (Unfreeze)` 項目と `force_reset` Signal を追加
- **フリーズ再現用デバッグスクリプト** (`scripts/simulate_freeze.py`)
  - `sounddevice.InputStream.stop` / `close` を「指定秒数だけ眠るだけ」のメソッドに monkeypatch してから voicekey 本体を起動。`FREEZE_SEC` 環境変数で待ち時間を制御（既定 30 秒）
  - 上記フリーズ系修正の動作検証を確実に再現できるようにするため
- **録音状態のデバッグログ拡充**
  - `src/core/audio_recorder.py`: `start()` でストリーム ID、`_audio_callback` で各セッション初回の callback 受信、`stop()` で取得した `queue_items` / `samples` / `duration` / `callback_received` をログ出力。フリーズや録音欠損の切り分けに使用

### Technical Details
- **編集**: `src/app.py`（import に `os` / `subprocess` / `sys` を追加、`stop_and_transcribe` の非同期化、`_finalize_recording_async` 新設、`force_reset_recording` を再起動方式へ書き換え、`_tray.force_reset` の signal 接続）
- **編集**: `src/core/audio_recorder.py`（`_session_id` / `_callback_received` 追加、`_make_audio_callback` クロージャ生成、`_cleanup_stream` のタイムアウト化、`stop()` のログ拡充）
- **編集**: `src/ui/system_tray.py`（`force_reset` Signal、Force Reset メニュー項目）
- **新規**: `scripts/simulate_freeze.py`

## [Unreleased] - 2026-05-01

### Added
- **API キーの OS シークレットストア保管 (macOS Keychain / Windows Credential Manager)**
  - 新規モジュール `src/utils/secrets.py`: `keyring` ライブラリを通じて `get_api_key` / `set_api_key` / `delete_api_key` を提供（サービス識別子 `voicekey.Groq` / `voicekey.OpenAI`）
  - 設定ウィンドウの各 Hotkey の API 設定エリアに「API Key」入力欄（パスワードマスク）と Save / Clear ボタンを追加。同じ backend を選んだ Hotkey 間で同じエントリを共有
  - 取得は **Keychain → 環境変数** の優先順。既存の `.env` / `GROQ_API_KEY` / `OPENAI_API_KEY` 利用は維持され、後方互換を保ったまま Keychain に移行可能（自動マイグレーションは行わない）
  - `settings.yaml` には API キーを書き込まない（ConfigManager 側は変更なし）
- **macOS でのメニューバー常駐動作**
  - `python run.py` 起動時に `NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)` を呼んで Dock / Cmd+Tab から非表示化
  - PyInstaller ビルド版は `.app` バンドル化し、`Info.plist` に `LSUIElement: True` / `NSPrincipalClass: NSApplication` / `NSMicrophoneUsageDescription` を含める（`voicekey.spec`）
  - 設定ウィンドウを開く処理を `raise_()` → `activateWindow()` → `NSApp.activateIgnoringOtherApps_(True)` の順で前面化するよう修正（メニューバー → Settings で確実に最前面に出る）

### Changed
- `requirements.txt` に `keyring>=24.0` と `pyobjc-framework-Cocoa>=10.0; sys_platform == "darwin"` を追加
- `PlatformAdapter` に `configure_app_visibility(hide_from_dock)` と `bring_to_front(window)` を追加（既定 no-op）。macOS アダプタでのみ AppKit 経由で実装し、Windows は no-op
- `GroqTranscriber._get_client` / `OpenAITranscriber._get_client` の API キー取得経路を `_resolve_api_key()` ヘルパー経由に統一（Keychain → 環境変数）。エラーメッセージも「設定ウィンドウから保存するか、環境変数を設定してください」に更新
- **メニューバーアイコンの左クリック挙動を変更**: 以前は左クリック / ダブルクリックで設定ウィンドウが直接開いていたが、コンテキストメニューを表示するだけに変更。ユーザーがメニューから「Settings」を選んだ時にのみ設定ウィンドウを開く（`src/ui/system_tray.py` の `_setup_click_handler` / `_on_activated` を削除、`setContextMenu` のみで動作）

### Removed
- **Force Reset 機能を完全削除**
  - トレイメニューの「Force Reset」項目（`src/ui/system_tray.py` の `force_reset` Signal とアクション）を削除
  - `src/app.py` から `force_reset()` メソッド本体、シグナル接続、`_reset_generation` 世代カウンタ、`_queue_processor` / `_process_transcription_task` 内の世代比較による結果破棄ロジックを全て削除
  - 通常運用で連打フリーズ等の根治対応（自動復旧ループ・録音解除等）が既に入っているため、ユーザー手動のリセットボタンは不要と判断
- **Dynamic Island 風オーバーレイ UI を完全廃止** (`src/ui/overlay.py` 削除)
  - 録音 / 文字起こし状態は **トレイアイコンの色だけ** で判別する設計に統一（IDLE 青 / RECORDING 赤 / RECORDING_AUTO_ENTER 紫 / TRANSCRIBING オレンジ）
  - `src/app.py` から overlay 関連の初期化（`_setup_ui_components` 内）、状態反映 (`_update_ui_status`)、音声レベル反映 (`_on_audio_level`)、波形コールバック登録 (`set_level_callback`) をすべて削除
  - `_show_backend_warning` を「オーバーレイにメッセージ表示」から「ログに warning 出力」に変更（API キー未設定時など）
  - `src/ui/__init__.py` から `DynamicIslandOverlay` の export を削除
  - `src/config/constants.py` から UI セクションの `OVERLAY_BASE_WIDTH` / `OVERLAY_BASE_HEIGHT` / `OVERLAY_EXPANDED_WIDTH` / `OVERLAY_EXPANDED_HEIGHT` / `OVERLAY_TOP_MARGIN` / `ANIMATION_DURATION_MS` を削除
  - 副次効果: 起動時にオーバーレイ用の QMainWindow を作らないため、初期化が軽量化

### Security
- **`settings.yaml` を git 追跡対象から除外**
  - `.gitignore:81` に `settings.yaml` が記載されていたが、過去に誤って追跡されていたため `git rm --cached settings.yaml` で解除（ローカルファイルは残存）
  - 新規 `settings.example.yaml` をコミット対象に追加。新規ユーザー / Clone 時はこのファイルをコピーして使う
  - 将来 `settings.yaml` に万一機密情報を書き込んでも誤コミットされないよう予防
- **依存パッケージの完全 lock**
  - 新規 `requirements.lock`: `pip freeze --exclude-editable` で venv 内の全パッケージとバージョンを固定（再現性 / サプライチェーン耐性向上）
  - 既存の `requirements.txt` は人間が読みやすい下限指定の形を維持。lock ファイルは並列に追加するだけで既存インストール手順への影響なし

### Technical Details
- **新規ファイル**: `src/utils/secrets.py` / `settings.example.yaml` / `requirements.lock`
- **編集**: `src/main.py`（QApplication 作成後に `configure_app_visibility(True)` を呼び出し）
- **編集**: `src/app.py`（`_open_settings` を最前面化シーケンスに変更）
- **編集**: `src/platform/base.py` / `src/platform/macos/adapter.py`（可視性制御メソッドを追加）
- **編集**: `src/core/groq_transcriber.py` / `src/core/openai_transcriber.py`（`_resolve_api_key` 追加）
- **編集**: `src/ui/settings_window.py`（API キー入力欄、`_save_api_key` / `_clear_api_key` / `_refresh_api_key_status` 追加、backend 切替時に Keychain ステータス再描画）
- **編集**: `src/ui/system_tray.py`（左クリック直接起動を廃止、メニュー経由のみ）
- **編集**: `voicekey.spec`（macOS 用 BUNDLE と Info.plist）

## [Unreleased] - 2026-04-30

### Added
- **音声前処理パイプライン（音量正規化）**
  - 新規モジュール `src/core/audio_preprocess.py` を追加
  - Peak+RMS ハイブリッド音量正規化：目標 RMS = -20 dBFS、ピーク上限 = -3 dBFS（音割れ防止）
  - 録音直後・API 送信前に適用、numpy のみで <1ms の低レイテンシ
  - 小さい声を底上げして API 文字起こしの精度を向上、大音量はクリッピング防止のため抑え込み
  - ノイズ対策は API モデル側に任せる方針（noisereduce 等は採用せず）
- **Auto Enter Delay スライダーを設定 UI に追加**
  - ダブルタップ Auto-Enter 機能で、テキスト挿入後から Enter 押下までの待機時間を 0〜500ms で調整可能（`src/ui/settings_window.py`）
  - 既定値 50ms。一部アプリが即時 Enter に反応しない問題に対するユーザー調整手段（`src/config/constants.py`）

### Changed
- `DEFAULT_CONFIG` に `audio_preprocess.volume_normalize` キーを追加（既定 True）
- 設定 UI の Advanced タブに音声前処理セクションを追加
- `stop_and_transcribe()` で `recorder.stop()` 直後に `preprocess_audio()` を呼ぶよう変更（`src/app.py`）

### Fixed
- **録音状態の Race Condition 解消（Phase 3）**
  - `_recording_lock` (RLock) を導入し、`start_recording` / `stop_and_transcribe` / `force_reset` の check-then-set を直列化（`src/app.py`）
  - 並列スレッドから start/stop が同時に呼ばれた場合に `_is_recording` と `_active_slot` の整合性が崩れる問題を解消
  - `start_recording` で transcriber 取得失敗時に `_active_slot` をリセットするよう修正（リーク防止）
  - 6 並列スレッドで 600 回の start/stop を実行しても整合性が保たれることを確認
  - ロック順序: `_recording_lock` → `_queue_worker_lock` → `recorder._lock`（逆順は禁止、デッドロック防止）

- **プラットフォーム整合性の向上（Phase 2）**
  - `InputHandler.insert_text` の貼り付けキー操作を `with pressed(...)` から明示的な `try/finally` に変更。`'v'` の release で例外が発生しても修飾キー（Cmd/Ctrl）が確実に解放されるよう改善（`src/core/input_handler.py`）
  - `OpenAITranscriber` / `GroqTranscriber` に `close()` メソッドを追加し、`unload_model()` から呼び出すよう変更。httpx 接続プールを明示的に閉じてリークを防ぐ（`src/core/openai_transcriber.py`, `src/core/groq_transcriber.py`）
  - `_setup_hotkey_slots()` の冒頭で旧 `api_transcriber.close()` を呼び、Hot reload 時に旧クライアントの HTTP 接続が leak する問題を解消（`src/app.py`）
  - `_apply_config_changes()` で slots 変更検出時に `self._listener.stop()` を呼び、自動再起動ループに新設定でリスナーを再立ち上げさせる（`src/app.py`）

- **連打フリーズ問題の根治（マイク占有/キー押下誤認識/Force Reset 効かず）**
  - `force_reset()` で `_pressed_keys` / `_last_hotkey_release_time` / `_last_hotkey_release_slot` をクリアするよう修正。リセット後も「キーが押されたまま」と誤認識される問題を解消（`src/app.py`）
  - キーボードリスナー (`_start_keyboard_listener`) を自動復旧ループ化。例外で死んでも黙って永久停止せず、押下キー状態をクリアして再起動する（`src/app.py`）
  - `_quit_app()` で `listener.stop()` と `recorder.stop()` を明示的に呼ぶよう修正。終了時にマイクが OS にロックされ続ける問題を解消（`src/app.py`）
  - `AudioRecorder` の `start` / `stop` / `_cleanup_stream` を `threading.RLock` で直列化。`stop` 中に `start` が割り込んで旧ストリームが OS 占有のまま捨てられる競合を解消（`src/core/audio_recorder.py`）
  - `_cleanup_stream` で `stream.stop()` と `stream.close()` を独立 try/except で囲み、片方が例外を出しても他方を必ず実行するよう修正（`src/core/audio_recorder.py`）
  - `_queue_processor` の各タスク処理を try/except/finally で囲み、個別タスクの例外でワーカー全体が死なないようにした。`task_done()` も常に呼ぶ（`src/app.py`）
  - `_queue_worker_running` の check-and-set を `_queue_worker_lock` で排他化し、二重ワーカー起動を防止（`src/app.py`）
  - `_handle_key_press` / `_handle_key_release` で `_normalize_key` 失敗時の挙動を改善。debug ログ出力＋永久録音を防ぐ保険として「押下キー無し＋録音中」検出時に自動停止（`src/app.py`）

### Technical Details
- **src/app.py**
  - `_setup_state` に `_queue_worker_lock` (Lock) と `_listener` 参照保持を追加
  - `_start_queue_worker` を `_start_queue_worker_locked` にリネーム（呼び出し側がロック取得済み前提）
  - キーボードリスナー再起動ループにより Hot reload 時のリスナー入れ替えも将来対応可能
- **src/core/audio_recorder.py**
  - `__init__` に `threading.RLock` を追加し、ライフサイクル全パスを保護
  - `start()` 冒頭で残骸ストリームのクリーンアップを実施

---

## [Unreleased] - 2026-04-18

### Added
- **Auto Enter 遅延調整スライダー**
  - ダブルタップ時のテキスト挿入後〜Enter押下までの待機時間をUIから調整可能に
  - Settings の Advanced ページにスライダー（0〜500ms、既定50ms）と現在値ラベルを追加
  - 即時Enterに反応しないアプリ（Slack、一部Webフォーム等）向けに遅延を伸ばせる
  - 新規設定キー `auto_enter_delay_ms` を追加（settings.yaml・ホットリロード対応）

### Technical Details
- **constants.py**: `DEFAULT_CONFIG` に `auto_enter_delay_ms: 50` を追加
- **settings_window.py**: `QSlider` + `QLabel` を Advanced ページに追加、load/save に反映
- **app.py**: `_handle_transcription_result()` のハードコード `time.sleep(0.05)` を `self._config.get("auto_enter_delay_ms", 50)` 参照に置換

---

## [Unreleased] - 2026-04-08

### Added
- **強制リセット機能**
  - トレイアイコンの右クリックメニューに「Force Reset」を追加
  - 録音中・文字起こし中の全処理を強制停止してidle状態に復帰
  - 世代カウンタにより実行中のAPI呼び出し結果も安全に破棄

- **ダブルタップ + ホールドで自動Enterキー送信**
  - ホールドモードでホットキーをダブルタップ（2回目を長押し）すると、文字起こし結果入力後にEnterキーを自動送信
  - チャットアプリでの音声入力→送信をワンアクションで完結
  - ダブルタップ判定ウィンドウ: 400ms

### Technical Details
- **types.py**: `TranscriptionTask` に `auto_enter` フィールドを追加
- **input_handler.py**: `press_enter()` メソッドを追加（pynput Key.enter使用）
- **system_tray.py**: `force_reset` シグナルとメニュー項目を追加
- **app.py**: `force_reset()` メソッド、世代カウンタ `_reset_generation`、ダブルタップ検出ロジック、`text_ready` シグナルを `Signal(str, bool)` に拡張

---

## [Unreleased] - 2026-02-27

### Added
- **Cross-platform 抽象レイヤーを追加**
  - `src/platform/` を新設し、OS差分を `core` から分離
  - `PlatformAdapter` インターフェースと `get_platform_adapter()` ファクトリを追加
  - `windows` / `macos` 向けアダプタ実装を追加

- **入力デバイス選択機能を追加**
  - Settings の Advanced ページでマイク入力デバイスを選択可能
  - `audio_input_device` 設定キーを追加（`default` / デバイスID）
  - 録音開始時に指定デバイスを使用し、失敗時は自動でデフォルトへフォールバック

- **運用ドキュメントの追加**
  - `docs/CROSS_PLATFORM_UNIFICATION_PLAN.md`（統合計画）
  - `docs/CROSS_PLATFORM_TEST_CHECKLIST.md`（検証チェックリスト）
  - `run.sh`（macOS/Linux向け起動スクリプト）

### Changed
- **入力処理を platform 注入方式へ移行**
  - `src/core/input_handler.py` の `sys.platform` 分岐を削除
  - 貼り付け修飾キー（Cmd/Ctrl）を platform アダプタで制御

- **録音設定の動的反映を強化**
  - `settings.yaml` の変更監視で入力デバイス設定の更新を即時適用

- **UI のOS依存ロジックを分離**
  - `src/ui/settings_window.py` のホットキー変換を platform 経由に変更
  - `src/ui/system_tray.py` のアクティベーション判定を platform ポリシー化
  - `src/ui/styles.py` のフォント指定を OS別フォールバック対応に変更

- **アプリ初期化の依存注入を整理**
  - `src/app.py` で platform アダプタを初期化し、
    InputHandler / SettingsWindow / SystemTray / キー正規化に注入

### Technical Details
- **新規追加**
  - `src/platform/base.py`
  - `src/platform/factory.py`
  - `src/platform/common/keymap.py`
  - `src/platform/windows/adapter.py`
  - `src/platform/macos/adapter.py`

- **更新**
  - `src/app.py`
  - `src/core/input_handler.py`
  - `src/ui/settings_window.py`
  - `src/ui/system_tray.py`
  - `src/ui/styles.py`
  - `README.md`

## [Unreleased] - 2026-02-03

### Added
- **起動時プリロード機能の実装**
  - 起動時にVADモデルをバックグラウンドでプリロードし、最初の文字起こしを高速化
  - `preload_on_startup` 設定オプションを追加（デフォルト: true）
  - `app.py` に `_preload_models_async()` を追加

### Fixed
- **VADプリロードのタイミング改善**
  - ホットキースロット初期化後にプリロードを実行するよう調整
  - 起動順序を `setup_state -> start_background_threads -> preload` に整理

### Technical Details
- **src/app.py**
  - `_preload_models_async()` を追加し、設定に応じて非同期プリロードを実行
  - `_preload_vad_model()` を実行ロジック専用に整理
- **src/config/constants.py**
  - `DEFAULT_CONFIG` に `preload_on_startup: true` を追加

## [Unreleased] - 2026-01-30

### Added
- **文字起こしキューイング機能の実装**
  - 文字起こし処理中に新しい録音を開始しても、前タスクを破棄せずキューに追加
  - すべての録音結果を順番に処理して入力
  - `queue.Queue` を使用したスレッドセーフなタスク管理
  - `TranscriptionTask` データクラスを追加

### Changed
- **app.py の文字起こし処理ロジックをキュー方式へ変更**
  - `start_recording()` からキャンセル方式を削除
  - `stop_and_transcribe()` でキュー投入
  - `_start_queue_worker()`, `_queue_processor()`, `_process_transcription_task()` を追加
  - `_handle_transcription_result()` は結果処理専用にし、idle遷移はワーカー管理へ移行

### Technical Details
- **src/config/types.py**
  - `TranscriptionTask` データクラスを追加（audio_data, slot_id, timestamp）
- **src/app.py**
  - `_transcription_queue` / `_queue_worker_running` を追加
  - キュー処理完了時に `idle` へ復帰する制御を追加

## [Unreleased] - 2026-01-15

### Added
- **CONTRIBUTING.md ドキュメント作成**
  - 詳細なバージョニングルール（X=大きな変更、Y=ユーザーが気づく変更、Z=小さな修正）
  - コミットメッセージ規約（type: description形式）
  - 変更記録（CHANGELOG）の運用ルール
  - ブランチ戦略とリリースプロセス
  - プルリクエストのガイドライン
  - プッシュのタイミングとチェックリスト

- **デュアルホットキー機能の実装**
- **2つの独立したホットキー設定**: 固定で2つのホットキースロットを追加
  - 各ホットキーに対して異なるショートカット、モード（hold/toggle）、バックエンド（local/groq/openai）を設定可能
  - APIバックエンド（Groq/OpenAI）の場合、各ホットキーで異なるモデルとプロンプトを指定可能
  - ローカルバックエンドは両方のホットキーで共通の設定を使用（VRAM節約）

- **新しい設定構造**: `settings.yaml` の階層化
  - `hotkey1` / `hotkey2`: 各ホットキーの個別設定
  - `local_backend`: ローカルGPU設定（共通）
  - `language`, `vad_filter` などのグローバル設定

- **自動マイグレーション機能**
  - 旧設定フォーマット（単一ホットキー）を検出し、新形式に自動変換
  - 既存ユーザーの設定を保持しながらアップグレード可能
  - マイグレーション時のログ出力

- **設定UIの刷新**
  - Generalページ: 2つのホットキーを横並びで設定
  - 各ホットキーグループに: ショートカット入力、モード選択、バックエンド選択、API設定
  - Modelページ: ローカル共通設定のみに簡略化
  - API設定の動的表示（バックエンド選択に応じて表示/非表示）

### Changed
- **CLAUDE.md に自動コミットルール追加**
  - AI開発者向けに、機能実装完了時の自動コミットルールを明記
  - コミットのタイミング、必須チェック項目、例外ケースを定義
  - プッシュは手動実行（自動プッシュしない）
  - ユーザーへの報告フォーマットを標準化

- **README.md コントリビューションセクション更新**
  - CONTRIBUTING.md へのリンク追加
  - 開発ガイドラインへのナビゲーション改善

- **app.py の大幅なリファクタリング**
  - `HotkeySlot` データクラスを追加（各スロットの状態管理）
  - `_hotkey_slots` 辞書で複数ホットキーを管理
  - `_local_transcriber` を共有インスタンスとして分離
  - `_active_slot` で現在アクティブなスロットを追跡
  - キーボードリスナーが両方のホットキーを同時監視
  - `start_recording()` にスロットID引数を追加

- **config_manager.py の強化**
  - `_deep_merge()` 関数でネストされた辞書のマージをサポート
  - `_migrate_legacy_config()` メソッドで旧設定を自動変換
  - 深いマージによりデフォルト設定との統合を改善

- **ホットリロード機能の維持**
  - `_apply_config_changes()` が新構造に対応
  - ホットキー設定変更時の自動更新
  - バックエンド変更時のAPI Transcriber再作成
  - ローカル設定変更時のモデルアンロード

### Technical Details
- **types.py**
  - `HotkeySlotConfig` データクラスを追加

- **constants.py**
  - `DEFAULT_CONFIG` を新構造（hotkey1/hotkey2/local_backend）に変更
  - `default_api_models` でバックエンド別のデフォルトモデルを定義

- **settings_window.py**
  - `_create_hotkey_group()` で各スロットのUIを生成
  - `_create_api_settings_widget()` でAPI設定ウィジェットを動的生成
  - `_on_slot_backend_changed()` でバックエンド変更を処理
  - `_load_current_settings()` / `_save_settings()` を新構造に対応

### Fixed
- ホットキー競合時の優先順位（最初に検出されたスロットが優先）
- Hold/Toggle混在時のキーボードリスナー処理

---

## [v2.0.0] - 2026-01-15

### Added
- デュアルホットキースロットとMP3音声サポート
- OpenAIバックエンドとGroqバックエンドのモジュール化
- macOSスタイルの設定UI

### Changed
- プロジェクト構造のリファクタリング
- バックエンドの分離（local/groq/openai）

---

## [Previous Releases]

### [2026-01-05] - LLMプロンプト処理の改善
- LLMプロンプト処理のリファクタリング
- 設定UIの改善

### [2025-12-09] - UI改善とドキュメント更新
- オーバーレイUIの改善
- ホットキー処理の改善
- AI用コメントルールの追加
- README更新

### [2025-12-09] - v2.0.0リリース
- 日本語コメントの追加
- LLM処理ログ表示
- コード整理

### [2025-12-08] - LLM後処理機能
- LLM後処理機能の追加
- GUI設定の追加
- macOSスタイルのUIテーマ
- オーバーレイの改善

### [2025-12-08] - Groqバックエンド統合
- Groq API対応
- VADフィルター統合
- PyInstallerビルド設定更新

### [2025-12-08] - プロジェクト整形
- 全体的なコード整形
- 安定版リリース

### [2025-12-01] - 初期リリース
- プロジェクト名変更（SuperWhisperLike → voicekey）
- GNU GPL v3ライセンス追加
- 無音検出のUIフィードバック
- エラーハンドリング改善
- ビルドアーティファクトのクリーンアップ

---

## Notes

### 変更記録のガイドライン
- すべての機能追加、変更、修正を記録する
- 各エントリには簡潔な説明と影響範囲を含める
- 技術的な詳細は "Technical Details" セクションに記載
- ユーザー影響のある変更は目立つように記載

### バージョニング
- メジャーバージョン: 破壊的変更または大規模な機能追加
- マイナーバージョン: 後方互換性のある機能追加
- パッチバージョン: バグ修正とマイナーな改善
