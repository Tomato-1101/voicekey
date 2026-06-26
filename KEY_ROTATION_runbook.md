# 旧バージョン無効化 = 提供元キーのローテーション手順

最終更新: 2026-06-27 / 対象: 全配布版

## なぜこれが必要か

- 旧版（v1.0.0〜v1.2.0）は **アプリに埋め込まれた提供元 API キーで各プロバイダを直接叩く**。
  サーバーを経由しないので、こちらのサーバー側では止められない。
- **唯一確実に旧版を無効化する方法は、埋め込まれている提供元キーを失効（revoke）させること**。
- ただし **v1.3.0 のサーバー（`voicekey.vercel.app`）も提供元キーを使う**ので、何も考えず revoke すると
  v1.3.0 まで止まる。→ **サーバーを新キーへ移してから、旧キーを revoke する**順序を厳守する。

## いま分かっている構成（値は未確認・本人だけが知る）

| | 旧版アプリ（埋め込み） | v1.3.0 アプリ | サーバー（Vercel） |
|---|---|---|---|
| Deepgram | 埋め込みキー直叩き | サーバー発行の短命JWTで直叩き | `DEEPGRAM_API_KEY` を使用 |
| ElevenLabs | 埋め込みキー直叩き | サーバープロキシ経由 | `ELEVENLABS_API_KEY` を使用 |
| Groq（整形） | 埋め込みキー直叩き | サーバープロキシ経由 | `GROQ_API_KEY` を使用 |
| OpenAI | （release では未使用） | 配布版は非対応 | 未使用 |

- **v1.3.0 配布ビルドは埋め込みキーを一切使わない**（`_dist_guard` が未ログインをブロック・コードで確認済み）。
  → 埋め込みキーを失効しても **v1.3.0 は無傷**。影響を受けるのは旧版だけ。
- 埋め込みキーは Mac の Keychain（`voicekey.Deepgram` など / account `default`）と
  GitHub Secrets（`DEEPGRAM_API_KEY` など）から **ビルド時に**取り込まれている。
  これらはあなたが普段使う「自分用ビルド(main)」が読む Keychain と同じ可能性が高い。
  → **revoke するとあなたの自分用ビルドも同じ鍵なら止まる**（製品版 v1.3.0 に乗り換える前提なら問題なし）。

## 手順（この順序を厳守）

### Deepgram / ElevenLabs / Groq（サーバーが使う3つ）— 1プロバイダずつ実施

各プロバイダについて、**新キー発行 → Vercel 更新 → 動作確認 → 旧キー失効** の順で 1 つずつ進める
（まとめてやらない。1つ終えて v1.3.0 が動くのを確認してから次へ）。

1. **新キーを発行**（プロバイダのダッシュボード。あなたの作業＝ログインが要る）
   - Deepgram: https://console.deepgram.com → API Keys → Create
   - ElevenLabs: https://elevenlabs.io → Profile → API Keys → Create
   - Groq: https://console.groq.com/keys → Create API Key
   - 発行直後の平文キーをコピー。

2. **Vercel の本番環境変数を新キーに更新**（`voicekey-site` プロジェクト）
   - https://vercel.com → voicekey プロジェクト → Settings → Environment Variables
   - 対象変数（Production）を新しい値に更新:
     - Deepgram → `DEEPGRAM_API_KEY`
     - ElevenLabs → `ELEVENLABS_API_KEY`
     - Groq → `GROQ_API_KEY`
   - 保存後、**再デプロイが要る**（env 変更は再デプロイで反映）。
     `cd /Users/tomato/Project/voicekey-site && vercel deploy --prod`（Claude に頼んでもよい）。

3. **v1.3.0 で動作確認**（ログイン＋有効キーの状態で）
   - そのプロバイダのモードで文字起こしが通ること。
     Deepgram=「高速リアルタイム」/ ElevenLabs=「正確性」/ Groq=テキスト整形が効くこと。

4. **旧キーを失効（revoke / delete）**（プロバイダのダッシュボード）
   - 各プロバイダの API Keys 一覧で、**新キー以外の古いキーを全部 revoke/delete**。
   - これで旧版アプリ（v1.0.0〜1.2.0）の埋め込みキーが死ぬ。

### OpenAI（サーバー未使用）

- release の旧版では未使用なので Vercel 更新は不要。
- 旧版・自分用ビルドに残る OpenAI 直叩きを止めたい場合だけ:
  https://platform.openai.com/api-keys で古いキーを revoke。

## 完了後の状態

- 旧版（v1.0.0〜1.2.0）: 埋め込みキーが revoke 済み → **文字起こし不可（実質無効化）**。
- v1.3.0: サーバーが新キーで動く → **ログイン＋アクティベーションキーがあれば正常**。
- 自分用ビルド(main): 同じ鍵を Keychain に持っていたなら止まる → 製品版 v1.3.0 を使うか、
  Keychain に別の鍵を入れ直す。

## 注意

- **埋め込み用の Keychain / GitHub Secrets は新キーで更新しない**こと。
  v1.3.0 以降はサーバー専用に倒す方針なので、埋め込みキーは休眠のままで良い
  （新キーを埋め込むと再びアクティベーションを迂回できてしまう）。
- 新キーは **Vercel（サーバー）にだけ**置く。
