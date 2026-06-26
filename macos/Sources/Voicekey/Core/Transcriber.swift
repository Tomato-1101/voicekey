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

    let backend: Backend

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

    /// 接続を再利用するためバックエンドごとに URLSession を保持
    private let session: URLSession

    private var baseURL: URL {
        switch backend {
        case .openai: return URL(string: "https://api.openai.com/v1")!
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
        guard let apiKey = Keychain.apiKey(for: backend) else { return }
        // 軽量な GET エンドポイントにアクセスして接続だけ確立する
        let path: String
        switch backend {
        case .openai, .groq, .elevenlabs: path = "models"
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
        case .openai, .groq:
            request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        case .elevenlabs:
            request.setValue(apiKey, forHTTPHeaderField: "xi-api-key")
        case .deepgram:
            request.setValue("Token \(apiKey)", forHTTPHeaderField: "Authorization")
        }
    }

    /// 音声を文字起こしする
    /// - Parameter samples: 音声データ（Float32, 16kHz, モノラル）
    /// - Returns: 文字起こし結果（前後空白除去済み）
    func transcribe(samples: [Float]) async throws -> String {
        guard !samples.isEmpty else { return "" }

        // 配布版（製品版ビルド）は「アクティベーション必須」。埋め込みキーへはフォールバックしない。
        // 未ログイン＝停止して「設定 → アカウント」でログイン＋キー登録を促す。
        // 利用権（アクティベーションキー/サブスク）が無い場合はサーバーが 403 を返し、
        // BackendError.noSubscription（＝キー登録を促すメッセージ）として表面化する。
        if EmbeddedKeys.isDist {
            guard BackendClient.isLoggedIn else {
                throw TranscriptionError(
                    message: "利用するにはログインとアクティベーションキーが必要です（設定 → アカウント）"
                )
            }
            switch backend {
            case .elevenlabs: return try await transcribeElevenLabsViaProxy(samples: samples)
            case .deepgram: return try await transcribeDeepgramViaJWT(samples: samples)
            case .openai, .groq:
                throw TranscriptionError(message: "この文字起こし方式は配布版では利用できません")
            }
        }

        // 開発ビルド: 従来の並存ガード（ログイン時はサーバー経路、未ログインは設定キー直叩き）。
        // 高速リアルタイム=Deepgram は短命 JWT で直叩き（低レイテンシ核心を維持）、
        // 正確性=ElevenLabs はサーバープロキシ経由（バッチは短命キー非対応）。
        // OpenAI/Groq は製品版の文字起こし選択肢に無いため従来の直叩きに委ねる。
        if BackendClient.isLoggedIn {
            switch backend {
            case .elevenlabs: return try await transcribeElevenLabsViaProxy(samples: samples)
            case .deepgram: return try await transcribeDeepgramViaJWT(samples: samples)
            case .openai, .groq: break
            }
        }

        guard let apiKey = Keychain.apiKey(for: backend) else {
            throw TranscriptionError(
                message: "\(backend.label) の API キーが未設定です（設定画面から保存してください）"
            )
        }

        let request = buildRequest(audio: encodeAudio(samples), apiKey: apiKey)
        let start = Date()
        let data = try await send(request)
        let text = try parseResponse(data)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let elapsed = Int(Date().timeIntervalSince(start) * 1000)
        log.info("\(self.backend.label, privacy: .public) 文字起こし完了: \(elapsed)ms, \(text.count) 文字")
        return text
    }

    // MARK: - 製品版サーバー経路（段階3）

    /// 正確性（ElevenLabs）: サーバープロキシ経由で文字起こしする。
    /// サーバー契約は WAV を期待するため FLAC ではなく WAV を送る。
    private func transcribeElevenLabsViaProxy(samples: [Float]) async throws -> String {
        let start = Date()
        do {
            let text = try await BackendClient.transcribeElevenLabs(
                wav: WavEncoder.encode(samples), language: language
            ).trimmingCharacters(in: .whitespacesAndNewlines)
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
        let text = try parseResponse(data)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let elapsed = Int(Date().timeIntervalSince(start) * 1000)
        log.info("\(self.backend.label, privacy: .public) 文字起こし完了: \(elapsed)ms, \(text.count) 文字")
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
        case .openai, .groq:
            return openAIRequest(audio: audio, apiKey: apiKey)
        case .elevenlabs:
            return elevenLabsRequest(audio: audio, apiKey: apiKey)
        case .deepgram:
            return deepgramRequest(audio: audio, apiKey: apiKey)
        }
    }

    /// OpenAI 互換 Audio Transcriptions API（OpenAI / Groq 共用）
    private func openAIRequest(audio: EncodedAudio, apiKey: String) -> URLRequest {
        var request = URLRequest(url: baseURL.appendingPathComponent("audio/transcriptions"))
        request.httpMethod = "POST"
        setAuth(apiKey, on: &request)

        var form = MultipartForm()
        form.field("model", model)
        form.field("response_format", "text")
        form.field("temperature", "0")
        if !language.isEmpty { form.field("language", language) }
        if !prompt.isEmpty { form.field("prompt", prompt) }
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
        } else if model.hasPrefix("nova-3") && language != "en" {
            // nova-3 は英語以外の単言語指定に未対応のため多言語モードを使う
            query.append(URLQueryItem(name: "language", value: "multi"))
        } else {
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

    // MARK: - 応答解析（バックエンド別）

    private struct ElevenLabsResponse: Decodable {
        let text: String
    }

    private struct DeepgramResponse: Decodable {
        struct Alternative: Decodable { let transcript: String }
        struct Channel: Decodable { let alternatives: [Alternative] }
        struct Results: Decodable { let channels: [Channel] }
        let results: Results
    }

    private func parseResponse(_ data: Data) throws -> String {
        switch backend {
        case .openai, .groq:
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
