# voicekey for Mac

macOS ネイティブ（Swift / SwiftUI）の音声入力アプリ。
ホットキーを押している間マイクで録音し、離すと OpenAI / Groq / ElevenLabs / Deepgram API で
文字起こしして、アクティブなウィンドウにテキストを貼り付けます。

## 特徴

- **メニューバー常駐**: Dock に表示されない軽量アプリ（アイコン色で状態表示）
- **デュアルホットキー**: 2 つのホットキーに別々のバックエンド / モデルを割り当て可能
  - 既定: 右⌘（OpenAI gpt-4o-transcribe）/ 右⌥（Groq whisper-large-v3-turbo）
  - 右側修飾キー単独のホットキーに対応（CGEventTap でデバイスビットを直接判定）
- **4 つのバックエンド**: OpenAI / Groq / ElevenLabs (Scribe) / Deepgram から選択
- **ログイン時自動起動**: 初回起動時に自動登録（設定画面のトグルで解除可能）
- **小型 HUD**: 録音中だけ画面下部に音声レベル連動の波形ピルを表示
- **ダブルタップで自動送信**: ホットキーを素早く 2 回押して録音すると、貼り付け後に Enter を自動送信
- **VAD（発話検出）**: Apple SoundAnalysis のオンデバイス ML 分類器で発話を判定。
  無音・ノイズだけの録音は API に送らない（幻覚と無駄なコストを防止）
- **音量正規化**: ゲイン上限つき正規化でノイズフロアの過剰増幅を防ぐ
- **クリップボード復元**: 貼り付け後に元のクリップボード内容を自動復元
- **Keychain 保存**: API キーは macOS キーチェーンに保存（Python 版と同一エントリを共用）

## ビルド

要件: macOS 14+ / Xcode（Command Line Tools）

```bash
cd macos

# 開発実行
swift run

# .app バンドルを作成（dist/voicekey.app）
./scripts/build_app.sh
open dist/voicekey.app
```

### 署名について（重要 — 権限の引き継ぎ）

`build_app.sh` は自己署名証明書 `voicekey-codesign` があればそれで署名する。
**ad-hoc 署名（`-` 署名）はビルドごとに「別アプリ」扱いになり、マイク・入力監視・
アクセシビリティの許可が毎回リセットされてホットキーが効かなくなる。**
証明書署名なら署名 ID が固定され、リビルド後も許可が引き継がれる。

一度だけ以下を実行して証明書を作成・登録する（登録時に Mac のログインパスワードを求められる）:

```bash
# コード署名用の自己署名証明書を作成
cat > /tmp/vk_csr.conf <<'EOF'
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = voicekey-codesign
[v3]
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
basicConstraints = critical,CA:false
EOF
openssl req -x509 -newkey rsa:2048 -keyout /tmp/vk_key.pem -out /tmp/vk_cert.pem \
  -days 3650 -nodes -config /tmp/vk_csr.conf
# macOS 互換の p12 を /usr/bin/openssl(LibreSSL) で作る（OpenSSL3 の p12 は import 不可）
/usr/bin/openssl pkcs12 -export -out /tmp/vk_identity.p12 \
  -inkey /tmp/vk_key.pem -in /tmp/vk_cert.pem -passout pass:voicekey -name voicekey-codesign
# login キーチェーンに取り込み、コード署名用として信頼
security import /tmp/vk_identity.p12 -k ~/Library/Keychains/login.keychain-db \
  -P voicekey -T /usr/bin/codesign
security add-trusted-cert -p codeSign -k ~/Library/Keychains/login.keychain-db /tmp/vk_cert.pem
```

ad-hoc 版から証明書版に切り替えた直後は、古い許可が残っているため一度だけ
リセットして付け直す:

```bash
tccutil reset ListenEvent com.voicekey.app
tccutil reset Accessibility com.voicekey.app
tccutil reset Microphone com.voicekey.app
open dist/voicekey.app   # 再起動して許可ダイアログに従う（この 1 回だけ）
```

以降はリビルドしても許可を聞かれない。

## 初回セットアップ

1. 起動するとメニューバーにマイクアイコンが表示される
2. 権限ダイアログに従って以下を許可（システム設定 → プライバシーとセキュリティ）
   - **マイク**: 録音用
   - **入力監視**: グローバルホットキー検出用
   - **アクセシビリティ**: テキスト貼り付け（Cmd+V 合成）用
3. メニューバーアイコン → 「設定…」 → 「API キー」タブで OpenAI / Groq の API キーを保存
4. ホットキー（既定: 右⌘）を押しながら話し、離すとテキストが入力される

権限を変更した後はアプリの再起動が必要です。

## アーキテクチャ

```
Sources/Voicekey/
├── VoicekeyApp.swift        # エントリポイント（MenuBarExtra・状態アイコン）
├── AppController.swift      # 中央コントローラー（状態機械・パイプライン直列化）
├── AppState.swift           # アプリ状態の定義
├── Config/
│   └── AppConfig.swift      # 設定モデル（UserDefaults 永続化）
├── Core/
│   ├── HotkeyMonitor.swift  # CGEventTap キー監視（タップ自動再有効化つき）
│   ├── KeyToken.swift       # キートークン定義・左右修飾キー判定
│   ├── AudioRecorder.swift  # AVAudioEngine 録音（16kHz モノラル変換）
│   ├── VoiceActivity.swift  # 音量正規化・VAD（SoundAnalysis + エネルギー）
│   ├── Transcriber.swift    # OpenAI / Groq REST クライアント（WAV multipart）
│   ├── Keychain.swift       # API キーの Keychain 保存
│   └── Paster.swift         # クリップボード貼り付け（復元つき）
└── UI/
    ├── Hud.swift            # 録音 HUD（NSPanel + SwiftUI）
    ├── SettingsView.swift   # 設定ウィンドウ
    └── HotkeyRecorderView.swift  # ホットキー入力欄
```

設計上の要点:

- **ホットキーのコールバックは絶対にブロックしない**。CGEventTap が OS に
  無効化されると ホットキーが効かなくなるため、コールバックはトークン集合の
  更新とメインスレッドへのディスパッチのみ。さらに `tapDisabledByTimeout` を
  受けたら即座に再有効化する
- **UI 状態は単一の発信点**（`AppController.emitState()`）が内部状態から計算する
- **文字起こしパイプラインは直列チェーン**（連続録音でも挿入順を保証）
