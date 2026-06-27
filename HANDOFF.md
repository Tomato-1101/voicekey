# HANDOFF — voicekey 無料テスト版（ベータ）配布

セッションをまたぐ作業の現在地。再開時はまずこれを読む。
承認済み計画の全文: `/Users/tomato/.claude/plans/api-api-api-mac-abstract-hare.md`

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
  入れるかは毎回ユーザー指定、未指定なら聞く。beta ブランチ廃止は維持。ソース非公開はブランチではなく
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

- 現在地: **v1.5.0 実装完了・配布は GO 待ち**（2026-06-27。release＝製品版ブランチ。
  **アカウントごとに無料体験枠 200 回**を付与。これまで「ログイン＋有効キー必須」で一切試せなかったのを、
  ログインすれば無料で 200 回まで文字起こしを試せるようにし、使い切ったら従来どおりアクティベーションキー登録へ誘導
  〔課金は未実装＝当面はキー登録のみが解放手段〕。数え方は「文字起こし 1 回＝1 消費・累計一度きり・整形は消費なし」。
  サーバーだけが見える本物の呼び出し回数で数えるためクライアント申告では突破不可。サーバーで原子的にカウント
  〔`entitlements.free_quota`(既定200)/`free_used` ＋ `consume_free_quota(uuid)` RPC＝条件付き UPDATE・残枠ゼロは 402〕。
  **DB マイグレーション `free_quota_count_based` は本番 Supabase に適用・検証済み**（free_used 0→1→reset 0 を実測）。
  サーバーコード〔`lib/apiAuth.ts` の `authorizeUsable`・ephemeral/elevenlabs〔consume〕・format〔no-consume〕・me〔残量返却〕〕
  は編集済み・`tsc --noEmit` 通過。**ただし未デプロイ**（`vercel deploy --prod` が GO 待ち）。
  アプリ側は Mac〔BackendClient/LoginCoordinator/SettingsView・build 12〕・Windows〔backend_client/login_coordinator/settings_window〕
  実装済み、Mac ビルド＆起動 OK・Windows 53 テスト〔無料体験分岐含む〕パス。後方互換: 旧サーバー応答（free_* 無し）でも 0 扱いで従来挙動。
  - **次の不可逆 GO（要ユーザー承認）**: ①`voicekey-site` を `vercel deploy --prod`〔無料体験がサーバー全体で有効化〕、
    ②Mac DMG ビルド〔`build_dmg.sh --version 1.5.0`〕、③Windows CI〔`windows-build.yml -f version=1.5.0`〕、
    ④GitHub Release 公開＝既存ユーザーへ自動更新配信。①を先に出さないと旧アプリが 200/402 の新挙動を受ける前に
    新アプリが残量表示を期待してしまうため、**①→②③④の順**。
  - 既往: Mac/Windows v1.4.0（2026-06-27・使用実績のアカウント連携〔#10〕。`usage_stats` に紐付き複数端末合算・
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
- 次の一手: **v1.5.0（無料体験）の配布 GO 待ち**（上記「次の不可逆 GO」①→②③④）。その後の残るユーザー作業は
  **各 API ダッシュボードで利用上限・アラート設定**のみ（保険）。無料体験 200 回の上限値は `entitlements.free_quota`
  の既定値（DB 側）で調整可能＝コード変更・再配布なしに将来見直せる。
- **リリース手順（Mac/Windows は常に両方を同じ修正内容で同期リリースする・版番号は Claude が semver で決める）**:
  - Mac: `cd macos && ./scripts/build_dmg.sh --version X.Y.Z`（Info.plist 自動 bump・署名・zip/DMG/appcast 生成）
    → DMG を `voicekey-site/downloads/`、`voicekey-X.Y.Z.zip`＋`voicekeyN-M.delta`＋`appcast.xml` を
    `voicekey-site/mac/` へコピー → `downloads.json` の mac を更新。
  - Windows: `gh workflow run windows-build.yml -f version=X.Y.Z` → `gh run watch`（約15分）→
    `gh run download -n voicekey-windows-installer -D /tmp/vk_ci/` → setup.exe を
    `voicekey-releases` の GitHub Releases（タグ `vX.Y.Z`）へ `gh release create` で公開 →
    CI が出力した `version.json`（GitHub Releases の URL を含む）を `voicekey-site/windows/` へ置き、
    `downloads.json` の windows を更新（sha256・size も）。
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
