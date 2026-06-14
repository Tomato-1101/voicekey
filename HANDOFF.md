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
- ブランチは **main 一本**（2026-06-14 確定）。リリースは **git tag（vX.Y.Z）** で管理し、配布物は
  タグ時点の main から dist ビルドする。beta ブランチは廃止（ソロ運用では同期コストだけ増え、実際 8
  コミット遅れで放置され機能していなかった）。ソース非公開はブランチではなく「private リポジトリ ＋
  配布物に .py 平文を含めない」で担保する（後者は voicekey.spec の datas=('src','src') が根治対象・未完）。
- 配布物置き場: **すべて Vercel サイト**（https://voicekey.vercel.app、ソースは
  `/Users/tomato/Project/voicekey-site/`、`vercel deploy --prod` で更新）。
  DMG/exe は `/downloads/`、appcast+更新 zip は `/mac/`、version.json は `/windows/`、
  最新版表示は `/downloads.json`。**GitHub はテスターから一切見えない**（2026-06-12 ユーザー要望で
  GitHub Releases 方式から移行。voicekey-releases は private 化済み・もう使わない）。
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

- 現在地: **Mac 版 v1.0.1 リリース完了**（2026-06-12。アプリアイコン追加・DMG レイアウト改善・
  Sequoia 向けインストール手順修正）。Phase 0〜6 完了・Phase 7 中止。
  テスターへの渡し方は https://voicekey.vercel.app を共有するだけ。
  注意: macOS 15 では DMG を開く段階でも Gatekeeper 警告が出る（実テスター報告）。
  手順はサイトと DMG 同梱手順書に記載済み（完了 → プライバシーとセキュリティ → このまま開く）。
- **未完了タスク: Windows 版 v1.0.0 ビルド（2026-06-12 ユーザー判断で後回し）**。
  方式は **GitHub Actions に変更済み**（2026-06-12 ユーザー承認。PC またぎ・.env.dist 手運びを廃止）。
  ワークフロー `.github/workflows/windows-build.yml` 作成済み。
  残り: ①ユーザーが初回のみ `gh secret set -f .env.dist` で Secrets 登録（登録後 .env.dist は削除）
  ②`gh workflow run windows-build.yml -f version=1.0.0` → artifact を DL →
  voicekey-site へ配置 → downloads.json 更新 → `vercel deploy --prod`（詳細 docs/BUILD_WINDOWS.md）。
  ~/Desktop/voicekey-windows-build-prompt.txt（旧・実機ビルド用プロンプト）は不要になった。
- **dev/beta の起動運用（2026-06-12 確定）**: 普段は開発版だけを使う。
  `/Applications/voicekey.app` に開発版を常設（`macos/scripts/run_dev.sh` がビルド→インストール→
  再起動まで実施）。ベータ版の動作確認は `macos/scripts/run_beta.sh`（dist/ の最新 DMG を
  マウントして一時起動・終わったら run_dev.sh で戻る）。dist/voicekey.app はビルドごとに
  dev/dist で上書きされるため常用しない。見分け方: 設定画面に API キータブがあれば開発版。
- 次の一手: ①ユーザーが各 API ダッシュボードで利用上限・アラート設定
  ②ユーザーが `gh secret set -f .env.dist` → Windows 版 v1.0.0 を CI でビルド・リリース。
- 次回リリース（バグ修正等）の手順: main で修正 → `build_dmg.sh --version X.Y.Z` →
  DMG を voicekey-site/downloads/、zip+appcast を voicekey-site/mac/ へコピー →
  downloads.json 更新 → `vercel deploy --prod` → 既存ユーザーへ自動配布。
- Windows 版リリース手順: Mac で `--export-env` → `.env.dist` を Windows ビルド機へ →
  `build_windows_dist.ps1 -Version X.Y.Z` → setup.exe を voicekey-site/downloads/、
  version.json を voicekey-site/windows/ へ → downloads.json 更新 → `vercel deploy --prod`
  （詳細は docs/BUILD_WINDOWS.md）。
