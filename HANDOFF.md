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
- 配布用ブランチは `beta`。開発は main、リリース時に main → beta マージ → dist ビルド → タグ。
- 配布物置き場: 公開リポジトリ `voicekey-releases`（バイナリ+appcast+version.json のみ。2026-06-12 に
  ユーザー承認のうえ public 作成済み）。テスターに渡す URL は Vercel のダウンロードページ
  **https://voicekey.vercel.app**（ソースは `/Users/tomato/Project/voicekey-site/`、
  `vercel deploy --prod` で更新）。DL ボタンは GitHub API で最新リリースの DMG/exe 直リンクを自動解決。
- **Apple Developer Program には加入しない（2026-06-12 ユーザー確定）**。配布は Apple Development 署名 +
  右クリック→開く手順書同梱が恒久形。Phase 7（公証切替）は中止。
  build_dmg.sh の --identity / --notarize パラメータは残置するが使わない。
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
- ~~Phase 7: Developer ID + 公証切替~~ — **中止**（2026-06-12、ユーザーが Apple Developer Program
  不加入を決定。Apple Development 署名 + 手順書の現行方式が恒久形）

## ユーザー待ちの項目（Phase 3 の完了に必要）

1. ~~public リポジトリ voicekey-releases の作成承認~~ — **完了**（2026-06-12 ユーザー承認 →
   https://github.com/Tomato-1101/voicekey-releases を public 作成・push 済み。
   同日に Vercel のダウンロードページ https://voicekey.vercel.app も公開済み）。
2. ~~実キービルドの初回 Keychain 許可~~ — **完了**（2026-06-12 ビルド成功・v1.0.0 公開済み）。
3. **各 API ダッシュボードで利用上限・アラート設定**（現行キーをそのまま埋め込むため必須の保険。
   **唯一の残り項目**）。

## 現在地 / 次の一手

- 現在地: **Mac 版 v1.0.0 リリース完了**（2026-06-12）。Phase 0〜6 完了・Phase 7 中止。
  テスターへの渡し方は https://voicekey.vercel.app を共有するだけ。
- 次の一手: ①ユーザーが各 API ダッシュボードで利用上限・アラート設定（唯一の残り）
  ②Windows 版は Windows 実機で docs/BUILD_WINDOWS.md の手順でビルド → Setup.exe を
  Releases に添付 → version.json を windows/ にコミット → サイトの Windows ボタンが自動で生きる。
- 次回リリース（バグ修正等）の手順: main で修正 → `build_dmg.sh --version X.Y.Z` →
  DMG を Releases に添付 + zip/appcast を mac/ に上書きコミット → 既存ユーザーへ自動配布。
- Windows 版リリース手順: Mac で `--export-env` → `.env.dist` を Windows ビルド機へ →
  `build_windows_dist.ps1 -Version X.Y.Z` → Releases 添付 → version.json コミット
  （詳細と順序の注意は docs/BUILD_WINDOWS.md）。
