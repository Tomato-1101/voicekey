//
//  StreamingTranscriber.swift
//  Deepgram リアルタイム文字起こし（WebSocket ストリーミング）
//
//  録音中の 16kHz PCM チャンクを WebSocket で逐次送り、暫定（interim）と
//  確定（is_final）のテキストをリアルタイムに受け取る。HUD のライブ字幕に使う。
//  ベンチマークで Deepgram nova-3 が「離した瞬間にほぼ確定（60〜170ms）」かつ
//  最高精度だったため、ストリーミングの既定バックエンドとして採用している。
//
//  外部依存を増やさないため URLSessionWebSocketTask（標準 API）を使う。
//  送信は audio スレッド、受信は URLSession のキュー、開始/終了はメインから
//  呼ばれるため、共有状態は NSLock で保護する。
//

import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "stream")

/// Deepgram WebSocket による逐次文字起こしセッション（1 録音 = 1 インスタンス）
final class StreamingTranscriber: @unchecked Sendable {

    /// 現在の全文（確定 + 暫定）の更新通知。HUD 用にメインへホップして使う想定
    var onInterim: ((String) -> Void)?

    private let model: String
    private let language: String
    private let session: URLSession
    private var task: URLSessionWebSocketTask?

    private let lock = NSLock()
    /// 確定済みセグメント（is_final=true の順次追記）
    private var finals: [String] = []
    /// 直近の暫定セグメント（次の確定で置き換わる）
    private var interim: String = ""
    /// finish() を解決する継続。二重 resume を防ぐため lock 下で nil 化する
    private var finishContinuation: CheckedContinuation<Void, Never>?
    /// 接続が閉じた / 確定し終えたか
    private var done = false

    init(model: String, language: String) {
        self.model = model
        self.language = language
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 30
        self.session = URLSession(configuration: cfg)
    }

    /// Deepgram の言語パラメータ（REST 側 Transcriber と同じ規則）。
    /// nova-3 は日本語の単言語指定に未対応のため多言語モードを使う。
    private var streamLanguage: String {
        if model.hasPrefix("nova-3") { return "multi" }
        return language.isEmpty ? "ja" : language
    }

    /// WebSocket を開いて受信ループを開始する。
    /// - Returns: キー未設定など開始できない場合 false（呼び出し側は REST にフォールバック）
    func start() -> Bool {
        guard let key = Keychain.apiKey(for: .deepgram) else {
            log.error("Deepgram キーが未設定のためストリーミングを開始できません")
            return false
        }
        var comps = URLComponents(string: "wss://api.deepgram.com/v1/listen")!
        comps.queryItems = [
            URLQueryItem(name: "model", value: model),
            URLQueryItem(name: "language", value: streamLanguage),
            URLQueryItem(name: "encoding", value: "linear16"),
            URLQueryItem(name: "sample_rate", value: "16000"),
            URLQueryItem(name: "channels", value: "1"),
            URLQueryItem(name: "interim_results", value: "true"),
            URLQueryItem(name: "punctuate", value: "true"),
            URLQueryItem(name: "smart_format", value: "true"),
        ]
        var request = URLRequest(url: comps.url!)
        request.setValue("Token \(key)", forHTTPHeaderField: "Authorization")

        let task = session.webSocketTask(with: request)
        self.task = task
        task.resume()
        receiveLoop()
        log.info("Deepgram ストリーミング開始 (model=\(self.model, privacy: .public), lang=\(self.streamLanguage, privacy: .public))")
        return true
    }

    /// 16kHz モノラル Float32 チャンクを送信する（audio スレッドから呼ばれる）
    func send(_ samples: [Float]) {
        guard let task, !samples.isEmpty else { return }
        // Float32[-1,1] → Int16 LE 生バイト（範囲外はクリップして轟音化を防ぐ）
        var pcm = Data(capacity: samples.count * 2)
        for s in samples {
            let clipped = max(-1.0, min(1.0, s))
            var v = Int16(clipped * 32767).littleEndian
            withUnsafeBytes(of: &v) { pcm.append(contentsOf: $0) }
        }
        task.send(.data(pcm)) { error in
            if let error { log.debug("送信エラー: \(error.localizedDescription)") }
        }
    }

    /// 送信を打ち切り、確定テキストを返す（ホットキーを離したときに呼ぶ）。
    /// CloseStream を送ると Deepgram は残りを確定して Metadata を返し接続を閉じる。
    /// 取りこぼし対策で最大 3 秒だけ待つ。
    func finish() async -> String {
        // CloseStream で残りバッファの確定を促す
        task?.send(.string("{\"type\":\"CloseStream\"}")) { _ in }

        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            lock.lock()
            if done {
                lock.unlock()
                cont.resume()
                return
            }
            finishContinuation = cont
            lock.unlock()
            // 確定が来ない場合の保険（接続ハング対策）
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) { [weak self] in
                self?.resolveFinish()
            }
        }
        task?.cancel(with: .normalClosure, reason: nil)
        // 録音のたびに生成するセッションは明示的に破棄する（放置すると漸増リーク）
        session.finishTasksAndInvalidate()
        return Self.stripCJKSpaces(currentText())
    }

    /// 結果を使わずに接続を破棄する（録音破棄時など）
    func cancel() {
        task?.cancel(with: .normalClosure, reason: nil)
        session.finishTasksAndInvalidate()
        resolveFinish()
    }

    /// 確定 + 暫定を結合した現在の全文（生・スペース除去前）
    private func currentText() -> String {
        lock.lock(); defer { lock.unlock() }
        return (finals + (interim.isEmpty ? [] : [interim])).joined()
    }

    // MARK: - 受信

    private func receiveLoop() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                // 切断・エラーは finish() を解決して REST 側の判断に委ねる
                self.resolveFinish()
            case .success(let message):
                switch message {
                case .string(let text): self.handle(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) { self.handle(text) }
                @unknown default: break
                }
                self.receiveLoop()
            }
        }
    }

    /// Deepgram のストリーミング応答（必要なフィールドのみ）
    private struct DGMessage: Decodable {
        let type: String?
        let is_final: Bool?
        let channel: Channel?
        struct Channel: Decodable { let alternatives: [Alternative] }
        struct Alternative: Decodable { let transcript: String }
    }

    private func handle(_ json: String) {
        guard let data = json.data(using: .utf8),
              let msg = try? JSONDecoder().decode(DGMessage.self, from: data) else { return }

        // Metadata は CloseStream 後の終端通知 → 確定完了とみなす
        if msg.type == "Metadata" {
            resolveFinish()
            return
        }

        guard let transcript = msg.channel?.alternatives.first?.transcript else { return }
        let trimmed = transcript.trimmingCharacters(in: .whitespacesAndNewlines)

        lock.lock()
        if msg.is_final == true {
            if !trimmed.isEmpty { finals.append(trimmed) }
            interim = ""
        } else {
            interim = trimmed
        }
        let snapshot = (finals + (interim.isEmpty ? [] : [interim])).joined()
        lock.unlock()

        // HUD へは日本語スペースを除去した見た目で渡す
        onInterim?(Self.stripCJKSpaces(snapshot))
    }

    /// finish() の継続を一度だけ解決する（接続終了 / Metadata / タイムアウトのいずれか）
    private func resolveFinish() {
        lock.lock()
        let cont = finishContinuation
        finishContinuation = nil
        done = true
        lock.unlock()
        cont?.resume()
    }

    // MARK: - 日本語スペース除去

    /// Deepgram は日本語出力に単語間スペースを挿入するため、
    /// 「前後どちらかが CJK 文字」のスペースだけを除去する。
    /// （英単語間のスペースは残すので "GPT 4" などは壊さない）
    static func stripCJKSpaces(_ s: String) -> String {
        let chars = Array(s)
        var out = String()
        out.reserveCapacity(chars.count)
        for (i, c) in chars.enumerated() {
            if c == " " {
                let prev = i > 0 ? chars[i - 1] : nil
                let next = i + 1 < chars.count ? chars[i + 1] : nil
                if (prev.map(isCJK) ?? false) || (next.map(isCJK) ?? false) {
                    continue  // CJK 隣接のスペースは除去
                }
            }
            out.append(c)
        }
        return out
    }

    /// ひらがな・カタカナ・漢字（および全角記号の一部）判定
    private static func isCJK(_ c: Character) -> Bool {
        for scalar in c.unicodeScalars {
            let v = scalar.value
            if (0x3040...0x30FF).contains(v)       // ひらがな・カタカナ
                || (0x3400...0x4DBF).contains(v)   // CJK 拡張A
                || (0x4E00...0x9FFF).contains(v)   // CJK 統合漢字
                || (0xF900...0xFAFF).contains(v)   // CJK 互換漢字
                || (0xFF00...0xFFEF).contains(v) { // 全角・半角形
                return true
            }
        }
        return false
    }
}
