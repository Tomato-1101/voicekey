//
//  Transcriber.swift
//  OpenAI / Groq Audio Transcriptions API クライアント
//
//  両 API は OpenAI 互換の同一 REST 形式のため、1 つの実装で共用する。
//  multipart/form-data で 16kHz モノラル WAV を送信する。
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

/// OpenAI 互換 Audio Transcriptions API のクライアント
final class Transcriber {

    let backend: Backend
    var model: String
    var language: String
    var prompt: String

    /// 接続を再利用するためバックエンドごとに URLSession を保持
    private let session: URLSession

    private var baseURL: URL {
        switch backend {
        case .openai: return URL(string: "https://api.openai.com/v1")!
        case .groq: return URL(string: "https://api.groq.com/openai/v1")!
        }
    }

    init(backend: Backend, model: String, language: String, prompt: String) {
        self.backend = backend
        self.model = model
        self.language = language
        self.prompt = prompt

        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 20
        config.timeoutIntervalForResource = 60
        config.httpMaximumConnectionsPerHost = 4
        self.session = URLSession(configuration: config)
    }

    /// TLS 接続を事前確立して初回リクエストの往復を短縮する（録音開始時に呼ぶ）。
    /// 失敗しても文字起こしには影響しないため、結果は無視する。
    func prewarm() {
        guard let apiKey = Keychain.apiKey(for: backend) else { return }
        var request = URLRequest(url: baseURL.appendingPathComponent("models"))
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 5
        session.dataTask(with: request) { _, _, _ in }.resume()
    }

    /// 音声を文字起こしする
    /// - Parameter samples: 音声データ（Float32, 16kHz, モノラル）
    /// - Returns: 文字起こし結果（前後空白除去済み）
    func transcribe(samples: [Float]) async throws -> String {
        guard !samples.isEmpty else { return "" }
        guard let apiKey = Keychain.apiKey(for: backend) else {
            throw TranscriptionError(
                message: "\(backend.label) の API キーが未設定です（設定画面から保存してください）"
            )
        }

        let wav = WavEncoder.encode(samples)
        let boundary = "voicekey-\(UUID().uuidString)"

        var request = URLRequest(url: baseURL.appendingPathComponent("audio/transcriptions"))
        request.httpMethod = "POST"
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        var body = Data()
        func field(_ name: String, _ value: String) {
            body.append(Data("--\(boundary)\r\n".utf8))
            body.append(Data("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n".utf8))
            body.append(Data("\(value)\r\n".utf8))
        }
        field("model", model)
        field("response_format", "text")
        field("temperature", "0")
        if !language.isEmpty { field("language", language) }
        if !prompt.isEmpty { field("prompt", prompt) }
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data("Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n".utf8))
        body.append(Data("Content-Type: audio/wav\r\n\r\n".utf8))
        body.append(wav)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        request.httpBody = body

        let start = Date()
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

        let text = (String(data: data, encoding: .utf8) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let elapsed = Int(Date().timeIntervalSince(start) * 1000)
        log.info("\(self.backend.label, privacy: .public) 文字起こし完了: \(elapsed)ms, \(text.count) 文字")
        return text
    }
}
