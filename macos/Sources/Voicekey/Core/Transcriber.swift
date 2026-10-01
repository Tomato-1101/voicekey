//
//  Transcriber.swift
//  文字起こし API クライアント（OpenAI / Groq / ElevenLabs / Deepgram / Microsoft MAI）
//
//  - OpenAI / Groq: OpenAI 互換 REST（multipart WAV、Bearer 認証、text 応答）
//    gpt-transcribe だけは json 応答・languages[] で送る（2026-10-01 追加）
//  - ElevenLabs: Scribe API（multipart WAV、xi-api-key 認証、JSON 応答）
//  - Deepgram: prerecorded API（WAV 生バイト、Token 認証、JSON 応答）
//  - Microsoft MAI: Azure Speech の LLM Speech API（multipart WAV＋definition、
//    Ocp-Apim-Subscription-Key 認証、JSON 応答）
//  - Soniox はライブ（SonioxLiveTranscriber）専用で REST を持たない。ここに来るのはライブ接続で
//    文字が取れなかったときだけで、録音全体を新しいライブセッションへ流し直して救う
//

import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "transcriber")

/// 文字起こし失敗。message はそのままユーザー通知に使える日本語
struct TranscriptionError: LocalizedError {
    let message: String
    /// キーが未設定・無効・接続先未設定など「設定 › API キー」で直せる失敗か。
    /// true のとき AppController が API キーの設定画面へ案内する
    var needsApiKey: Bool = false
    /// サーバー応答本文（1 行化・切り詰め済み）。原因の切り分けに要るが、生 JSON を
    /// HUD に出すと読めないうえ通知が長くなるので message とは分け、行動ログにだけ出す
    var detail: String? = nil
    var errorDescription: String? { message }

    /// 応答本文を行動ログ 1 行に収まる形へ整える（改行・制御文字を空白にして詰め、limit 文字で切る）。
    /// HTML のエラーページのような巨大な本文でも全体を文字列化しないよう、先にバイト数で頭だけ取る
    static func responseDetail(_ data: Data, limit: Int = 200) -> String {
        // 1 文字は UTF-8 で最大 4 バイトなので、limit 文字ぶん取るには limit×4 バイトあれば足りる
        let head = String(decoding: data.prefix(limit * 4), as: UTF8.self)
        let flattened = String(String.UnicodeScalarView(head.unicodeScalars.map {
            $0.properties.generalCategory == .control ? " " : $0
        }))
        let oneLine = flattened.split(separator: " ", omittingEmptySubsequences: true).joined(separator: " ")
        guard oneLine.count > limit else { return oneLine }
        return String(oneLine.prefix(limit)) + "…"
    }
}

/// 音声サンプル（Float32, 16kHz, モノラル）を WAV (PCM16) に変換する
enum WavEncoder {
    static func encode(_ samples: [Float], sampleRate: Int = 16000) -> Data {
        // 範囲外サンプルのラップアラウンド（轟音ノイズ化）を防ぐためクリップ
        var pcm = Data(capacity: samples.count * 2)
        for s in samples {
            let clipped = max(-1.0, min(1.0, s))
            var v = Int16(clipped * 32767).littleEndian
            withUnsafeBytes(of: &v) { pcm.append(contentsOf: $0) }
        }

        var data = Data()
        let dataSize = UInt32(pcm.count)
        let byteRate = UInt32(sampleRate * 2)  // モノラル 16bit

        func appendLE<T: FixedWidthInteger>(_ value: T) {
            var v = value.littleEndian
            withUnsafeBytes(of: &v) { data.append(contentsOf: $0) }
        }

        data.append(contentsOf: "RIFF".utf8)
        appendLE(UInt32(36 + dataSize))
        data.append(contentsOf: "WAVE".utf8)
        data.append(contentsOf: "fmt ".utf8)
        appendLE(UInt32(16))           // fmt チャンクサイズ
        appendLE(UInt16(1))            // PCM
        appendLE(UInt16(1))            // モノラル
        appendLE(UInt32(sampleRate))
        appendLE(byteRate)
        appendLE(UInt16(2))            // ブロックサイズ
        appendLE(UInt16(16))           // ビット深度
        data.append(contentsOf: "data".utf8)
        appendLE(dataSize)
        data.append(pcm)
        return data
    }
}

/// 文字起こし API クライアント（バックエンドごとにリクエスト形式を切り替える）
///
/// 分割並列送信では複数タスクが同一インスタンスの transcribe を同時に呼ぶ。
/// 可変設定は configLock で保護し、URLSession は並列リクエストに対応するため
/// 実態としてスレッドセーフ。それを明示するため @unchecked Sendable とする。
final class Transcriber: @unchecked Sendable {

    /// Whisper 系（Groq / OpenAI）へ送る数字表記の style プロンプト。Whisper は prompt の表記
    /// スタイルに追従する性質があるため、半角数字を含む短い例文を与えて漢数字化を抑制する
    /// （Windows 版 api_transcriber._NUMERAL_STYLE_PROMPT と文言を一致させること）。
    static let numeralStyleHint = "数字は半角で表記します。例: 2026年7月15日、3人で1200円、成功率98.5%。"

    /// Whisper へ送る文字起こしプロンプトを組み立てる（数字 style ヒント + ユーザー設定プロンプト）。
    /// release はプロンプト非選択＝userPrompt は空なので style ヒントのみになる。純関数（テスト用）。
    static func whisperPrompt(userPrompt: String) -> String {
        let user = userPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        return user.isEmpty ? numeralStyleHint : numeralStyleHint + " " + user
    }

    let backend: Backend

    /// 文字起こしの経路。selectRoute が副作用なしで決める（テスト対象）。
    enum Route: Equatable {
        /// ログイン済み: 自社サーバー経由（短命 JWT 直叩き / プロキシ）
        case server
        /// Keychain のキーで直叩き（personal 常時 / 未ログイン開発ビルド）。
        /// personal は Keychain 直読でサーバー往復ゼロ＝最速。開発者の既存キーをそのまま使う。
        case directKeychain
        /// 配布ビルド未ログイン: 停止してログインを促す
        case needLogin
    }

    /// 経路選択（純関数・テスト対象）。personal は他条件（isDist/ログイン）に関わらず必ず
    /// Keychain 直叩きを選ぶ＝サーバー経路・ログイン要求を一切通らないことをテストで保証する。
    static func selectRoute(isPersonal: Bool, isDist: Bool, isLoggedIn: Bool) -> Route {
        if isPersonal { return .directKeychain }
        if isDist { return isLoggedIn ? .server : .needLogin }
        if isLoggedIn { return .server }
        return .directKeychain
    }

    // モデル等は設定変更時にメインスレッドが書き換え（接続を維持したまま更新する設計）、
    // 文字起こしタスクが別スレッドで読むため lock で保護する
    var model: String {
        get { configLock.lock(); defer { configLock.unlock() }; return _model }
        set { configLock.lock(); _model = newValue; configLock.unlock() }
    }
    var language: String {
        get { configLock.lock(); defer { configLock.unlock() }; return _language }
        set { configLock.lock(); _language = newValue; configLock.unlock() }
    }
    var prompt: String {
        get { configLock.lock(); defer { configLock.unlock() }; return _prompt }
        set { configLock.lock(); _prompt = newValue; configLock.unlock() }
    }

    private let configLock = NSLock()
    private var _model: String
    private var _language: String
    private var _prompt: String

    /// REST（音声ファイル一括）に投げるモデル名。
    /// openaiLive の gpt-live-transcribe は Realtime WS 専用で、REST に投げると
    /// 404 "Invalid URL (POST /v1/audio/transcriptions)" になる（2026-07-31 実測）。
    /// そのため同世代の一括用モデル gpt-transcribe へ差し替える。
    /// この経路はライブ接続が張れなかったとき（キー無し・WS 失敗）のフォールバックでのみ通る。
    private var restModel: String {
        guard backend == .openaiLive else { return model }
        return "gpt-transcribe"
    }

    /// gpt-transcribe の送信形式（json 応答・languages[]）で送るか。
    /// Whisper 系（Groq・旧 gpt-4o 系）は従来の text 応答・language のまま。
    /// モデルは設定変更で途中から変わりうるので、transcribe() で 1 回だけ評価して送信と解析の両方へ渡す
    /// （別々に評価すると、json で送って text として読む等の食い違いが起きる）。
    var usesGptTranscribeFormat: Bool {
        backend != .groq && restModel.hasPrefix("gpt-transcribe")
    }

    /// 接続を再利用するためバックエンドごとに URLSession を保持
    private let session: URLSession

    /// 以降の HTTP 経路（baseURL / setAuth / buildRequest / parseResponse）に `.appleLocal` は
    /// 到達しない（`transcribe()` の冒頭でオンデバイス経路へ分岐するため）。
    /// `.soniox`（REST を持たない）と `.azureMAI`（リソースごとの URL を別に組み立てる）も
    /// baseURL / buildRequest には来ない。switch の網羅性を満たすためだけに `.openai` と同じ枝へ畳んである。
    private var baseURL: URL {
        switch backend {
        case .openai, .openaiLive, .appleLocal, .soniox, .azureMAI: return URL(string: "https://api.openai.com/v1")!
        case .groq: return URL(string: "https://api.groq.com/openai/v1")!
        case .elevenlabs: return URL(string: "https://api.elevenlabs.io/v1")!
        case .deepgram: return URL(string: "https://api.deepgram.com/v1")!
        }
    }

    init(backend: Backend, model: String, language: String, prompt: String) {
        self.backend = backend
        self._model = model
        self._language = language
        self._prompt = prompt

        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 20
        config.timeoutIntervalForResource = 60
        config.httpMaximumConnectionsPerHost = 4
        self.session = URLSession(configuration: config)
    }

    deinit {
        // バックエンド変更で捨てられた旧インスタンスのセッションを明示破棄する
        // （invalidate しない URLSession は解放されず漸増リークになる）
        session.finishTasksAndInvalidate()
    }

    /// TLS 接続を事前確立して初回リクエストの往復を短縮する（録音開始時に呼ぶ）。
    /// 失敗しても文字起こしには影響しないため、結果は無視する。
    func prewarm() {
        // Soniox はライブ（WebSocket）専用で REST を叩かないので、温める接続が無い
        if backend == .soniox { return }
        guard let apiKey = Keychain.apiKey(for: backend) else { return }
        if backend == .azureMAI {
            // Azure はリソースごとに接続先が違う。TLS を張るだけなので認証なしの GET で足りる
            guard let base = Self.azureBaseURL(Keychain.azureSpeechEndpoint()) else { return }
            var request = URLRequest(url: base)
            request.timeoutInterval = 5
            session.dataTask(with: request) { _, _, _ in }.resume()
            return
        }
        // 軽量な GET エンドポイントにアクセスして接続だけ確立する
        let path: String
        switch backend {
        case .openai, .openaiLive, .groq, .elevenlabs, .appleLocal, .soniox, .azureMAI: path = "models"
        case .deepgram: path = "projects"
        }
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        setAuth(apiKey, on: &request)
        request.timeoutInterval = 5
        session.dataTask(with: request) { _, _, _ in }.resume()
    }

    /// バックエンドごとの認証ヘッダを設定する
    private func setAuth(_ apiKey: String, on request: inout URLRequest) {
        switch backend {
        case .openai, .openaiLive, .groq, .appleLocal, .soniox:
            request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        case .elevenlabs:
            request.setValue(apiKey, forHTTPHeaderField: "xi-api-key")
        case .azureMAI:
            request.setValue(apiKey, forHTTPHeaderField: "Ocp-Apim-Subscription-Key")
        case .deepgram:
            request.setValue("Token \(apiKey)", forHTTPHeaderField: "Authorization")
        }
    }

    /// 音声を文字起こしする
    /// - Parameters:
    ///   - samples: 音声データ（Float32, 16kHz, モノラル）
    ///   - serverFormat: groq × ログイン済みプロキシ経路のときだけ有効。true にすると
    ///     サーバー内で STT→整形まで統合実行し整形済みテキストを返す（他バックエンド・直叩きは無視）。
    ///   - presetId: 統合整形時の整形プリセット（serverFormat=true の groq 経路でのみ実効）。
    /// - Returns: 文字起こし結果（前後空白除去済み。serverFormat 時は整形済み）
    func transcribe(samples: [Float], serverFormat: Bool = false, presetId: String = "standard") async throws -> String {
        guard !samples.isEmpty else { return "" }

        // ローカル（Apple）は API を一切叩かない。オンデバイスで 1 回だけ書き起こす。
        // 通常はストリーミング経路（LocalSpeechTranscriber）を通るので、ここに来るのは
        // ストリーミングの確定が空だったときのフォールバック。
        if backend == .appleLocal { return try await transcribeLocally(samples: samples) }

        // どの経路で文字起こしするかを純関数で決める（personal=Keychain 直叩き / ログイン=サーバー /
        // 未ログイン開発=Keychain 直叩き / 配布未ログイン=ログイン要求）。selectRoute はテスト対象。
        // personal は他条件に関わらず必ず directKeychain＝BackendClient・サーバーを一切参照しない。
        switch Self.selectRoute(
            isPersonal: EmbeddedKeys.isPersonal,
            isDist: EmbeddedKeys.isDist,
            isLoggedIn: BackendClient.isLoggedIn
        ) {
        case .needLogin:
            // 配布版（製品版ビルド）は「アクティベーション必須」。埋め込みキーへはフォールバックしない。
            // 未ログイン＝停止して「設定 → アカウント」でログインを促す（ログイン後は無料体験で使える）。
            // 無料体験を使い切った/利用権が無い場合はサーバーが 402/403 を返し、
            // BackendError.freeQuotaExhausted / .noSubscription として後段で表面化する。
            throw TranscriptionError(message: "ログインすると無料体験で使えます（設定 → アカウント）")

        case .server:
            // ログイン済み: 自社サーバー経由（並存ガード）。
            // 高速リアルタイム=Deepgram は短命 JWT で直叩き（低レイテンシ核心を維持）、
            // 正確性=ElevenLabs / 高速=Groq はサーバープロキシ経由（バッチは短命キー非対応）。
            switch backend {
            case .groq: return try await transcribeGroqViaProxy(samples: samples, serverFormat: serverFormat, presetId: presetId)
            case .elevenlabs: return try await transcribeElevenLabsViaProxy(samples: samples)
            case .deepgram: return try await transcribeDeepgramViaJWT(samples: samples)
            case .openai, .openaiLive, .appleLocal, .soniox, .azureMAI:
                // 配布版は openai / openaiLive / soniox / azureMAI を提供しない（personal 限定）。
                // appleLocal はここに来ない（冒頭でオンデバイス経路へ分岐済み）。
                // 開発ビルド（ログイン）では従来どおり直叩きへ委ねる。
                if EmbeddedKeys.isDist {
                    throw TranscriptionError(message: "この文字起こし方式は配布版では利用できません")
                }
                // 下の共通の直叩き経路（Keychain キー）へフォールスルー
            }

        case .directKeychain:
            break  // 下の共通の直叩き経路へ（personal / 未ログイン開発とも Keychain キーを使う）
        }

        // 直叩き（personal / 未ログイン開発とも Keychain のキーで直接プロバイダーを叩く）。
        guard let apiKey = Keychain.apiKey(for: backend) else {
            throw TranscriptionError(message: Self.missingKeyMessage(for: backend), needsApiKey: true)
        }

        // Soniox は REST を持たない（ライブ専用）。ここに来るのはライブ接続で文字が取れなかったときだけ
        if backend == .soniox {
            return try await transcribeSonioxReplay(samples: samples, apiKey: apiKey)
        }

        // 送信形式と応答の解析形式は同じ判定を使う（途中でモデル設定が変わっても食い違わないよう 1 回だけ評価）
        let gptTranscribeFormat = usesGptTranscribeFormat
        let request: URLRequest
        if backend == .azureMAI {
            guard let url = Self.azureTranscribeURL(endpoint: Keychain.azureSpeechEndpoint()) else {
                throw TranscriptionError(
                    message: "Microsoft の接続先（エンドポイント）が未設定です（設定 › API キー で入力してください）",
                    needsApiKey: true
                )
            }
            request = azureRequest(url: url, wav: WavEncoder.encode(samples), apiKey: apiKey)
        } else {
            request = buildRequest(audio: encodeAudio(samples), apiKey: apiKey, gptTranscribeFormat: gptTranscribeFormat)
        }
        let start = Date()
        let data = try await send(request)
        // 応答が成功で返った＝課金された時点で使用量を記録する（数量だけ・本文は渡さない）。
        // openaiLive がここに来るのは REST フォールバックなので、実際に呼んだ restModel で数える
        ApiUsageStore.shared.recordAudio(
            provider: ApiProvider(backend: backend), model: restModel,
            seconds: Double(samples.count) / AudioRecorder.sampleRate)
        let text = TextNormalize.stripCJKSpaces(
            try parseResponse(data, gptTranscribeFormat: gptTranscribeFormat)
                .trimmingCharacters(in: .whitespacesAndNewlines)
        )
        let elapsed = Int(Date().timeIntervalSince(start) * 1000)
        log.info("\(self.backend.label, privacy: .public) 文字起こし完了: \(elapsed)ms, \(text.count) 文字")
        return text
    }

    // MARK: - ローカル（Apple）経路

    /// Apple のオンデバイス音声認識で 1 回だけ書き起こす。
    /// ネットワーク・API キー・サーバー往復のいずれも通らない。
    private func transcribeLocally(samples: [Float]) async throws -> String {
        guard #available(macOS 26.0, *) else {
            throw TranscriptionError(message: "ローカル（Apple）の文字起こしは macOS 26 以降でのみ使えます")
        }
        let start = Date()
        let session = LocalSpeechTranscriber(language: language)
        _ = session.start()
        session.send(samples)
        let text = await session.finish()
        guard !text.isEmpty else {
            throw TranscriptionError(
                message: "ローカル音声認識で文字を取得できませんでした（システム設定 > 一般 > キーボード > 音声入力 で言語を追加してください）"
            )
        }
        let elapsed = Int(Date().timeIntervalSince(start) * 1000)
        log.info("\(self.backend.label, privacy: .public) 文字起こし完了: \(elapsed)ms, \(text.count) 文字")
        return text
    }

    // MARK: - Soniox の救済（録音全体の再送）

    /// キー未設定の案内文。入力先（設定 › API キー）をそのまま案内する
    static func missingKeyMessage(for backend: Backend) -> String {
        "\(backend.label) の API キーが未設定です（設定 › API キー で入力してください）"
    }

    /// Soniox の失敗を、ユーザーが次に何をすればよいか分かる文言へ写す。
    /// コードは数値で来ても文字列で来ても文字列にそろえてある（SonioxTranscript）
    static func sonioxFailureMessage(_ failure: LiveFailure) -> String {
        if case .error(let code?, _) = failure {
            switch code {
            case "401", "403":
                return "Soniox の API キーが無効です（設定 › API キー を確認してください）"
            case "402":
                return "Soniox の残高・利用上限が尽きています"
            case "429":
                return "Soniox の同時接続・回数の上限に達しました"
            default:
                break
            }
        }
        return "Soniox に接続できませんでした（ネットワークを確認してください）"
    }

    /// Soniox の失敗がキーの無効（401/403）か。設定 › API キー へ案内するかの判定に使う
    static func isInvalidKeyFailure(_ failure: LiveFailure) -> Bool {
        if case .error(let code?, _) = failure { return code == "401" || code == "403" }
        return false
    }

    /// 録音中のライブ接続が失敗して文字が取れなかったときに、手元の録音を新しいセッションへ流し直す。
    /// 録音はもう終わっているので実時間は待たず、100ms 分ずつ続けて送信キューへ積む。
    /// 正常に終わって空なら空文字（本当に無言）。失敗して何も取れなければ原因別の文言で投げる
    private func transcribeSonioxReplay(samples: [Float], apiKey: String) async throws -> String {
        let session = SonioxLiveTranscriber(model: model, language: language, prompt: prompt)
        guard session.start(apiKey: apiKey) else {
            throw TranscriptionError(message: Self.missingKeyMessage(for: backend), needsApiKey: true)
        }
        let start = Date()
        let chunk = Int(AudioRecorder.sampleRate / 10)
        var index = 0
        while index < samples.count {
            let end = min(index + chunk, samples.count)
            session.send(Array(samples[index..<end]))
            index = end
        }
        let audioSeconds = Double(samples.count) / AudioRecorder.sampleRate
        let text = await session.finish(
            timeout: SonioxLiveTranscriber.replayFinishTimeout(audioSeconds: audioSeconds)
        )
        let ending = session.ending
        // 実時間より速く送ったときの処理時間は公式に明記が無い。待ち上限を実測で詰めるために必ず残す
        let elapsed = Int(Date().timeIntervalSince(start) * 1000)
        let summary = "Soniox 再送: 音声 \(String(format: "%.1f", audioSeconds))s → \(elapsed)ms, "
            + "\(text.count) 文字, 終わり方=\(ending.logLabel)"
        log.notice("\(summary, privacy: .public)")
        ActionLog.shared.write("transcriber", summary)

        // 失敗でも取れた分があれば返す（喋った内容を捨てない）。何も取れなければ原因を伝える
        if case .failed(let failure) = ending, text.isEmpty {
            throw TranscriptionError(
                message: Self.sonioxFailureMessage(failure),
                needsApiKey: Self.isInvalidKeyFailure(failure)
            )
        }
        return text
    }

    // MARK: - 製品版サーバー経路（段階3）

    /// 正確性（ElevenLabs）: サーバープロキシ経由で文字起こしする。
    /// アップロードは FLAC（可逆・約半分のサイズ）を優先し、失敗時のみ WAV へフォールバック
    /// （main の EL 直叩きと同じ符号化。EL passthrough は生ボディを EL へ流すだけなので FLAC がそのまま通る）。
    private func transcribeElevenLabsViaProxy(samples: [Float]) async throws -> String {
        let start = Date()
        let audio = encodeAudio(samples)
        do {
            let text = TextNormalize.stripCJKSpaces(
                try await BackendClient.transcribeElevenLabs(
                    audio: audio.data, filename: audio.filename,
                    contentType: audio.contentType, language: language
                ).trimmingCharacters(in: .whitespacesAndNewlines)
            )
            let elapsed = Int(Date().timeIntervalSince(start) * 1000)
            log.info("\(self.backend.label, privacy: .public) 文字起こし完了: \(elapsed)ms, \(text.count) 文字")
            return text
        } catch let e as BackendClient.BackendError {
            throw TranscriptionError(message: e.userMessage)
        }
    }

    /// 高速（Groq）: サーバープロキシ経由で文字起こしする（普通入力・バッチ）。
    /// アップロードは FLAC（可逆・約半分のサイズ）を優先し、失敗時のみ WAV へフォールバック。
    /// main（自分用）の Groq 直叩きと同じ符号化にすることで、release との差は「1 ホップ
    /// （Vercel Edge・東京）＋整形」だけになる（従来は WAV 送信で main の約2倍のアップロードだった）。
    ///
    /// serverFormat=true のときは `format=1` を付けてサーバーで STT→整形まで統合実行させ、
    /// 整形済みテキストを受け取る（録音後のクライアント整形の往復を省く＝単発送信時のみ使う）。
    /// presetId は統合整形時の整形プリセット（サーバーへ preset_id として送る）。
    private func transcribeGroqViaProxy(samples: [Float], serverFormat: Bool = false, presetId: String = "standard") async throws -> String {
        let start = Date()
        let audio = encodeAudio(samples)
        do {
            let text = TextNormalize.stripCJKSpaces(
                try await BackendClient.transcribeGroq(
                    audio: audio.data, filename: audio.filename,
                    contentType: audio.contentType, language: language, format: serverFormat,
                    presetId: presetId, prompt: Self.whisperPrompt(userPrompt: prompt)
                ).trimmingCharacters(in: .whitespacesAndNewlines)
            )
            let elapsed = Int(Date().timeIntervalSince(start) * 1000)
            log.info("\(self.backend.label, privacy: .public) 文字起こし完了: \(elapsed)ms, \(text.count) 文字")
            return text
        } catch let e as BackendClient.BackendError {
            throw TranscriptionError(message: e.userMessage)
        }
    }

    /// 高速リアルタイム（Deepgram）: 短命 JWT を取得し Bearer で直叩きする。
    /// クエリ構築は直叩きと共通の deepgramRequest を使い、認証だけ Token→Bearer に差し替える。
    private func transcribeDeepgramViaJWT(samples: [Float]) async throws -> String {
        let grant: BackendClient.EphemeralToken
        do {
            grant = try await BackendClient.fetchEphemeralToken()
        } catch let e as BackendClient.BackendError {
            throw TranscriptionError(message: e.userMessage)
        }
        var request = deepgramRequest(audio: encodeAudio(samples), apiKey: "")
        request.setValue("Bearer \(grant.token)", forHTTPHeaderField: "Authorization")

        let start = Date()
        let data = try await send(request)
        // Deepgram の解析は gpt-transcribe 形式を見ない（OpenAI 系の判定なので常に false）
        let text = TextNormalize.stripCJKSpaces(
            try parseResponse(data, gptTranscribeFormat: false).trimmingCharacters(in: .whitespacesAndNewlines)
        )
        let elapsed = Int(Date().timeIntervalSince(start) * 1000)
        log.info("\(self.backend.label, privacy: .public) 文字起こし完了: \(elapsed)ms, \(text.count) 文字")
        // 文字起こしが成立したら無料体験の消費を確定する（ベストエフォート・非ブロッキング）。
        // ストリーミングが空文字で REST へフォールバックしたとき、その 1 録音を数えるのはこの経路。
        // 段階1=保留 ID(jti) を確定 / 段階3=再利用トークンの回数を +1（jti なし）。
        if !text.isEmpty {
            if let jti = grant.jti {
                Task { await BackendClient.confirmUsage(jti: jti) }
            } else if grant.meter {
                Task { await BackendClient.confirmUsageCount() }
            }
        }
        return text
    }

    // MARK: - 送信・符号化（直叩き / サーバー経路で共通）

    /// FLAC（可逆圧縮）で WAV 比約半分までアップロードサイズを削減する。
    /// 量子化は WAV と同じ 16bit のため精度への影響はゼロ。失敗時は WAV。
    private func encodeAudio(_ samples: [Float]) -> EncodedAudio {
        if let flac = FlacEncoder.encode(samples) {
            return EncodedAudio(data: flac, filename: "audio.flac", contentType: "audio/flac")
        }
        return EncodedAudio(
            data: WavEncoder.encode(samples), filename: "audio.wav", contentType: "audio/wav"
        )
    }

    /// リクエストを送り、ステータス検査を通過したボディを返す。
    /// 通信失敗・HTTP エラーはそのままユーザー通知に使える日本語例外へ写す。
    private func send(_ request: URLRequest) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError where error.code == .timedOut {
            throw TranscriptionError(
                message: "\(backend.label) API がタイムアウトしました（ネットワークを確認してください）"
            )
        } catch {
            throw TranscriptionError(
                message: "\(backend.label) API への接続に失敗しました: \(error.localizedDescription)"
            )
        }

        guard let http = response as? HTTPURLResponse else {
            throw TranscriptionError(message: "\(backend.label) API から不正な応答を受信しました")
        }
        switch http.statusCode {
        case 200:
            break
        case 401:
            throw TranscriptionError(
                message: "\(backend.label) の API キーが無効です（設定 › API キー を確認してください）",
                needsApiKey: true
            )
        case 429:
            throw TranscriptionError(message: "\(backend.label) API のレート制限に達しました（しばらく待って再試行してください）")
        default:
            // 本文は HUD に出さない（detail に分けて行動ログの transcriber 行にだけ残す）
            throw TranscriptionError(
                message: "\(backend.label) API エラー (HTTP \(http.statusCode))",
                detail: TranscriptionError.responseDetail(data)
            )
        }
        return data
    }

    // MARK: - リクエスト構築（バックエンド別）

    /// アップロードする符号化済み音声（FLAC または WAV）。
    /// リクエスト組み立てをテストから呼ぶため internal にしている
    struct EncodedAudio {
        let data: Data
        let filename: String
        let contentType: String
    }

    private func buildRequest(audio: EncodedAudio, apiKey: String, gptTranscribeFormat: Bool) -> URLRequest {
        switch backend {
        // soniox / azureMAI はここに来ない（transcribe() で先に分岐する）。網羅性のためだけに畳む
        case .openai, .openaiLive, .groq, .appleLocal, .soniox, .azureMAI:
            return openAIRequest(audio: audio, apiKey: apiKey, gptTranscribeFormat: gptTranscribeFormat)
        case .elevenlabs:
            return elevenLabsRequest(audio: audio, apiKey: apiKey)
        case .deepgram:
            return deepgramRequest(audio: audio, apiKey: apiKey)
        }
    }

    /// OpenAI 互換 Audio Transcriptions API（OpenAI / Groq 共用）。
    /// テストから送信内容を確かめるため internal にしている
    /// - Parameter gptTranscribeFormat: gpt-transcribe の形で送るか（transcribe() が 1 回だけ評価した値）
    func openAIRequest(audio: EncodedAudio, apiKey: String, gptTranscribeFormat: Bool) -> URLRequest {
        var request = URLRequest(url: baseURL.appendingPathComponent("audio/transcriptions"))
        request.httpMethod = "POST"
        setAuth(apiKey, on: &request)

        var form = MultipartForm()
        form.field("model", restModel)
        if gptTranscribeFormat {
            // gpt-transcribe は json 応答で、言語は複数指定の languages[] で渡す
            // （単数の language と両方送らない。temperature は仕様に無いので送らない）
            form.field("response_format", "json")
            if !language.isEmpty { form.field("languages[]", language) }
            // gpt-transcribe の prompt は自由形式の「文脈」として読まれるので、Whisper 用の数字の例文を
            // 混ぜると例文そのものが出力に漏れうる。ユーザーのプロンプトだけを送る（空なら送らない）。
            // 数字の表記はアプリ側の NumeralNormalizer が半角へ直す
            let user = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
            if !user.isEmpty { form.field("prompt", user) }
        } else {
            form.field("response_format", "text")
            form.field("temperature", "0")
            if !language.isEmpty { form.field("language", language) }
            // Whisper 系（Groq / 旧 gpt-4o 系）は prompt の表記に追従するので、数字を半角で出させる
            // style プロンプト（＋ユーザー設定プロンプト）を常に付与する
            form.field("prompt", Self.whisperPrompt(userPrompt: prompt))
        }
        form.file("file", filename: audio.filename, contentType: audio.contentType, data: audio.data)
        form.apply(to: &request)
        return request
    }

    /// ElevenLabs Scribe API（speech-to-text）。テストから送信内容を確かめるため internal にしている
    func elevenLabsRequest(audio: EncodedAudio, apiKey: String) -> URLRequest {
        var request = URLRequest(url: baseURL.appendingPathComponent("speech-to-text"))
        request.httpMethod = "POST"
        setAuth(apiKey, on: &request)

        var form = MultipartForm()
        form.field("model_id", model)
        // (笑い) などの音声イベントタグは音声入力には不要。scribe_v2 は既定で付けるので必ず明示する
        form.field("tag_audio_events", "false")
        if !language.isEmpty { form.field("language_code", language) }
        form.file("file", filename: audio.filename, contentType: audio.contentType, data: audio.data)
        form.apply(to: &request)
        return request
    }

    /// Deepgram prerecorded API（符号化済み音声の生バイトを直接送る）
    private func deepgramRequest(audio: EncodedAudio, apiKey: String) -> URLRequest {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("listen"), resolvingAgainstBaseURL: false
        )!
        var query = [
            URLQueryItem(name: "model", value: model),
            URLQueryItem(name: "smart_format", value: "true"),
        ]
        if language.isEmpty {
            // 言語未指定（自動判定）の場合は言語検出を有効化する
            query.append(URLQueryItem(name: "detect_language", value: "true"))
        } else {
            // nova-3 も現在は ja 等の単言語指定をサポート済み（2026-07 ドキュメント確認）。
            // 旧実装の multi（多言語自動判定）は日本語を韓国語等に誤判定するため使わない。
            query.append(URLQueryItem(name: "language", value: language))
        }
        components.queryItems = query

        var request = URLRequest(url: components.url!)
        request.httpMethod = "POST"
        setAuth(apiKey, on: &request)
        request.setValue(audio.contentType, forHTTPHeaderField: "Content-Type")
        request.httpBody = audio.data
        return request
    }

    /// Azure の API バージョン（LLM Speech API の MAI-Transcribe 対応版）
    private static let azureAPIVersion = "2025-10-15"

    /// AZURE_SPEECH_ENDPOINT を正規化したベース URL（scheme + host + port だけを使う）。
    /// Azure ポータルからコピーした値に余計なパス・クエリが付いていても壊れないよう、それらは捨てる。
    /// scheme の無い値（xxx.cognitiveservices.azure.com）は https を補う。
    /// 未設定・空白を含む値・ホストを持たない値は nil
    static func azureBaseURL(_ endpoint: String?) -> URL? {
        guard var text = endpoint?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty,
              !text.contains(where: \.isWhitespace) else {
            return nil
        }
        if !text.contains("://") { text = "https://" + text }
        guard let parsed = URLComponents(string: text), let host = parsed.host, !host.isEmpty else {
            return nil
        }
        var base = URLComponents()
        base.scheme = parsed.scheme
        base.host = host
        base.port = parsed.port
        return base.url
    }

    /// 文字起こしの送信先 URL。appendingPathComponent はパス中の「:」をエスケープしうるので文字列で組む
    static func azureTranscribeURL(endpoint: String?) -> URL? {
        guard let base = azureBaseURL(endpoint) else { return nil }
        return URL(string: base.absoluteString
            + "/speechtotext/transcriptions:transcribe?api-version=\(azureAPIVersion)")
    }

    /// multipart の definition（LLM Speech の enhancedMode で MAI モデルを指定する）。
    /// 言語が空なら locales を付けない＝自動判定に任せる
    static func azureDefinitionJSON(model: String, language: String) -> String {
        var definition: [String: Any] = ["enhancedMode": ["enabled": true, "model": model]]
        let lang = language.trimmingCharacters(in: .whitespaces)
        if !lang.isEmpty { definition["locales"] = [lang] }
        guard let data = try? JSONSerialization.data(withJSONObject: definition, options: [.sortedKeys]),
              let json = String(data: data, encoding: .utf8) else { return "{}" }
        return json
    }

    /// Microsoft MAI-Transcribe（Azure Speech の LLM Speech API）。テストから送信内容を確かめるため internal
    func azureRequest(url: URL, wav: Data, apiKey: String) -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        setAuth(apiKey, on: &request)

        var form = MultipartForm()
        form.file("audio", filename: "audio.wav", contentType: "audio/wav", data: wav)
        form.field("definition", Self.azureDefinitionJSON(model: model, language: language))
        form.apply(to: &request)
        return request
    }

    /// Azure の応答から本文を取り出す。combinedPhrases[0].text を優先し、無ければ phrases を連結する。
    /// 無音（どちらも空配列）は空文字、形が違えば nil（解析失敗）
    static func parseAzureResponse(_ data: Data) -> String? {
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return nil }
        let combined = obj["combinedPhrases"] as? [[String: Any]]
        if let text = combined?.first?["text"] as? String { return text }
        if let phrases = obj["phrases"] as? [[String: Any]] {
            // 区切りの空白は stripCJKSpaces が日本語の間だけ取り除く
            return phrases.compactMap { $0["text"] as? String }.joined(separator: " ")
        }
        return combined != nil ? "" : nil
    }

    // MARK: - 応答解析（バックエンド別）

    private struct ElevenLabsResponse: Decodable {
        let text: String
    }

    /// gpt-transcribe（response_format=json）の応答
    private struct OpenAIJSONResponse: Decodable {
        let text: String
    }

    private struct DeepgramResponse: Decodable {
        struct Alternative: Decodable { let transcript: String }
        struct Channel: Decodable { let alternatives: [Alternative] }
        struct Results: Decodable { let channels: [Channel] }
        let results: Results
    }

    /// 応答本文を取り出す。テストから確かめるため internal にしている
    /// - Parameter gptTranscribeFormat: 送信時と同じ判定（gpt-transcribe なら json 応答として読む）
    func parseResponse(_ data: Data, gptTranscribeFormat: Bool) throws -> String {
        switch backend {
        case .openai, .openaiLive, .groq, .appleLocal, .soniox:
            if gptTranscribeFormat {
                guard let parsed = try? JSONDecoder().decode(OpenAIJSONResponse.self, from: data) else {
                    throw TranscriptionError(message: "\(backend.label) API の応答を解析できませんでした")
                }
                return parsed.text
            }
            // response_format=text のためプレーンテキストがそのまま返る
            return String(data: data, encoding: .utf8) ?? ""
        case .azureMAI:
            guard let text = Self.parseAzureResponse(data) else {
                throw TranscriptionError(message: "\(backend.label) API の応答を解析できませんでした")
            }
            return text
        case .elevenlabs:
            guard let parsed = try? JSONDecoder().decode(ElevenLabsResponse.self, from: data) else {
                throw TranscriptionError(message: "\(backend.label) API の応答を解析できませんでした")
            }
            return parsed.text
        case .deepgram:
            guard let parsed = try? JSONDecoder().decode(DeepgramResponse.self, from: data),
                  let transcript = parsed.results.channels.first?.alternatives.first?.transcript else {
                throw TranscriptionError(message: "\(backend.label) API の応答を解析できませんでした")
            }
            return transcript
        }
    }
}

/// multipart/form-data リクエストボディの組み立てヘルパー
private struct MultipartForm {
    private let boundary = "voicekey-\(UUID().uuidString)"
    private var body = Data()

    mutating func field(_ name: String, _ value: String) {
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n".utf8))
        body.append(Data("\(value)\r\n".utf8))
    }

    mutating func file(_ name: String, filename: String, contentType: String, data: Data) {
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data(
            "Content-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\n".utf8
        ))
        body.append(Data("Content-Type: \(contentType)\r\n\r\n".utf8))
        body.append(data)
        body.append(Data("\r\n".utf8))
    }

    func apply(to request: inout URLRequest) {
        var final = body
        final.append(Data("--\(boundary)--\r\n".utf8))
        request.setValue(
            "multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type"
        )
        request.httpBody = final
    }
}
