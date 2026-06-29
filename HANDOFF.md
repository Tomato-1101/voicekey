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

- 現在地: **Mac v1.6.1 / Windows v1.6.1 リリース完了**（2026-06-29。release＝製品版ブランチ。全フィード 200 検証済み・既存ユーザーへ自動更新配信中）。
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
- 次の一手: **v1.6.1（高速リアルタイムの遅延解消＋残り回数の即時表示）は両 OS 配布完了**（2026-06-29・全フィード 200 検証済み・自動更新配信中）。
  実機での体感確認（無料体験アカウントで「高速リアルタイム」の話し始めの遅延が消えたか・録音ごとに残り回数が減って見えるか）が次の確認ポイント。
  残るユーザー作業は (1) **v1.2.0 以前に配布した埋め込み済み旧プロバイダーキーのローテーション**（各プロバイダーのダッシュボードで失効・再発行＝本人のみ可能・キー値は Claude が扱わない）、
  (2) **各 API ダッシュボードで利用上限・アラート設定**（保険）。無料体験 200 回の上限値は `entitlements.free_quota` の
  既定値（DB 側）で調整可能＝コード変更・再配布なしに将来見直せる。なお無料体験が広く使われ始めるため、
  各 API ダッシュボードの利用上限・コストアラート設定は早めにやっておくと安全。
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
