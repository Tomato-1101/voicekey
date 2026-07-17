# personal エディション（個人用最速版）

開発者本人の日常利用専用の Mac ビルド。**配布しない**（顧客に渡さない・GitHub Releases にも載せない）。
目的は「サーバー / ログイン / 課金の往復ゼロで最速」「音声入力オンリー」「見た目は製品版（release）と同等・ただしライト固定」。

> ブランチ: **`personal`**（`release` から派生）。`main` / `release` は一切変更しない。

---

## 1. これは何か（設計の要点）

- **personal = `release` + エディションフラグ `EmbeddedKeys.isPersonal`**。
  コードのほぼ全ては release と共通で、`isPersonal == true` のときだけ挙動を差し替える。
- **STT は Keychain 直読の直叩き**。`isPersonal` のとき、Deepgram（WS ストリーミング）・Groq・ElevenLabs は
  **開発者の Keychain（`voicekey.Deepgram` / `voicekey.Groq` / `voicekey.ElevenLabs`）から鍵を直読して
  直接プロバイダーを叩く**（＝`isDist=false` の既存「Keychain 直叩き」経路をそのまま流用）。
  自社サーバー（`BackendClient`）・短命 JWT・warm ループ・`confirmUsage`・ログイン/課金ゲートは**一切通らない**。
  Keychain 読みはプロセス内キャッシュ済みで即時（同期）＝サーバー往復ゼロ＝先読みコールド窓が原理的に発生しない。
- **鍵は埋め込まない**。バイナリにプロバイダーキーを焼き込まない（2026-06-28 のセキュリティ方針を維持）。
  鍵ローテ時も再ビルド不要（Keychain の値を更新するだけ）。
- **音声入力オンリー**。会話 / 秘書 / Realtime / agent 系は `voice-agent` ブランチにのみ存在し、
  personal（release 由来）には無い＝新たな会話系機能は入れない。
- **外観は OS 追従**（ライト/ダーク双方に馴染む）。以前 `NSApp.appearance = .aqua` でライト固定していたが、
  ダーク環境で HUD の浮遊ガラス（Liquid Glass）まで白くなり「前のデザインが白い」と指摘されたため撤去した（2026-07-17）。
- **認証/課金 UI 非表示**。ログイン/アカウント/サブスク/API キータブ/利用権プロンプトを隠す。
  オンボーディングからログインステップを除外する。
- **署名・バンドル ID は release と同一**（Apple Development 証明書・`com.voicekey.app`・TeamID `9KT598FS4A`）。
  これにより既存の Keychain 項目（`partition_id` に teamid が入った状態）を personal ビルドがそのまま読める。
  ad-hoc 署名やバンドル ID 変更は **禁止**（Keychain の所有が壊れて毎回パスワード要求が出る）。

## 2. personal 固有の差分（`isPersonal` で分岐している箇所だけ）

マージ衝突を最小化するため、personal 固有の差分は下記の箇所に閉じ込めている。

| ファイル | 分岐内容 |
| --- | --- |
| `macos/scripts/generate_embedded_keys.sh` | `--personal` で `isPersonal=true` / `isDist=false` を生成（キーは焼かない） |
| `macos/Sources/Voicekey/Core/Transcriber.swift` | `selectRoute()` が personal を常に `.directKeychain`（サーバー/ログイン要求を通らない） |
| `macos/Sources/Voicekey/Core/StreamingTranscriber.swift` | `start()` 冒頭で personal は Keychain の Deepgram キーで直結 |
| `macos/Sources/Voicekey/AppController.swift` | personal のとき streaming の interim を HUD へライブ字幕配線 |
| `macos/Sources/Voicekey/UI/Hud.swift` | 録音ピルに `liveText`（固定幅・tail 表示）を追加（`setLiveText` は録音中のみ反映）。ライブ字幕が出ている間は音量バーを非表示にする |
| `macos/Sources/Voicekey/UI/SettingsView.swift` | personal はアカウントナビ項目・アカウント行・API キータブを非表示 |
| `macos/Sources/Voicekey/UI/OnboardingView.swift` | personal は `goNext/goBack` でログインステップを飛ばす |
| `macos/Sources/Voicekey/Core/Keychain.swift` | personal は `authSession()` が常に nil を返す（旧 release DIST の残存トークンがあっても未ログイン扱い）。`BackendClient.isLoggedIn` が本メソッド依存なので、起動時の利用権確認・warm ループ・短命トークン取得などのサーバー往復が単一点で全て no-op になる |

`Transcriber.selectRoute(isPersonal:isDist:isLoggedIn:)` は純関数で、`Tests/VoicekeyTests/TranscribeRouteTests.swift`
が「personal は isDist / ログイン状態に関わらず必ず `.directKeychain`（サーバー経路・ログイン要求を通らない）」ことを保証する。

## 3. 3 版の同期方式

- **共通機能は `release` に実装する**。バグ修正・新機能・UI 改善は release で作り、
  `git switch personal && git merge release` で personal に取り込む。
- personal 固有の差分は上表の `isPersonal` 分岐だけに閉じ込めてあるため、release からのマージは
  基本的に衝突しない（衝突が出るのは同じ行を両方で触ったときだけ）。
- personal から release へは **持ち込まない**（personal は非配布の私用ブランチ）。
  `main` / `release` は一切変更しない（2 ブランチ運用のルールを守る）。

## 4. ビルド手順（personal ビルドを作る）

```bash
cd macos
./scripts/generate_embedded_keys.sh --personal   # isPersonal=true・isDist=false を生成（キーは焼かない）
./scripts/build_app.sh                            # dist/voicekey.app を Apple Development 証明書で署名ビルド
```

> `EmbeddedKeys.generated.swift` は git 管理外（.gitignore 済み）。ブランチを切り替えたら必ず
> 対象エディションで再生成すること（release/main へ戻すときは `--dist` か引数なしで再生成）。

## 5. 日常利用ビルドを /Applications へ入れる（実キーは Keychain 前提）

鍵はアプリに焼き込まず開発者の **Keychain** から読む。Keychain に鍵が無い場合だけ、先に release/main の
設定画面「API キー」から一度保存しておく（`voicekey.Deepgram` 等の項目ができる）。鍵が既にあれば下記だけでよい。

```bash
cd /Users/tomato/Project/voicekey/macos

# 1) personal エディションで生成 → 署名ビルド
./scripts/generate_embedded_keys.sh --personal
./scripts/build_app.sh

# 2) 旧プロセスを止める（常駐アプリなので kill してから差し替える）
pgrep -x voicekey && pkill -x voicekey || true

# 3) /Applications へ差し替え（既存を消してからコピー）
rm -rf /Applications/voicekey.app
cp -R dist/voicekey.app /Applications/voicekey.app

# 4) 起動
open /Applications/voicekey.app
```

> 注意: release の DIST ビルド（サーバー経由・ログイン版）を常用している場合、この差し替えで
> personal（Keychain 直叩き・非配布）に置き換わる。元に戻すには release ブランチで
> `./scripts/generate_embedded_keys.sh --dist && ./scripts/build_app.sh` してから同様に差し替える。

## 6. 将来: 1 コードベースのビルドフラグ（エディション）へ寄せる道筋

現状は `main` / `release` / `personal` を 3 ブランチで運用しているが、差分の実体は
「エディションフラグ（`isDist` / `isPersonal` と、main の実プロバイダー名・モデル選択 UI）」に集約されつつある。
将来は次のように 1 コードベース＋ビルド時フラグへ寄せられる。

- `EmbeddedKeys`（生成物）を **エディション列挙**（`enum Edition { case dev, dist, personal, mine }`）に拡張し、
  `generate_embedded_keys.sh --<edition>` で 1 つ生成する。
- ブランチ固有仕様（release の認証層、main のモデル選択 UI、personal のライト固定・認証非表示）を
  すべて `Edition` 分岐（`if edition == .personal` 等）へ移し、ブランチではなくフラグで切り替える。
- そうすればブランチは 1 本（または最小限）になり、`merge` の同期作業自体が不要になる。
- 移行は一気にやらず、まず現在 `isPersonal` で分岐している箇所を `Edition` 経由に置き換えるところから始める
  （挙動を変えずにフラグ体系だけ差し替える）。
