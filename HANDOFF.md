# HANDOFF — voicekey 無料テスト版（ベータ）配布

セッションをまたぐ作業の現在地。再開時はまずこれを読む。
承認済み計画の全文: `/Users/tomato/.claude/plans/api-api-api-mac-abstract-hare.md`

## 最新の現在地（2026-08-10 更新）

- **ライブ字幕を voicekey に統合（統合フェーズ 1・personal ブランチ・Mac・macOS 26 以降）**。
  旧 subglass のモジュール一式を `macos/Sources/Voicekey/Caption/`（27 ファイル）へ移植し、
  メニューバーの「ライブ字幕」サブメニュー・グローバル ⌥⌘S・ConfigStore・Info.plist へ結線した。
  ディクテーションのクリティカルパスには触れていない（`AppController.caption` は遅延生成）。
  恒久要件・キー探索・App Nap 対策・ハーネスは `CLAUDE.md` の「ライブ字幕」節が正本。
  - 検証: `swift build` / `swift test` **116 件 全 PASS**（Dock 常時表示の既定を personal で ON に
    変更したため `ConfigStoreDefaultsTests` の期待値を更新）。ビルド → 再起動 → ⌥⌘S 登録をログで確認。
  - **未完（ユーザー操作待ち）**: 「システムオーディオを収録するためのアクセス権」の許可ダイアログが
    voicekey に対して表示され、**未応答のため字幕ハーネス（caption_e2e.sh）はタイムアウト**している
    （tccd ログで `AUTHREQ_PROMPTING service=kTCCServiceAudioCapture subject=com.voicekey.app` を確認。
    CoreAudio の `AudioDeviceStart` が応答待ちでブロックする）。**ユーザーが 1 回「許可」を押せば解決**し、
    以後 e2e / tts-loop / scope の 3 ハーネスを回せる。
  - 並行稼働の注意: 旧 subglass も ⌥⌘S を登録しているため、両方起動中はどちらがホットキーを
    受け取るか不定。字幕の操作はメニューからも行える。subglass の廃止は統合フェーズ 3。

- **HUD 再発 2 件を計測で根治（release `05b29f8` / main `6fe045b`・push 済み・常用アプリ反映済み）**:
  ①「変換中…」の横揺れ＝ジオメトリは不動（0px 実測）で、**正体は明滅の谷で「…」が先に視認閾値を割り
  可視インクの重心が ±7pt 振れる知覚現象**。変換中テキストを完全静止化（明滅撤去・両 OS）。after は
  50 フレーム range 0.00px。**今後この場所に活動表現を入れるなら左右非対称要素（…）の明滅は再発するので禁止**。
  ②フルスクリーン中に待機ピルが消えない＝f9a3339 の CGWindowList 検出＋asyncAfter 保険は App Nap で
  保険が沈黙する構造欠陥。検出を全廃し **collectionBehavior で OS の Space 管理へ委譲**
  （待機=現在 Space のみ・録音/変換/通知= .canJoinAllSpaces+.fullScreenAuxiliary でフルスクリーン上も表示。
  ※実測: .fullScreenAuxiliary を外すだけでは .canJoinAllSpaces が全 Space へ強制表示するため不足）。
  実フルスクリーン Space の画素判定で 待機=消滅（non-white 0）/録音=表示 を確認。
  恒久回帰ハーネス `macos/scripts/dev/fullscreen_helper.swift`（TCC 不要）をコミット。
  **教訓（lessons.md 記録済み）**: 常駐 UI の視覚挙動は「インストール済み最終ビルド＋実条件ハーネスの
  数値/画素判定」まで行って完了と言う。Windows にはフルスクリーン非表示の実装が元々ない（未対応・後日）。
- **UI/UX 大型バッチ実装済み（release `546ec43`〜`0e1abb4`・9 コミット・push 済み・未リリース）**。内容:
  ①ピル刷新第 6 弾（三段階サイズ 待機<変換中<録音中・全体小型化・Dock/画面構成変化への追従・
  ハンズフリーはラベル/「もう一度押すと停止」撤去→ティール #00C7B8 の色のみ・両 OS）
  ②「変換中」横揺れ回帰の修正（`transcribingSizeLocked` で小さく×固定・回帰テスト付き。回帰原因は
  546ec43 が凍結ガードを外したこと＝「main の移植漏れ」仮説は監査で否定）
  ③フルスクリーン中は待機ピルを非表示（**常駐 App Nap 下では遅延 Task{@MainActor}/Timer が実行されない
  実測知見**＝同期コールバック化＋CGWindowList のレイヤー 0 全画面被覆判定。memory
  `voicekey-appnap-deferred-tasks` に恒久記録）
  ④UI の紫ウォッシュ/ネオン発光を全排除（無彩色ガラス基調・両 OS）
  ⑤アプリアイコンをガラス質感に刷新（現行デザイン維持・両 OS・main に残っていた旧アイコンは不採用＝破棄対象）
  ⑥**セットアップガイド全面再デザイン**（Mac=メインウィンドウ内 2 ペイン 12 ステップ・Win=8 ステップ・
  参考画像 36 枚〔Typeless/Aqua Voice〕準拠）＋**初回権限ポップアップの直列化**（startup() をサブシステム分解し
  各ステップの「許可する」でのみ発火）＋ガイドは起動時のみ表示＋**ホームに「マイクテスト」「セットアップガイド」
  カード常設**（Win は設定に配置・MicLevelMonitor 新設＝無料枠不消費）。
  - 検証: Mac swift test 69 / Win unittest 406 全緑・全ステップのスクショ目視確認済み。
- **voicekey-site 3 点を本番反映済み**（`ee2f586`・`d2d698f`）: ①LP のガラス感をピル準拠に強化
  ②サーバー遅延削減（proxy middleware の /api/ 除外ほか＝warm TTFB 実測 161ms→68-76ms）
  ③**STT 双方向フォールバック（Groq⇄ElevenLabs・タイムアウト付き・`stt_timing` JSON ログ）**。
  ハンズフリーの中身は EL のまま不変（ユーザー指示「遅くてよい」）。先日の EL 失敗の正体は
  ユーザーの EL クレジット切れ（本人談）＝フォールバックで「何も入力されない」は根絶。
  Groq 純処理は p50 268ms で高速（「遅い」体感の主因はサーバー税→解消）。
  **【訂正 2026-07-14】push では本番デプロイは走らない**（GitHub 連携の自動デプロイは未接続と実測確認。
  本番反映は手動 `vercel deploy --prod` のみ。Claude 側に許可ルール追加済みで自走可能）。
- **main への移植も本日完了**（下の「現在地 / 次の一手」の対応表を参照）。main⇄release 監査で
  main→release の移植必要分はゼロと確定済み。
- **音声エージェント系（会話モード v4/v5・秘書モード・Realtime）: 2026-07-14 凍結**（商品化フォーカスのため）。
  v5（声=gpt-realtime-2 司令塔＋本物 claude TUI・複数セッション・秘書モード・swift test 557 緑）まで実装完了済み。
  凍結解除トリガー= **OpenAI GPT-Live**（2026-07-08 発表・全二重）の **API 公開**。現在地・繰越・再開手順は
  **voice-agent ブランチの `docs/VOICE_AGENT_FREEZE.md`（fa8cebe）が正本**。release/main には未マージ。
  ※凍結対象外だった製品側移植（ホットキー「未割り当て」UI）は 2026-07-15 に完了（release `2e2b382` / main `4004ef1`・両 OS）。
- **Stripe テストモード E2E 全通過（2026-07-14）**: ¥980/月確定・Test の商品/Price/Webhook 作成済み
  （price_1Tt01EQiaA5hs7siVnKRT7Ih / we_1Tt09n…）・特商法/プライバシーページ公開・LP 実額化済み。
  4242 購入 E2E で checkout→webhook→entitlements 付与（+1ヶ月）→ポータル期末解約→解約 webhook 反映まで
  本番環境で実測合格（テストユーザー・test customer は削除済み）。
  途中の躓きと教訓: 本番 `STRIPE_SECRET_KEY` が別サンドボックスの旧鍵で「No such price」
  （逆引きで確定→本体テストモードの sk_test に差し替えで解消）。`vercel env ls` の created 列は
  更新を反映しないので env 反映の最終判定は E2E で行う。env 変更はデプロイし直すまで効かない。
- **Stripe 本番(live)切替 完了＝販売開始状態（2026-07-15）**: 本人確認承認済み（charges_enabled=true）。
  Live の商品/価格 `price_1TtJYWQiaA5hs7siOWaZfOy7`（¥980/月）/Webhook `we_1TtJYWQiaA5hs7sidFTqjX13`
  （enabled・4イベント・本番 URL）を作成し env 3 種を live へ差し替え済み・デプロイ済み。
  checkout が `cs_live_` を返し決済ページが ¥980・テスト表示なしで開くことを実測確認（実課金は未実行＝配線確認のみ）。
  live 商品作成は CLI の rk_live では権限不足で、ユーザーが sk_live で作成スクリプトを実行。
  **教訓**: sk_live はチャットに貼らせない（一度露出→ユーザーに Roll key させて新キーで作業＝今回そうした）。
  残: ユーザーが実カードで初回課金の最終確認（任意）。CLI ログイン鍵 rk_live は read のみ・書込は sk_live 必須。
- **サイトのセキュリティ強化を本番反映（2026-07-14・19c1016）**: /admin と管理 API に Basic 認証
  （fail-closed・資格情報は Vercel env ADMIN_BASIC_USER/PASS 投入済み）、管理者のみ TOTP 二段階認証
  （/admin/mfa・AAL2）、全ルートにセキュリティヘッダー 5 種。本番で 401/正資格通過/無回帰を実測確認済み。
  残り（ユーザー作業）: /admin 初回アクセスで TOTP 登録、カード会社のセキュリティアンケート提出
  （回答シート提供済み・Q1 は「その他（Stripe Checkout）」）。npm audit 残 2 件（moderate・postcss）は
  next 16.3 安定版更新で解消見込み（別作業）。
- **即時入力の遅延再発を根治（2026-07-15・release `46fc63e`・常用アプリ反映済み）**: 原因はコード退行でなく
  ①先読みが TTL 満了前に更新しない構造的コールド窓（毎時 0〜240 秒。token_grants の毎時 1 回発行パターンで実証）
  ②コールド中の短発話が CloseStream no-op→固定 3 秒待ち。修正=warm 経路は残 360s 未満で事前更新
  （録音開始は従来 15s・Windows にも同欠陥ありで閾値を移植）＋finish-before-connect の即解決＋.notice 診断ログ
  （warm tick / warm・cold ヒット / finish 解決要因）。実機ログで warm ヒット接続 22ms・録音停止→確定 234〜338ms を確認。
  main は認証層が無いため対象外（構造からして同欠陥なし）。
- **Windows パリティ次フェーズ 完了（2026-07-15）**: ホームダッシュボード・サイドノッチ・操作音・
  メディアダッキング・実ブラー背景（アクリル＋フォールバック）・フルスクリーン判定（待機ピル退避）の
  6 要素を Windows へ同等化（release `ca6adca`〜`74a5622`・main `38a3932`・テスト 493/330 全緑）。
  win32/COM/DWM の実挙動は Mac 上で検証不可＝実機確認項目を `docs/BUILD_WINDOWS.md` チェックリストに追記済み。
- **数字の半角出力（2026-07-15・ユーザー指示）**: 数字の読み上げを漢数字でなく半角で入力する 3 層実装
  （Whisper prompt バイアス／貼付前の安全な後処理＝連続漢数字のみ変換・「一人」等は保護／整形プロンプト）。
  release `ff59b55` / main `c9b7e6d` / site `d23e983`（プロキシの prompt 転送も修正・本番デプロイ済み）。
  Deepgram の numerals は ja 非対応と確認し不採用。
- サイトの追加修正（2026-07-15）: /admin/mfa の TOTP コード入力が IME 全角数字・漢数字で入力不能になる
  問題を修正（site `53b06e6`・本番反映済み。送信時に全角/漢数字→半角へ正規化）。
- next 16.3 更新は上流ブロック中: 16.3.0 安定版が未公開（16.2.10 も脆弱 postcss のまま・修正は canary のみ）。
  公開され次第 更新→フル回帰→デプロイ。前倒し手段は package.json overrides（非公式・ユーザー判断待ち）。
- macos/README.md:13 の陳腐化は main 側で修正済み（`4004ef1` に同梱）。
- 既往（2026-07-05）: LP を「Porcelain Glass」へ全面刷新（e0d92a6）・用語「リアルタイム」→「即時入力」
  （release 139f748）・体験型ガイド初版（release 99dedb4 / main af9ba67・本日 2026-07-06 の再デザインで置換済み）。

## ゴール

テスター向け無料ベータを配布できる状態にする: Mac は DMG・Windows は exe インストーラ、
API キーは開発者の現行キーをビルド時埋め込み（テスターは入力不要）、自動アップデート両 OS 必須、
ソース非公開。あわせてマイク自動検出（両 OS）と Windows UI 再デザインを行う。

## 恒久要件（ユーザー確定事項・変更しない）

- API キーを git にコミットするのは絶対禁止。ビルド時にローカル（Mac: Keychain / Win: .env.dist）から注入し、
  XOR 難読化した生成ソース（gitignore 対象）としてバイナリに焼き込む。
- 自動アップデートは Mac 版・Windows 版の両方に必ず組み込む（ユーザー強い要望）。
- ソースコード非公開。配布物に .py 平文を含めない（voicekey.spec の datas=('src','src') は根治対象）。
- DIST ビルドでは設定画面の API キータブを非表示。
- ブランチは **2 本運用**（2026-06-17 確定・**main 一本化を上書き**）: **`main`=自分用 / `release`=製品版**。
  この 2 ブランチは絶対に混ぜない。詳細仕様は `CLAUDE.md` 冒頭「2 ブランチ運用」と memory
  `project_voicekey_branch_split` を参照。**配布タグ（vX.Y.Z）は `release` ブランチで打ち**、配布物は
  release の dist ビルドから作る（自分用 main の実プロバイダー名 UI を配布しないため）。どのブランチに変更を
  入れるかは **Claude が内容から判断**（2026-07-03 改訂・毎回確認を廃止。原則 release 実装→main 移植。
  混ぜない原則・指示なき merge/cherry-pick 禁止は不変）。beta ブランチ廃止は維持。ソース非公開はブランチではなく
  「private リポジトリ ＋ 配布物に .py 平文を含めない」で担保（後者は voicekey.spec の datas=('src','src')
  が根治対象・未完）。
- 配布物置き場（2026-06-14 確定の実態）:
  - **Mac**: すべて Vercel サイト（https://voicekey.vercel.app、ソースは
    `/Users/tomato/Project/voicekey-site/`、`vercel deploy --prod` で更新）。
    DMG は `/downloads/`、appcast+更新 zip+delta は `/mac/`。
  - **Windows**: setup.exe（約 270MB）は **公開リポ `voicekey-releases` の GitHub Releases** でホスト
    （Vercel のファイルサイズ上限を超えるため）。`voicekey-releases` は **public だがソース非公開**
    （README + バイナリのみ。アプリ本体リポ `Tomato-1101/voicekey` は private）。
  - サイトの `/windows/version.json`（自動更新フィード・GitHub Releases の URL を指す）と
    `/downloads.json`（ダウンロードページの最新版表示）は Vercel に置く。
  - 注: 2026-06-12 時点では「全部 Vercel・GitHub 不使用」案だったが、Windows exe が大きすぎて
    Vercel に置けず、06-14 に Windows のみ GitHub Releases ホストへ確定した。
- **Apple Developer Program 加入は「実際に売る段階で」に延期（2026-06-12 ユーザー最終決定）**。
  経緯: 同日に不加入確定 → 加入方針 → 「テスト段階だから今はやらない。販売時にやる」で確定。
  ベータ期間中の配布は Apple Development 署名 + Gatekeeper 回避手順書（サイト・DMG 同梱）で運用。
  販売開始時に Phase 7（Developer ID + 公証、手順は Phase 7 の項）を実施する。
- Mac 版コード変更後は ビルド→旧プロセス kill→open→動作確認 までワンセット（CLAUDE.md ルール）。
- UI スモーク・テストでは secrets/keyring を必ずモック（実 Keychain ダイアログ事故防止）。

## フェーズと検証チェックポイント

- [x] Phase 0: beta ブランチ + .gitignore（検証済み: git check-ignore で生成物3つが無視される）
- [x] Phase 1: Mac キー埋め込み（検証済み: スタブビルド起動OK / ダミーキー DIST で strings に平文なし /
      XOR 復号ラウンドトリップ OK。残: DIST の API キータブ非表示の目視と Keychain 空環境での
      文字起こし E2E は Phase 3 の実キービルドで行う）
- [x] Phase 2: Sparkle + build_dmg.sh（検証済み: codesign --verify --deep --strict 通過 /
      ローカル http.server の appcast で旧→新の自動更新 E2E 成功（差分 DL→終了時インストール）。
      Sparkle EdDSA 鍵: 公開鍵は Info.plist、秘密鍵は ~/.voicekey/sparkle_eddsa_key とログイン Keychain）
- [x] Phase 3: 初回 Mac リリース（**2026-06-12 完了**: v1.0.0 を公開。実キービルド →
      strings 平文 0 件確認 → DMG を Releases v1.0.0 に添付 / zip+appcast を mac/ にコミット /
      配布ページ https://voicekey.vercel.app の Mac ボタンが v1.0.0 直リンクになったことを確認済み。
      appcast・zip の raw URL も 200 確認済み＝既存インストールへの自動更新経路が有効。
      ソース本体リポジトリは private 化済み（2026-06-12、ソース非公開要望のため））
- [x] Phase 4: Windows 配布一式（検証済み: py_compile 全通過 / unittest 99 件全通過（新規 22 件含む）/
      オフスクリーン UI スモークで dev=5タブ・DIST=4タブ・トレイ更新通知を確認（secrets モック）。
      DIST 時の設定画面クラッシュバグをスモークで発見し修正済み。
      残: Windows 実機での E2E は docs/BUILD_WINDOWS.md のチェックリストで実施（実機が無いため未実施））
- [x] Phase 5: マイク自動検出 両 OS（スコア = RMS p90 − p10。検証済み: スコアロジック単体テスト
      Swift/Python 両方 / 偽 sounddevice での同時監視統合テスト / オフスクリーン UI スモーク /
      Mac ビルド+再起動。残: Mac 実マイクでの最終確認は要ユーザー操作 — 設定 → 一般 →
      「自動検出」→ 一言喋る。スコアは `log show --predicate 'subsystem == "com.voicekey.app"'
      --last 5m | grep 自動検出` で確認できる。CLI からの実マイクテストは Terminal への
      TCC マイク許可ダイアログを誘発するため実施しない）
- [x] Phase 6: Windows UI 再デザイン（検証済み: preview_ui.py で全タブ × ダーク/ライト + HUD 4 状態 +
      トレイ 4 状態のスクショを生成・目視 / py_compile / unittest 111 件全通過。
      スクショで一般タブの横スクロールバー常駐バグを発見し修正。スクショはユーザーに送付済み）
- [ ] Phase 7: Developer ID + 公証切替 — **再開待ち**（2026-06-12 にいったん中止 → 同日ユーザーが
      加入へ方針転換。**ユーザーの Program 加入承認が完了したら着手**）。手順:
      ① Developer ID Application 証明書を作成（Xcode → Settings → Accounts → Manage Certificates）
      ② App 用パスワードを appleid.apple.com で発行 → `xcrun notarytool store-credentials voicekey-notary`
      ③ `build_dmg.sh --version X.Y.Z --identity "Developer ID Application: ..." --notarize` でリリース
      ④ サイト・dmg_readme.txt から Gatekeeper 警告の回避手順を削除（警告自体が消える）
      ⑤ 検証: `spctl -a -t open --context context:primary-signature` で DMG が accept、
        クリーン環境で警告なしダブルクリック起動、既存テスターへの Sparkle 自動更新
        （新旧同 Team ID 9KT598FS4A なので通る見込み）
      補足: voicekey は Mac App Store 不可（サンドボックスがグローバルホットキー・テキスト注入を
      許さない）。公証付き直接配布が正しい経路。App Store は将来の iPhone アプリ用

## ユーザー待ちの項目（Phase 3 の完了に必要）

1. ~~public リポジトリ voicekey-releases の作成承認~~ — **完了**（2026-06-12 ユーザー承認 →
   https://github.com/Tomato-1101/voicekey-releases を public 作成・push 済み。
   同日に Vercel のダウンロードページ https://voicekey.vercel.app も公開済み）。
2. ~~実キービルドの初回 Keychain 許可~~ — **完了**（2026-06-12 ビルド成功・v1.0.0 公開済み）。
3. **各 API ダッシュボードで利用上限・アラート設定**（現行キーをそのまま埋め込むため必須の保険。
   **唯一の残り項目**）。

## 現在地 / 次の一手

### 次の一手

1. ~~voicekey-site の本番デプロイ~~ → **完了（2026-07-04）**: ユーザー指示の `! vercel deploy --prod` で
   本番反映済み（deployment READY・整形プリセット 4 種がサーバーで有効）。
2. **Windows への移植は引き続き後日（ユーザー指示・残り縮小）**。2026-07-06 のバッチで HUD 刷新
   （三段階サイズ・待機ピル・横揺れ修正）・無彩色ガラス化・セットアップガイド再デザイン・マイクテストは
   Windows も同等化済み。**残り**: 大型機能バッチ（ホームダッシュボード・SideNotch・操作音・
   メディアダッキング・実ブラー背景）＋整形プリセット UI・長文分割調整・既定ホットキー・
   フルスクリーン時ピル非表示の Windows 判定。次版リリースは両 OS 同期が原則なので、
   リリース前に Windows 分の扱い（移植 or Mac 先行の可否）を判断する。

**マシン状態のメモ**: 2026-07-03 夜にユーザーが新規ユーザーとして再インストールテストを完走
（UserDefaults / 履歴・統計 / キャッシュ / TCC / Keychain の Auth・DeviceId は全消去→再作成済み。個人 API キー 4 件は温存）。
**2026-07-06 に /Applications/voicekey.app を最新 release DIST（`0e1abb4` 反映・strings localhost=0・
Apple Development 9KT598FS4A 署名）へ差し替え、以後は /Applications から常用稼働**（dist/ 直接起動をやめた。
dist/voicekey.app はビルドごとに上書きされるため常用しない）。

- 現在地: **2026-07-06 の UI/UX 大型バッチ＋サーバー改善＋main 移植まで完了・両ブランチ実装済み・未リリース**。
  - release=`546ec43`(ピル刷新第6弾)/`f0953ff`(.gitignore)/`7c42e34`(紫・ネオン排除)/`a621895`(ガイド起動時のみ)/
    `030d94d`(横揺れ回帰修正)/`f9a3339`(フルスクリーン非表示修正)/`1da5785`(ガラスアイコン)/`009e306`(Mac ガイド全面再設計＋
    権限直列化)/`0e1abb4`(Win ガイド再設計＋MicLevelMonitor)。内容の詳細は上の「最新の現在地」。
  - **main へ 1:1 適応移植済み・push 済み（origin/main=`20b609e`）**: `17278bf`/`9ee6668`/`f26d531`/`2bbae61`/
    `c9d185c`/`f83b95d`/`57938ab`/`d7f51a0`/`20b609e`。主な適応: ガイドの「ログイン」ステップ→「API キー案内」へ置換
    （LoginCoordinator/BackendClient 依存を除去・startBackendWarmupIfLoggedIn は main に持ち込まず）・Windows ガイドは
    実プロバイダー名表示・main に残っていた却下済み旧アイコンをガラス版でバイト同一置換・OVERVIEW.md は両ブランチ
    完全一致（`git diff main release -- OVERVIEW.md` 空）を確認。
  - Windows updater の Ed25519 署名検証（release `e8bf615`）は main へ**意図的に不移植**: 今日の移植範囲外のうえ、
    release の配布署名インフラ（sign_update.py・公開鍵定数）と結合しており「2 ブランチを混ぜない」境界を跨ぐ。
    main は自分用で公開配布なし＝実害小。将来 main を公開配布する場合のみ別途設計する。
  - 検証: release Mac swift test 69 / Win unittest 406、main Mac 59 / Win 244 すべて緑。
    常用アプリは /Applications へ最新 DIST 差し替え済み（下の「マシン状態のメモ」参照）。
- 既往: 2026-07-04 の HUD 継続フィードバック第 5 弾まで消化済み・両ブランチ実装済み・未リリース。第 5 弾の内容:
  ⑬**ピルのガラス透け感強化・存在感低減**（ユーザー指示「もっとガラスっぽく・存在感を小さく・後ろの文字がぼやける感じ or もっとクリア」）。
  ダークモードの VEV は全 material が tint で塗り潰れ背後の文字が平均色になる（8 素材を白地黒文字上で実測比較・どれも透けない）。
  公開 API にブラー半径調整は無いため **`alphaValue=0.7` の線形混合**でブラー層を薄めて文字がぼんやり透ける軽いガラスにし、
  リム 0.09・上縁ハイライト 0.14・影 0.10/0.16 に弱めた（release=`6cb0054` / main=`278c078`）。
  あわせてグローバルスキル **`~/.claude/skills/apple-liquid-glass/`** を新設（ユーザー指示。決定木＝同一ウィンドウ内は
  glassEffect / ウィンドウ越しは VEV(.behindWindow) 一択・NSGlassEffectView はスナップショット・alpha が透け感の主ノブ・
  観測者効果の検証禁止則・比較ハーネス references/backdrop-test.swift 付き）。
- 既往: 2026-07-04 の HUD 継続フィードバック第 4 弾（両ブランチ実装済み）。内容:
  ⑩**ピル背景ガラスを NSVisualEffectView（.behindWindow）既定に変更**: NSGlassEffectView は映り込みが**実質スナップショット**
  （再レイアウトまで固定）で背景の色替えに追従しないことがユーザーの目視で確定（「黄色に変わっても赤のまま」）。
  第 3 弾までの「ライブ追従を確認済み」は **screencapture の観測者効果**（撮影自体が WindowServer の再合成を誘発し
  追従して見える）による誤検証だった。**バックドロップの鮮度は今後スクショで検証しない**（ユーザー目視が正）。
  VEV は Dock と同じ合成経路で毎フレームライブ（屈折の歪みは犠牲）。NSGlassEffectView は `VOICEKEY_HUD_LIQUID=1` の実験用に残置。
  ⑪**変換中のカプセルサイズ凍結**: 「変換中…」が左右に動く根因はカプセル縮小そのもの（フェード遅延の 2 回の調整では解消せず）。
  transcribing 中は contentSize を更新しない凍結で根絶（挿入遅延 0.22s の特例も撤去し一律 0.06s に戻した）。
  ⑫**Deepgram の韓国語誤判定を修正（両OS）**: nova-3 の `language=multi`（導入当時 ja 未対応の名残）の言語自動判定が根因。
  現在は ja サポート済み（2026-07 ドキュメント確認）のため WS/REST 全 4 経路（Mac 2＋Windows 2）で設定言語（既定 ja）を直接送る。
  旧仕様前提のテスト 2 件も更新。効果はユーザーの次回リアルタイム入力で確認（万一でも REST フォールバックで壊れない）。
  - 検証: swift test 61 件 pass・Python unittest 392 件 OK・DIST ビルド（strings localhost=0）→再起動→ピルのスクショで
    磨りガラスの映り込みを確認（鮮度＝ライブ追従はユーザー目視待ち）。
- 既往: **2026-07-04 の HUD 継続フィードバック第 3 弾まで消化・両ブランチ実装済み**
  （release=`905417e`+`9c3bd1d` / main=`d9c948d`・全 push 済み）。第 3 弾の内容:
  ⑥**録音中のライブ字幕表示を撤去**（ユーザー指示「ストリームはいらない」。ストリーミング文字起こし＝速度は不変・
  HUD は波形のみ。AppController の onInterim 配線と HudModel.caption 一式を削除。README のライブ字幕記述も更新済み・
  main README には該当記述なし） ⑦**変換中は「変換中…」テキスト自体をゆっくり明滅**（waveform マーク撤去・
  opacity 1.0⇄0.35）＋**その場フェードイン**（挿入遅延 0.22s＝カプセル縮小の収束後。「横から流れる」解消）
  ⑧**ピルを下端アンカーに**（カプセル下端＝画面下端+約 8pt に全モードで揃う。パネル内センター配置をやめた）
  ⑨**ガラスの鮮度リフレッシュ**: Space 切替・アプリ切替の通知で再描画を強制（タイマーなし）。
  （※このとき「3 シナリオすべてライブ追従を確認済み」としたのは screencapture の観測者効果による誤検証。
  第 4 弾でユーザー目視により反証され、VEV 既定化で解決した。上記⑩参照）。
- 既往（同日第 1〜2 弾）: ①変換中 waveform の scale 揺れ撤去 ②待機ピルの mic アイコン撤去（中身なし極小ピル）
  ③モーフの滑らか化（spring damping 0.6→0.82＋非対称フェード） ④ピル位置を下端寄りへ（浮き 24pt→8pt・のち⑧で下端アンカー化）
  ⑤**ピル背景を本物のガラスへ**: SwiftUI glassEffect は同一ウィンドウ内しかサンプルできず HUD では灰色の塊になる（構造問題）が、
  **AppKit の NSGlassEffectView（macOS 26・style=.clear）はウィンドウ越しに背後をサンプルできる**ことを実機で確認し採用
  （Hud.swift の HudBackdrop。原色ストライプ背景のスクショで映り込み＋フチの屈折を確認済み。macOS 26 未満と
  VOICEKEY_GLASS_FALLBACK=1 は NSVisualEffectView .behindWindow のすりガラス）。
  （第 1〜2 弾コミット: release=`43dcced`+`f39f59e` / main=`a7901e0`+`0c757ec`）
  内容: ①変換中 waveform の scale 揺れ撤去（opacity 明滅のみ） ②待機ピルの mic アイコン撤去（中身なし極小ピル）
  ③モーフの滑らか化（spring damping 0.6→0.82 でバウンス除去＋中身の非対称フェード） ④ピル位置を下端寄りへ（浮き 24pt→8pt）
  ⑤**ピル背景を本物のガラスへ**: SwiftUI glassEffect は同一ウィンドウ内しかサンプルできず HUD では灰色の塊になる（構造問題）が、
  **AppKit の NSGlassEffectView（macOS 26・style=.clear）はウィンドウ越しに背後をサンプルできる**ことを実機で確認し採用
  （Hud.swift の HudBackdrop。原色ストライプ背景のスクショで映り込み＋フチの屈折を確認済み。macOS 26 未満と
  VOICEKEY_GLASS_FALLBACK=1 は NSVisualEffectView .behindWindow のすりガラス）。DIST ビルド→再起動→スクショで
  透け・mic なし・位置を実機確認済み。モーフ・明滅の体感はユーザーの次回音声入力で確認（録音を要するため自動検証不可）。
- 既往: **2026-07-03 夜のフィードバック一式＋同日深夜の追加フィードバックを全消化・両ブランチ実装済み・未リリース**（CHANGELOG Unreleased・次版候補）。
  1. **新規ユーザー既定ホットキー**: 録音キー1=右⌘・hold・リアルタイム（release のみ・既存ユーザーの保存値は不変・スロット2 は元から要件どおり）
  2. **整形を「削らない 4 プリセット」化**: standard（言いよどみだけ除去・既定）/ punctuation（そのまま）/ clean（言い直し整理）/ bullets（箇条書き）。全プリセット「削除・要約禁止＋適切な句読点」共通。設定の録音キータブで選択・`preset_id` をサーバーへ貫通（実体は voicekey-site `lib/format.ts`・未知 ID は standard 解決で後方互換）。main は defaultPrompt の「削らない」刷新のみ（プリセット UI なし＝自由プロンプトで代替）。**LLM を通す現行方式は維持と確定**（STT はプロンプトを指示として実行できないため「音声 AI にプロンプトを渡す」案は不成立・format=1 統合で往復増なし）
  3. **長文並列分割の調整**: 分割数を 2 個（24s 未満）/ 3 個（24s 以上）に均等化制限（無制限分割の消費増を防止・順序不変）＋ 0.7s 境界が無いとき 0.35s で一度だけ再探索（分割ゼロで遅い問題の解消）
  4. **HUD 刷新**: ピル⇄録音インジケーターを単一カプセルの連続変形に作り直し（削除＋挿入 transition を排除）・波形⇄ストリーミング字幕の切替に spring（字幕中は固定幅 360pt でガタつきなし）・変換中のくるくるスピナー→waveform の明滅パルス（opacity 1.0⇄0.35・0.8s 往復）
  5. **ダッシュボード 3 カード再設計**: 累計入力（＋回数・録音時間・レベル/進捗バー）/ 節約できた時間（＋カップ麺→通勤→映画→睡眠→日数の 6 段階換算）/ この期間（今日⇄今週セグメント・AppStorage `home.periodDays` 永続化・numericText ロール）。統計 UI の設計方法論はグローバルスキル `~/.claude/skills/stats-dashboard-design/` に保存（ユーザー指示）
  6. **サイト「他社 5 秒」表記**: 調査の結果**本番は既に全部 1.5 秒**（SSR HTML＋配信中 JS バンドルまで検証・修正不要）。ユーザーにはハードリロード（Cmd+Shift+R）を案内
  - コミット: release=`69eea53`(HUD)/`e1a28c1`(ダッシュボード)/`b45ab07`(分割)/`e148e42`(整形＋ホットキー)/`211b382`(docs)、main=`ecbfaed`/`880cfa4`/`edc8b18`/`d38abc5`(適応移植)、voicekey-site=`bcbba55`(**本番デプロイ未・上記「次の一手」**)。全 push 済み。
  - 検証: release swift test **61 件**・main **51 件** 全 pass / DIST ビルド（strings localhost=0）→kill→再起動→ホーム画面スクショで 3 カードの実データ描画を確認。HUD アニメの実動確認は次回の音声入力時にユーザーが体感で確認（録音を要するため自動検証不可）。
- 既往: **Mac 大型機能バッチ実装完了・未リリース（CHANGELOG Unreleased・次版候補）**（2026-07-03。両ブランチ実装済み）。
  競合パリティ＋独自機能の一括追加: 操作音 / メディアダッキング（現在音量が 12% ターゲットより大きい時のみ下げる）/ 再貼り付けキー / 入力履歴 200 件（アプリ別メタデータ・既定 ON・ローカルのみ）/ アプリ別使用状況統計 / HUD アプリアイコン＋常時表示ピル（録音インジケーターへ spring モーフィング）/ ホーム画面ダッシュボード / メインウィンドウ統合（浮遊ガラス島はサイドバーのみ・設定は同一ウィンドウ内切替）/ サイドノッチ（黒スリット＋外枠・クリック透過禁止・履歴パネル＝検索付き・消去ボタンはホームのみ）/ アップデート UX 刷新（メニューの手動チェック撤去・起動5分後＋6時間ごとサイレント検知→ホーム左上に更新ピル・手動チェックは設定のバージョン情報タブのみ）/ 離鍵即応化（0.4s 待ち廃止）。
  - **Mac のみ実装。Windows への同等機能の移植は後日（ユーザー指示・未着手）**。リリースは両 OS 同期が原則なので、次版リリース前に Windows 分の扱い（移植 or Mac 先行の可否）を判断すること。
  - コミット: release=`1a3e4e5`(Phase A)/`c403612`(B)/`62467bc`(C+v3.1)/`d740f93`(v3.2)/`8509c2a`(docs)、main=`6ad5af3`/`1215d42`/`5b4a2de`（サイドバーはアカウント行なし・API キータブ/モデル選択維持で適応移植）。全 push 済み。
  - 検証: swift test release 54 件・main 46 件 pass。常用アプリは release DIST ビルド稼働中（strings localhost=0 確認済み）。
  - **本番 DMG 再インストールテスト済み**（2026-07-03）: voicekey.vercel.app から v1.8.0 DMG を DL→SHA-256 が site 原本と一致→codesign verify OK→/Applications の旧 v1.2.0 を置換→起動 OK・appcast 200。/Applications は v1.8.0 になった（旧 v1.2.0 の実機テストベースは消滅）。
- 既往（リリース済み）: **Mac v1.8.0 / Windows v1.8.0 リリース完了**（2026-07-03。release＝製品版ブランチ。全フィード 200 検証済み・既存ユーザーへ自動更新配信中）。
  **全面再設計一連（2026-07-02〜03・計画 whimsical-sprouting-popcorn）**: ①初回セットアップガイド（両OS・既存ユーザー非表示）②モード2択化（リアルタイム=Deepgram / スタンダード=Groq・ハンズフリー時は内部 ElevenLabs）③整形実効化（サーバー STT+整形統合 format=1・既定モデル `openai/gpt-oss-20b` 実測選定）④UI 文言全面刷新 ⑤新ブランドアイコン（両OS）。サイトも全面刷新（共通ヘッダー・ログイン→未登録ならワンクリック新規登録・ライト+インディゴ・新ロゴ）。
  - 検証: Swift 37件・Python 392件 pass / 配布フィード3種とも本番 1.8.0・200 / DMG・zip・setup.exe 200 / Windows version.json に Ed25519 署名付与（sign_update.py。**1.7.0 では署名が漏れていた**＝公開鍵入りビルドは署名なしフィードだと更新を拒否するため、今回の付与で自動更新経路が正常化）。
  - コミット: 本体 release=`b30ffcf`〜`0be8081`＋タグ `v1.8.0`、main=`cb3b970`（オンボーディング移植）、voicekey-site=`a28b1f5`（認証UX）/`0d3e793`（デザイン）/`47eb985`（整形モデル）/`04c84ad`（配布物）。
  - **注意（EmbeddedKeys）**: build_dmg の trap で `EmbeddedKeys.generated.swift` は dev スタブ（isDist=false）に戻っている。次に release でビルドする前に `generate_embedded_keys.sh --dist` を流すこと（localhost 接続バグ回避）。
- 既往（リリース済み）: **Mac v1.7.0 / Windows v1.7.0**（2026-07-01。release＝製品版ブランチ。全フィード 200 検証済み・既存ユーザーへ自動更新配信中）。
  **製品版の速度を main に近づける一連の改善（段階0〜3＋A）＋文字起こしの 3 モード整理＋アップロード FLAC 化**:
  - **3 モードに整理**: 高速リアルタイム=Deepgram（短命トークン直叩き＋ストリーミング）/ 正確性=Groq（普通入力の既定・プロキシ）/ 高精度=ElevenLabs（ハンズフリーの既定・プロキシ）。普通入力を Groq にして録音開始のトークン取得ステップを排除。
  - **アップロード FLAC 化（Mac）**: プロキシ経路（Groq/EL）だけ生 WAV を送っていたのを可逆 FLAC にそろえ、STT アップロードを約 43%（実測 234KB→100KB）に削減＝main（直叩き・FLAC）との差を「1 ホップ＋整形」だけに圧縮。サーバー Groq route が filename を Groq へ引き継ぐよう修正（`audio.flac`→FLAC 復号）。**Windows は当面 WAV 据え置き**（Python に無料 FLAC エンコーダ無し・native lib 同梱は onnxruntime バグ同種のリスク。出力同一・退行なし）。
  - **クライアントに親キーは埋め込まない**（サーバー認証層＝製品の核を維持）。「1 ホップを消すために鍵を PC に置く」案は不採用（鍵漏洩で課金悪用＋無料200/課金ゲート崩壊・実測でホップ差は数十ms級）。
  - 検証: swift build/test 22件 OK / voicekey-site tsc exit0 / FLAC 実測 234KB→100KB / 全フィード本番200（appcast build19・旧build18 delta=404・GitHub setup.exe 200）/ Mac build19 dist 起動確認(PID 35751)。
  - **配布（完了・2026-07-01）**: voicekey-site `vercel deploy --prod` 済み（Groq route filename forward＋配布フィード）。Mac=DMG（1,995,718B）＋appcast〔build 19・EdDSA 署名・delta 19→13..17〕、Windows=setup.exe〔293,481,437B・公開リポ `voicekey-releases` の Release `v1.7.0`（DL URL 200）・sha256 `9bc445…8f2f`〕＋`windows/version.json`（sha256 一致）。`downloads.json` は mac/windows とも 1.7.0。転送用 Release `winci-1.7.0`（本リポ private）は relay 後に削除済み。
  - コミット: 本体 release=`dd1f0d3`（FLAC）/`615fdbc`（build19+CHANGELOG統合）ほか段階0〜3（`cb47da9`〜`2e0b667`）・push 済み、voicekey-site=`601de1c`（Groq route）/`ffe6317`（配布フィード・remote 無し）。
  - **注意（EmbeddedKeys）**: build_dmg の trap で `EmbeddedKeys.generated.swift` は dev スタブ（isDist=false）に戻っている（untracked）。稼働中アプリは dist build19（isDist=true 焼き込み済み）だが、次に `build_app.sh` で作り直すときは release では `generate_embedded_keys.sh --dist` を先に流すこと（localhost 接続バグ回避）。
  - **実機での体感確認ポイント**: 稼働中 Mac アプリ(build19)で普通入力（右⌘ホールド）を一度使い、`[計測]` の「文字起こし◯ms／総計◯ms」を確認。FLAC 化でアップロードが軽くなったぶん main との差が縮んだかを見る。
- 既往（リリース済み）: **Mac v1.6.3 / Windows v1.6.3 リリース完了**（2026-06-29。release＝製品版ブランチ。全フィード 200 検証済み・既存ユーザーへ自動更新配信中）。
  製品版テストで挙がった「録音後の処理中に『サーバーに接続できませんでした』が出て入力が消える」を修正＝**「正確性」(ElevenLabs) 経路のタイムアウト分離＋サーバー実行上限の明示**:
  - **タイムアウトを用途別に分離**: 録音直前のトークン取得用の短い既定(15s)を文字起こし/整形にも流用していたため、長文や cold start で応答前に切れ「通信に失敗」になっていた。文字起こし=90s・整形=60s をリクエスト単位で個別延長（トークン取得は速さ優先で 15s 据置）。
  - **サーバー側プロキシの実行上限を明示**: EL・整形 両 route.ts に `maxDuration=60`（既定の短い上限で長文処理が途中強制終了し logUsage 到達前に落ちるのを防止）。**前回セッションが整形 route の maxDuration 欠落＋CHANGELOG 誤記（「整形は既に60」）を残していたのを発見・訂正**。
  - **stale テスト修正**: `test_backend_client` の EL multipart 検証が旧フィールド名 `language`（v1.6.2 で `language_code` に変更済）で HEAD でも FAIL していたのを実名に追従（v1.6.2 が unittest 未実行で見逃していた）。
  - 実装（両OS同期・release のみ）: Mac=`BackendClient.transcribeElevenLabs`/`formatText` に `timeoutInterval` 90/60、Windows=`backend_client.transcribe_elevenlabs`/`format_text` に `httpx.Timeout` 90/60、voicekey-site=EL/format route.ts に `maxDuration=60`。
  - 検証: py_compile OK / unittest **370 件 pass** / Mac `build_dmg`(build17)成功・kill→open で 1.6.3 起動確認(PID 90088) / voicekey-site `tsc` exit 0。
  - **配布（完了・2026-06-29）**: voicekey-site `vercel deploy --prod` 済み（EL/ephemeral 暖機 GET 本番 200・EL/format `maxDuration` 反映）。Mac=DMG（1,983,993B）＋appcast〔build 17・EdDSA 署名・delta 17→12..16〕、Windows=setup.exe〔293,432,726B・公開リポ `voicekey-releases` の Release `v1.6.3`（DL URL 200）・sha256 `a91df8…aa6c`〕＋`windows/version.json`（sha256 一致）。`downloads.json` は mac/windows とも 1.6.3。転送用 Release `winci-1.6.3`（本リポ private）は relay 後に削除済み。
  - コミット: 本体 release=`b983c34`（実装＋テスト修正＋docs・push 済み）、voicekey-site=`75b3514`（route.ts maxDuration＋Mac/Windows 配布フィード・remote 無し）。
- 既往（リリース済み）: **Mac v1.6.2 / Windows v1.6.2**（2026-06-29。release＝製品版ブランチ。全フィード 200 検証済み）。
  ユーザーの製品版テストで挙げた「正確性でも遅くなるのはダメ」に対応＝**「正確性」(ElevenLabs) の遅延対策（段階A）**:
  - **プロキシ関数の cold start 解消**: 「正確性」は client 直叩き用の短命キーが無く（single-use token は Scribe v2 Realtime 専用）サーバープロキシ経由で中継する。初回利用時に Vercel 関数の cold start（最大数秒）を踏むのが遅延主因。サーバーに**消費なし `GET` 暖機ハンドラ**を追加（本番で `{"warm":true}` 200 確認）、アプリが起動時＋240s 間隔で叩いて温存（高速リアルタイムと同じ機構を EL 経路にも適用）。
  - **ストリーミング透過**: 新クライアントは EL 形式 multipart（`file`+`model_id=scribe_v1`+`language_code`）を `x-vk-passthrough:1` で送り、サーバーは body をバッファせず EL へ流す（中継の二度手間削減）。旧クライアントは従来の formData 組み直し＝**後方互換**。精度・モデルは scribe_v1 のまま不変。
  - 実装（両OS同期・release のみ）: voicekey-site `app/api/v1/transcribe/elevenlabs/route.ts`（GET 暖機＋透過）。Mac=`BackendClient.warmElevenLabs()`／`transcribeElevenLabs` に透過ヘッダ＋EL 形式 multipart／`AppController` 起動時暖機・`startEphemeralWarmLoop` に EL スロット判定。Windows=`backend_client.warm_elevenlabs()`／`transcribe_elevenlabs` に透過ヘッダ＋`model_id`/`language_code`／`app.py` の `_prewarm_backend`・`_ephemeral_warm_loop` に EL スロット判定。
  - 検証: Mac=`build_dmg.sh` 成功（build 16）・kill→再起動で起動確認（PID 12213）。Windows=`py_compile` OK（この Mac に Windows 依存が無く unittest は未実行）。voicekey-site=`tsc --noEmit` exit 0。改善前ベースライン（usage_logs・過去14日 EL `proxy_transcribe`）= n=4・avg 1007ms・max 2005ms（cold 外れ値1件）。**注意: サーバー `latency_ms` は EL 取得区間のみ計測で Vercel 関数 cold start はハンドラ前に起きるため出ない→cold start 解消の確認は体感ベース**。
  - **配布（完了・2026-06-29）**: voicekey-site `vercel deploy --prod` 済み（EL GET 暖機が本番で `{"warm":true}` 200）。Mac=DMG（1,984,250B）＋appcast〔build 16・EdDSA 署名・delta 16→11..15〕、Windows=setup.exe〔293,466,392B・公開リポ `voicekey-releases` の Release `v1.6.2`（DL URL 200）・sha256 `6983ca…e94b`〕＋`windows/version.json`（sha256 一致）。`downloads.json` は mac/windows とも 1.6.2。転送用 Release `winci-1.6.2`（本リポ private）は relay 後に削除済み。
  - コミット: 本体 release=`9da9d6e`（実装＋docs・push 済み）、voicekey-site=`8b6651c`（route.ts）/`3f01683`（Mac 配布）/`d096938`（Windows 配布）。
  - **段階B（保留・後日）**: Scribe v2 Realtime の精度を実音声で再ベンチ→精度が許容なら「正確性」を**クライアント直結 websocket（single-use token）**に切替えてリレー自体を排除（＝直叩き相当の速さ）。精度が落ちるなら scribe_v1＋プロキシのまま維持。
- 既往（リリース済み）: **Mac v1.6.1 / Windows v1.6.1**（2026-06-29。release＝製品版ブランチ。全フィード 200 検証済み）。
  ユーザーの製品版テストで挙げた 2 点を修正:
  1. **無料体験の「高速リアルタイム」の話し始めの遅延を解消**。無料ユーザーは録音直前に短命トークン発行
     `POST /api/v1/auth/ephemeral`（無料枠 1 消費）を毎回叩くが、これが Vercel serverless の cold start（最大数秒）を
     踏んで「話し始めの待ち」になっていた。サーバーに**消費なし・認証なしの `GET` 暖機ハンドラ**を追加し、アプリが
     **起動時＋約 240s 間隔**でこの GET を叩いて発行関数を温存→録音時の POST が warm path に乗る。ユーザー選択の
     「より低リスクな根本対応」（消費 API を分離せず＝突破耐性と「1 録音=1 消費」を維持したまま cold start だけ潰す）に準拠。
  2. **使うたびに設定画面の「残り回数」が即減って見えるよう修正**。消費は元々サーバーが原子的に行っていたが、
     アプリの残量表示が起動/ログイン時の一度きりで更新されず古いまま見えていた。**録音 1 回ごとに残量を静かに取り直す**
     （UI を「確認中…」に落とさない quiet 更新・クリティカルパス外・有料/未ログインは no-op）。
  - 実装（両OS同期・release のみ）: voicekey-site `app/api/v1/auth/ephemeral/route.ts` に GET 暖機。
    Mac=`BackendClient.warmEphemeral()`／`AppController` の `startEphemeralWarmLoop()`＋起動時暖機の無料/有料分岐＋
    `taskFinished()`→`refreshEntitlementQuiet()`、`LoginCoordinator.refreshEntitlementQuiet()`＋`applyStatus()`。
    Windows=`backend_client.warm_ephemeral()`／`app.py` の `_prewarm_backend` 拡張＋`_ephemeral_warm_loop`＋
    `_refresh_entitlement_async`（`account_refreshed` シグナル）、`login_coordinator.refresh_entitlement(quiet=)`＋`_apply_status()`。
  - 検証: Mac=`build_app.sh` 成功・kill→再起動で起動確認。Windows=`py_compile` OK・`unittest discover -s tests` 実行（version_consistency/
    outstanding_count のテストも 1.6.1 対応に追従済み）。バージョン: constants.py / Info.plist(short) / README ステータス表すべて 1.6.1。
  - **配布（完了・2026-06-29）**: voicekey-site `vercel deploy --prod` 済み（GET 暖機ハンドラ本番反映＝`/api/v1/auth/ephemeral` GET が `{"warm":true}` 200）。
    Mac=DMG（1,982,639B）＋appcast〔build 15・EdDSA 署名・delta 15→10..14〕、Windows=setup.exe〔293,483,660B・公開リポ `voicekey-releases` の Release `v1.6.1`（DL URL 200）・sha256 `4c91a5…06ae`〕＋`windows/version.json`（sha256 一致）。`downloads.json` は mac/windows とも 1.6.1。転送用 Release `winci-1.6.1`（本リポ private）は relay 後に削除済み。
    コミット: 本体 release=`beefe7b`（実装＋docs）/`16137a2`（Info.plist build15）、voicekey-site=`631df5c`（GET 暖機）/`ff68dfa`（配布フィード）。
- 既往（リリース済み）: **Mac v1.6.0 / Windows v1.6.0**（2026-06-29。release＝製品版ブランチ。
  **Windows 配布版で VAD（無音圧縮・長文分割）が実際に効くように修正**＋**コードレビュー31項目の反映**。
  配布ビルドに `onnxruntime` が同梱されておらず（`silero-vad` の optional extra でしか入らず `requirements.txt` 未宣言）、
  `vad.py` の遅延 `import onnxruntime` が失敗 → `analyze()` が安全側の `(True, None)` を返し **VAD が常時無効**だった潜在バグを解消。
  `requirements.txt` に `onnxruntime>=1.16.1`、`voicekey.spec` に `collect_all('onnxruntime')`、`build_windows_dist.ps1` に
  「PyInstaller 後に `onnxruntime_pybind11_state*` が dist にあるか」の自己チェック（未同梱なら CI 失敗）を追加。
  setup.exe が 281MB→293MB に増えたのが onnxruntime 同梱の裏付け。あわせて未ログイン時ゲート文言を無料体験仕様に統一・
  CI ユニットテスト自動実行（`.github/workflows/tests.yml`）・lint クリーンアップ・ドキュメント整合も反映。
  - 実装（両OS/両ブランチ同期）: spec/ps 修正は release・main 両方へ。Mac 版 VAD はエネルギー RMS のため onnxruntime 無関係（実害は Windows のみ）。
    テスト: release Python 370／Swift 22、main Python 223 すべて pass。バージョン整合（constants/Info.plist/README）OK。
  - 配布物（**全て本番反映済み・全 200 確認**）: Mac=DMG（1,981,144B）＋Sparkle appcast〔build 14・EdDSA 署名・delta 14→9..13・鍵平文 0 件確認〕、
    Windows=setup.exe〔293MB・公開リポ `voicekey-releases` の Release `v1.6.0`（Latest）・sha256 `ec3315…b445`・DL URL 200〕＋
    `windows/version.json`（sha256 一致）。`downloads.json` は mac/windows とも 1.6.0。転送用 Release `winci-1.6.0`（本リポ private）は relay 後に削除済み。
  - コミット: 本体 release=`3ee499d`（版+spec+ps+CHANGELOG+Info.plist build14）、main=`46fd3f3`（spec+ps+CHANGELOG）、voicekey-site=`34b2983`（配布フィード・remote 無し）。レビュー31項目本体は既コミット（〜`dbe1cd7`）。
  - 既往: **Mac/Windows v1.5.1**（2026-06-28・製品版の初回録音・初回整形を高速化＝アプリ起動時にサーバー接続を 1 回暖機して cold start を体感から除去・無料枠不消費設計）、
    **Mac/Windows v1.5.0**（2026-06-27・アカウントごとに無料体験枠 200 回〔#11〕。`entitlements.free_quota/free_used` ＋
    `consume_free_quota` RPC・残枠ゼロは 402。本番 Supabase 適用・検証済み）、
    Mac/Windows v1.4.0（2026-06-27・使用実績のアカウント連携〔#10〕。`usage_stats` に紐付き複数端末合算・
    再インストール後も引き継ぎ。自動更新フィード Mac build 11 / Win version.json を配信済み）、
    Mac/Windows v1.3.1（2026-06-27・ログイン誤失効バグ修正＋レイテンシ短縮）、
    v1.3.0（2026-06-26・アクティベーションキー版）、v1.1.0（2026-06-18・製品版2モード化）、
    Mac v1.0.2 / Windows v1.0.1（2026-06-16・遅延根治）、Mac v1.0.0/v1.0.1（2026-06-12）、
    Windows v1.0.0 初公開（2026-06-14、GitHub Actions ビルド → voicekey-releases の GitHub Releases へ公開）。
  - テスターへの渡し方は https://voicekey.vercel.app を共有するだけ。
  - 注意: macOS 15 では DMG を開く段階でも Gatekeeper 警告が出る（実テスター報告）。
    手順はサイトと DMG 同梱手順書に記載済み（完了 → プライバシーとセキュリティ → このまま開く）。
- **dev/beta の起動運用（2026-06-12 確定）**: 普段は開発版だけを使う。
  `/Applications/voicekey.app` に開発版を常設（`macos/scripts/run_dev.sh` がビルド→インストール→
  再起動まで実施）。ベータ版の動作確認は `macos/scripts/run_beta.sh`（dist/ の最新 DMG を
  マウントして一時起動・終わったら run_dev.sh で戻る）。dist/voicekey.app はビルドごとに
  dev/dist で上書きされるため常用しない。見分け方: 設定画面に API キータブがあれば開発版。
- 次の一手: **v1.8.0（全面再設計一連）は両 OS 配布完了**（2026-07-03・全フィード 200 検証済み・自動更新配信中）。
  内容: ①初回セットアップガイド（オンボーディング・両OS・既存ユーザー非表示）②モード2択化（リアルタイム=Deepgram / スタンダード=Groq・ハンズフリー時は内部 ElevenLabs 自動切替）③テキスト整形の実効化（サーバープロンプト刷新＋STT+整形の1リクエスト統合＝format=1・整形既定はスタンダードのみ ON）④UI 文言全面刷新（専門用語排除）⑤新ブランドアイコン（両OS）。
  整形の既定モデルは実測選定で `openai/gpt-oss-20b`（reasoning_effort:low。llama-3.1-8b-instant は言い直しを意味破壊・速度同等）＝voicekey-site `lib/format.ts` のコード既定・`GROQ_FORMAT_MODEL` env で差替可。
  サイトも全面刷新済み（共通ヘッダー＋ログイン導線・ログイン→未登録ならワンクリック新規登録・ライト+インディゴ配色・新ロゴ）。
  配布物: Mac=DMG 2,391,898B＋appcast〔build 20・delta 20→14..19〕、Windows=setup.exe〔293,443,984B・`voicekey-releases` Release `v1.8.0`・sha256 `1f1cfc…5e82`・**Ed25519 署名を version.json に付与**（sign_update.py・1.7.0 では漏れていた）〕。`downloads.json` 両OS 1.8.0。転送用 `winci-1.8.0` は relay 後削除済み。タグ `v1.8.0`（release・`0be8081`）。
  実機での確認ポイント: 初回起動オンボーディングの実走（tccutil reset 後）と、スタンダード（Groq）単発送信で `[計測]` ログの整形が 0ms（サーバー統合が効いている証拠）になっているか。EL「高精度」の段階B（Scribe v2 Realtime でリレー排除）は選択肢に残る。
  残るユーザー作業は (1) **v1.2.0 以前に配布した埋め込み済み旧プロバイダーキーのローテーション**（各プロバイダーのダッシュボードで失効・再発行＝本人のみ可能・キー値は Claude が扱わない）、
  (2) **各 API ダッシュボードで利用上限・アラート設定**（保険）。無料体験 200 回の上限値は `entitlements.free_quota` の
  既定値（DB 側）で調整可能＝コード変更・再配布なしに将来見直せる。なお無料体験が広く使われ始めるため、
  各 API ダッシュボードの利用上限・コストアラート設定は早めにやっておくと安全。
- **リリース手順（Mac/Windows は常に両方を同じ修正内容で同期リリースする・版番号は Claude が semver で決める）**:
  - Mac: `cd macos && ./scripts/build_dmg.sh --version X.Y.Z`（Info.plist 自動 bump・署名・zip/DMG/appcast 生成）
    → DMG を `voicekey-site/downloads/`、`voicekey-X.Y.Z.zip`＋`voicekeyN-M.delta`＋`appcast.xml` を
    `voicekey-site/mac/` へコピー → `downloads.json` の mac を更新。
  - Windows: `gh workflow run windows-build.yml --ref release -f version=X.Y.Z`（**--ref release 必須**）→
    `gh run watch`（約15分）→ 成果物は転送用 Release `winci-X.Y.Z` にあるので
    `gh release download winci-X.Y.Z --repo Tomato-1101/voicekey` で取得 →
    **`venv/bin/python scripts/build/sign_update.py --exe <setup.exe> --version-json <version.json>` で
    Ed25519 署名を version.json に付与（このステップを飛ばすと公開鍵入りビルドが自動更新を拒否する。
    v1.7.0 で漏れた実績あり・必須）** → setup.exe を
    `voicekey-releases` の GitHub Releases（タグ `vX.Y.Z`）へ `gh release create` で公開 →
    署名済み `version.json` を `voicekey-site/windows/` へ置き、
    `downloads.json` の windows を更新（sha256・size も）→ 転送用 `winci-X.Y.Z` を削除。
  - 仕上げ: `cd voicekey-site && vercel deploy --prod`（既存ユーザーへ自動更新が配信される）→
    アプリ本体リポで Info.plist/CHANGELOG/README のコミット。
  - 詳細は docs/BUILD_WINDOWS.md。

## 将来の強化（既知の設計課題・優先度低）

- **claim_login_code のセッション共有（潜在設計課題）**: 現状アプリのログインはブラウザ経由のワンタイム
  コード交換で、ブラウザと同一の Supabase セッションを共有する設計。同一 refresh_token を
  ブラウザとアプリが同時にローテーションすると GoTrue の reuse 検知で全セッションが失効しうる。
  v1.3.1 でリフレッシュ直列化＋サーバー 409（アプリ側でセッションを消さない）により**観測された
  バグは解消済み**だが、根治はアプリ独立セッション化（`generateLink` + `verifyOtp` で
  アプリ専用セッションを発行し、`claim_login_code` の RPC を変更）。規模が大きいため将来対応。
- **Leaked Password Protection**: Supabase Auth で無効（任意のセキュリティ強化）。ダッシュボードの
  Auth 設定で有効化できる（HaveIBeenPwned 照合）。SQL ではなく設定トグル。
