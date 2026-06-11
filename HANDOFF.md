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
- 配布物置き場: 公開リポジトリ `voicekey-releases`（バイナリ+appcast+version.json のみ）。
- Apple Developer Program はこれから加入。それまで Apple Development 署名 + 右クリック→開く手順書同梱。
  ビルドスクリプトは Developer ID + notarize に切替可能なパラメータ化をしておく。
- Mac 版コード変更後は ビルド→旧プロセス kill→open→動作確認 までワンセット（CLAUDE.md ルール）。
- UI スモーク・テストでは secrets/keyring を必ずモック（実 Keychain ダイアログ事故防止）。

## フェーズと検証チェックポイント

- [ ] Phase 0: beta ブランチ + .gitignore（検証: git check-ignore で生成物3つが無視される）
- [ ] Phase 1: Mac キー埋め込み（検証: スタブビルドで通常動作 / --dist で strings に平文なし /
      DIST で API キータブ非表示 / Keychain 空環境で文字起こし成功）
- [ ] Phase 2: Sparkle + build_dmg.sh（検証: codesign --verify / ローカル http.server で旧→新更新 E2E /
      DMG 右クリック→開くで起動）
- [ ] Phase 3: 初回 Mac リリース（voicekey-releases 作成・v1.0.0 公開。ユーザー側: API 利用上限設定・
      Developer Program 加入）
- [ ] Phase 4: Windows 配布一式（embedded_keys / updater.py / voicekey.iss / build_windows_dist.ps1 /
      BUILD_WINDOWS.md / spec の src 平文同梱除去。検証: py_compile・pytest・オフスクリーン UI）
- [ ] Phase 5: マイク自動検出 両 OS（スコア = RMS p90 − p10。検証: Mac 実機 E2E / Win は pytest）
- [ ] Phase 6: Windows UI 再デザイン（検証: preview_ui.py のスクショをユーザーに提示）
- [ ] Phase 7（後日）: Developer ID + 公証切替（加入完了待ち）

## 現在地 / 次の一手

- 現在地: 計画承認直後。実装未着手。
- 次の一手: Phase 0（beta ブランチ・.gitignore）から開始。
