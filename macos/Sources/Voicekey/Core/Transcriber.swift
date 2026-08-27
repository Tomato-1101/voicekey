//
//  Transcriber.swift
//  文字起こし API クライアント（OpenAI / Groq / ElevenLabs / Deepgram）
//
//  - OpenAI / Groq: OpenAI 互換 REST（multipart WAV、Bearer 認証、text 応答）
//  - ElevenLabs: Scribe API（multipart WAV、xi-api-key 認証、JSON 応答）
//  - Deepgram: prerecorded API（WAV 生バイト、Token 認証、JSON 応答）
//

import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "transcriber")

/// 文字起こし失敗。message はそのままユーザー通知に使える日本語
struct TranscriptionError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
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

    /// 接続を再利用するためバックエンドごとに URLSession を保持
    private let session: URLSession

    /// 以降の HTTP 経路（baseURL / setAuth / buildRequest / parseResponse）に `.appleLocal` は
    /// 到達しない（`transcribe()` の冒頭でオンデバイス経路へ分岐するため）。
    /// switch の網羅性を満たすためだけに `.openai` と同じ枝へ畳んである。
    private var baseURL: URL {
        switch backend {
        case .openai, .openaiLive, .appleLocal: return URL(string: "https://api.openai.com/v1")!
        case .groq: return URL(string: "https://api.groq.com/openai/v1")!
        case .elevenlabs: return URL(string: "https://api.elevenlabs.io/v1")!
        case .deepgram: return URL(string: "https://api.deepgram.com/v1")!
        case .gemini: return URL(string: "https://generativelanguage.googleapis.com/v1beta")!
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
        guard let apiKey = Keychain.apiKey(for: backend) else { return }
        // 軽量な GET エンドポイントにアクセスして接続だけ確立する
        let path: String
        switch backend {
        case .openai, .openaiLive, .groq, .elevenlabs, .appleLocal: path = "models"
        case .deepgram: path = "projects"
        // 認証なしでも TLS を張れれば十分（温めるのは接続であってレスポンスではない）
        case .gemini: path = "models"
        }
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        setAuth(apiKey, on: &request)
        request.timeoutInterval = 5
        session.dataTask(with: request) { _, _, _ in }.resume()
    }

    /// バックエンドごとの認証ヘッダを設定する
    private func setAuth(_ apiKey: String, on request: inout URLRequest) {
        switch backend {
        case .openai, .openaiLive, .groq, .appleLocal:
            request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        case .elevenlabs:
            request.setValue(apiKey, forHTTPHeaderField: "xi-api-key")
        case .gemini:
            // Google は Authorization ではなく専用ヘッダでキーを渡す
            request.setValue(apiKey, forHTTPHeaderField: "x-goog-api-key")
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
            case .openai, .openaiLive, .appleLocal, .gemini:
                // 配布版は openai / openaiLive / gemini を提供しない（いずれも personal 限定）。
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
            throw TranscriptionError(
                message: "\(backend.label) の API キーが未設定です（設定画面から保存してください）"
            )
        }

        let request = buildRequest(audio: encodeAudio(samples), apiKey: apiKey)
        let start = Date()
        let data = try await send(request)
        let text = TextNormalize.stripCJKSpaces(
            try parseResponse(data).trimmingCharacters(in: .whitespacesAndNewlines)
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
        let text = TextNormalize.stripCJKSpaces(
            try parseResponse(data).trimmingCharacters(in: .whitespacesAndNewlines)
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
            throw TranscriptionError(message: "\(backend.label) の API キーが無効です（設定を確認してください）")
        case 429:
            throw TranscriptionError(message: "\(backend.label) API のレート制限に達しました（しばらく待って再試行してください）")
        default:
            let detail = String(data: data.prefix(200), encoding: .utf8) ?? ""
            throw TranscriptionError(message: "\(backend.label) API エラー (HTTP \(http.statusCode)): \(detail)")
        }
        return data
    }

    // MARK: - リクエスト構築（バックエンド別）

    /// アップロードする符号化済み音声（FLAC または WAV）
    private struct EncodedAudio {
        let data: Data
        let filename: String
        let contentType: String
    }

    private func buildRequest(audio: EncodedAudio, apiKey: String) -> URLRequest {
        switch backend {
        case .openai, .openaiLive, .groq, .appleLocal:
            return openAIRequest(audio: audio, apiKey: apiKey)
        case .elevenlabs:
            return elevenLabsRequest(audio: audio, apiKey: apiKey)
        case .deepgram:
            return deepgramRequest(audio: audio, apiKey: apiKey)
        case .gemini:
            return geminiRequest(audio: audio, apiKey: apiKey)
        }
    }

    /// OpenAI 互換 Audio Transcriptions API（OpenAI / Groq 共用）
    private func openAIRequest(audio: EncodedAudio, apiKey: String) -> URLRequest {
        var request = URLRequest(url: baseURL.appendingPathComponent("audio/transcriptions"))
        request.httpMethod = "POST"
        setAuth(apiKey, on: &request)

        var form = MultipartForm()
        form.field("model", restModel)
        form.field("response_format", "text")
        form.field("temperature", "0")
        if !language.isEmpty { form.field("language", language) }
        // 数字を半角で出させる style プロンプト（＋ユーザー設定プロンプト）を常に付与する。
        // このリクエストは Whisper 系（OpenAI / Groq 直叩き）専用なので Whisper 前提でよい。
        form.field("prompt", Self.whisperPrompt(userPrompt: prompt))
        form.file("file", filename: audio.filename, contentType: audio.contentType, data: audio.data)
        form.apply(to: &request)
        return request
    }

    /// ElevenLabs Scribe API（speech-to-text）
    private func elevenLabsRequest(audio: EncodedAudio, apiKey: String) -> URLRequest {
        var request = URLRequest(url: baseURL.appendingPathComponent("speech-to-text"))
        request.httpMethod = "POST"
        setAuth(apiKey, on: &request)

        var form = MultipartForm()
        form.field("model_id", model)
        // (笑い) などの音声イベントタグは音声入力には不要
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

    /// Gemini 3.5 Transcribe（Interactions API・文字起こし専用モデル）
    ///
    /// 他の 3 社と違いフォームではなく JSON で、音声は base64 にして `data` に載せる
    /// （小さい音声は Files API へ上げずに 1 往復で済む。実測で 220KB の WAV でも通る）。
    /// `Api-Revision` ヘッダは Interactions API の必須指定。
    private func geminiRequest(audio: EncodedAudio, apiKey: String) -> URLRequest {
        var request = URLRequest(url: baseURL.appendingPathComponent("interactions"))
        request.httpMethod = "POST"
        setAuth(apiKey, on: &request)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(Self.geminiApiRevision, forHTTPHeaderField: "Api-Revision")

        var transcriptionConfig: [String: Any] = [
            // smart = フィラー除去・句読点付けまでモデル側でやる（音声入力の用途に合う）。
            // verbatim は言い直しや「えーと」もそのまま出すので使わない。
            "mode": ["type": "smart"]
        ]
        // 言語未指定は自動判定に任せる（85 言語以上を自動で判別する）
        if !language.isEmpty {
            transcriptionConfig["language_codes"] = [Self.geminiLanguageCode(language)]
        }
        // ユーザー辞書（固有名詞）はモデルへのヒントとして渡せる
        let vocabulary = Self.customVocabulary(from: prompt)
        if !vocabulary.isEmpty {
            transcriptionConfig["custom_vocabulary"] = vocabulary
        }

        let body: [String: Any] = [
            "model": restModel,
            "input": [[
                "type": "audio",
                "data": audio.data.base64EncodedString(),
                "mime_type": audio.contentType,
            ]],
            "generation_config": ["transcription_config": transcriptionConfig],
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        return request
    }

    /// Interactions API のリビジョン（Google のドキュメントが指定している固定値）
    private static let geminiApiRevision = "2026-05-20"

    /// 設定の言語コード（"ja"）を Gemini が期待する BCP-47（"ja-JP"）へ寄せる。
    /// 既に地域付き（"zh-Hans" など）ならそのまま使う。
    static func geminiLanguageCode(_ language: String) -> String {
        let code = language.trimmingCharacters(in: .whitespacesAndNewlines)
        // 空（自動判定）と、既に地域・スクリプトが付いているものはそのまま
        guard !code.isEmpty, !code.contains("-") else { return code }
        switch code {
        case "ja": return "ja-JP"
        case "en": return "en-US"
        case "ko": return "ko-KR"
        case "zh": return "zh-CN"
        default: return code
        }
    }

    /// ユーザー設定のプロンプトを固有名詞ヒントの配列にする。
    /// Whisper 系は文章のプロンプトを受けるが、Gemini は語のリストを受けるため、
    /// 読点・改行区切りの語だけを取り出して渡す（空なら何も送らない）。
    static func customVocabulary(from prompt: String) -> [String] {
        prompt
            .components(separatedBy: CharacterSet(charactersIn: ",、\n"))
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    // MARK: - 応答解析（バックエンド別）

    private struct ElevenLabsResponse: Decodable {
        let text: String
    }

    /// Interactions API の応答（`steps[].content[]` の text 要素をつないだものが本文）
    private struct GeminiResponse: Decodable {
        struct Content: Decodable {
            let type: String
            let text: String?
        }
        struct Step: Decodable {
            let type: String?
            let content: [Content]?
        }
        let steps: [Step]?
    }

    /// Gemini の応答から文字起こしテキストを取り出す（純関数・テスト対象）。
    /// 応答は step が複数返ることがあるので、text 要素をすべて連結する。
    static func parseGeminiText(_ data: Data) -> String? {
        guard let parsed = try? JSONDecoder().decode(GeminiResponse.self, from: data),
              let steps = parsed.steps else { return nil }
        let text = steps
            .flatMap { $0.content ?? [] }
            .filter { $0.type == "text" }
            .compactMap { $0.text }
            .joined()
        return text.isEmpty ? nil : text
    }

    private struct DeepgramResponse: Decodable {
        struct Alternative: Decodable { let transcript: String }
        struct Channel: Decodable { let alternatives: [Alternative] }
        struct Results: Decodable { let channels: [Channel] }
        let results: Results
    }

    private func parseResponse(_ data: Data) throws -> String {
        switch backend {
        case .openai, .openaiLive, .groq, .appleLocal:
            // response_format=text のためプレーンテキストがそのまま返る
            return String(data: data, encoding: .utf8) ?? ""
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
        case .gemini:
            guard let text = Self.parseGeminiText(data) else {
                throw TranscriptionError(message: "\(backend.label) API の応答を解析できませんでした")
            }
            return text
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
