//
//  OpenAILiveTranscriber.swift
//  OpenAI Realtime のライブ文字起こし（gpt-live-transcribe・WebSocket ストリーミング）
//
//  Deepgram 版（StreamingTranscriber）と同じ契約で、録音中の 16kHz PCM チャンクを
//  逐次送り、暫定（delta）と確定（completed）のテキストを受け取る。
//  personal（自分用）ブランチ限定の選択肢で、Keychain の OpenAI キーで直結する。
//
//  Deepgram との実装差（この 3 点のためにクラスを分けている）:
//  - 音声は生バイナリではなく base64 を JSON に載せて送る（input_audio_buffer.append）
//  - OpenAI は 24kHz 以上の PCM しか受けないので 16kHz を 1.5 倍にリサンプルする
//  - 終端は CloseStream ではなく input_audio_buffer.commit（turn_detection=null の手動確定）
//
//  実測（2026-07-31・benchmark/delay_sweep.py gpt-live-transcribe）:
//    delay=minimal が最速で TTFB 449-524ms・確定 649-708ms・CER 2.7/3.1%。
//    Deepgram nova-3（確定 69-75ms）には及ばないので既定は Deepgram のまま。
//

import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "stream")

/// ライブ（WebSocket）文字起こしセッションの共通契約。
/// Deepgram（StreamingTranscriber）と OpenAI（OpenAILiveTranscriber）を
/// AppController から同じ扱いで差し替えるために切っている。
protocol LiveTranscribing: AnyObject {
    /// 現在の全文（確定 + 暫定）の更新通知。HUD のライブ字幕用
    var onInterim: ((String) -> Void)? { get set }
    /// 接続を開始する。開始できなければ false（呼び出し側は REST へフォールバック）
    func start() -> Bool
    /// 16kHz モノラル Float32 チャンクを送る（audio スレッドから呼ばれる）
    func send(_ samples: [Float])
    /// 送信を打ち切り、確定テキストを返す（ホットキーを離したときに呼ぶ）
    func finish() async -> String
    /// 結果を使わずに接続を破棄する
    func cancel()
}

/// OpenAI Realtime WebSocket による逐次文字起こしセッション（1 録音 = 1 インスタンス）
final class OpenAILiveTranscriber: LiveTranscribing, @unchecked Sendable {

    var onInterim: ((String) -> Void)?

    /// OpenAI Realtime が受け付ける最小サンプルレート（16kHz は拒否されるため 24kHz へ上げて送る）
    private static let outputRate = 24000
    /// 録音側のサンプルレート
    private static let inputRate = 16000
    /// 遅延/精度のトレードオフ。実測で minimal が TTFB 最速（449ms）かつ確定も最速級のため固定。
    private static let delay = "minimal"

    private let model: String
    private let language: String
    private let session: URLSession
    private var task: URLSessionWebSocketTask?

    private let lock = NSLock()
    /// delta の累積（確定が来たら completed の全文で置き換える）
    private var deltas: [String] = []
    /// 確定テキスト（completed の transcript）
    private var finalText: String = ""
    /// 接続確立前に届いた PCM の退避（接続時に順序を保ってフラッシュする）
    private var pending: [Data] = []
    private var cancelled = false
    /// finish() が接続確立前に呼ばれた（接続確立後に pending フラッシュ後 commit を送る）
    private var closeRequested = false
    private var finishContinuation: CheckedContinuation<Void, Never>?
    private var done = false
    private let createdAt = Date()
    private var firstResultLogged = false

    // MARK: - リサンプル状態（audio スレッドからのみ触る）

    /// 出力位置（[prevSample] + samples の連結配列上の座標）。チャンクをまたいで持ち越し、
    /// 境界で波形が途切れない（＝クリック音で認識精度を落とさない）ようにする。
    private var resamplePos: Double = 1.0
    /// 直前チャンクの末尾サンプル（連結配列の index 0 に置く）
    private var prevSample: Float = 0

    init(model: String, language: String) {
        self.model = model
        self.language = language
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 30
        self.session = URLSession(configuration: cfg)
    }

    /// 文字起こしの言語ヒント（REST 側 Transcriber と同じ規則で既定 ja）
    private var streamLanguage: String {
        return language.isEmpty ? "ja" : language
    }

    /// WebSocket を開いて受信ループを開始する。
    /// personal / 未ログイン開発とも Keychain の OpenAI キーで直結する（サーバー往復ゼロ）。
    /// - Returns: キー未設定・配布版など開始できない場合 false（呼び出し側は REST にフォールバック）
    func start() -> Bool {
        // 配布版（製品版）は openaiLive を提供しない（personal 限定の選択肢）。
        guard !EmbeddedKeys.isDist else {
            log.error("OpenAI ライブ: 配布版では利用できません")
            return false
        }
        guard let key = Keychain.apiKey(for: .openaiLive) else {
            log.error("OpenAI ライブ: Keychain に OpenAI キーが未設定です（設定画面から保存してください）")
            return false
        }
        return start(apiKey: key)
    }

    /// キーを明示して開始する。実接続の疎通ハーネス（OpenAILiveE2ETests）が
    /// 実 Keychain に触れずに検証するための入口で、通常経路は start() を使う。
    @discardableResult
    func start(apiKey: String) -> Bool {
        guard !apiKey.isEmpty else { return false }
        connect(key: apiKey)
        return true
    }

    /// WebSocket を開き、セッション設定 → 退避 PCM のフラッシュ → 受信開始まで行う。
    private func connect(key: String) {
        var request = URLRequest(url: URL(string: "wss://api.openai.com/v1/realtime?intent=transcription")!)
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")

        let task = session.webSocketTask(with: request)

        lock.lock()
        if cancelled || done {
            lock.unlock()
            task.cancel(with: .normalClosure, reason: nil)
            resolveFinish(reason: "cancelled")
            return
        }
        self.task = task
        let buffered = pending
        pending = []
        let closeAfterFlush = closeRequested
        lock.unlock()

        task.resume()
        // セッション設定は音声より先に届く必要がある（turn_detection=null＝確定は手動 commit のみ）
        task.send(.string(sessionUpdateJSON())) { error in
            if let error { log.error("OpenAI ライブ: session.update 送信エラー: \(error.localizedDescription)") }
        }
        for chunk in buffered {
            task.send(.string(appendJSON(chunk))) { error in
                if let error { log.debug("退避 PCM 送信エラー: \(error.localizedDescription)") }
            }
        }
        // finish-before-connect: 退避を送り切ってから commit＝残りを確定させる
        if closeAfterFlush {
            task.send(.string("{\"type\":\"input_audio_buffer.commit\"}")) { _ in }
        }
        receiveLoop()
        log.notice("OpenAI ライブ開始 (model=\(self.model, privacy: .public), lang=\(self.streamLanguage, privacy: .public), delay=\(Self.delay, privacy: .public))")
    }

    /// セッション設定 JSON（transcription セッション・PCM 24kHz・手動 commit）
    private func sessionUpdateJSON() -> String {
        let payload: [String: Any] = [
            "type": "session.update",
            "session": [
                "type": "transcription",
                "audio": [
                    "input": [
                        "format": ["type": "audio/pcm", "rate": Self.outputRate],
                        "transcription": [
                            "model": model,
                            "language": streamLanguage,
                            "delay": Self.delay,
                        ],
                        // null＝サーバー VAD を使わず、commit したときだけ確定させる
                        // （押している間が 1 ターン＝ホットキー操作と一致する）
                        "turn_detection": NSNull(),
                    ]
                ],
            ],
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let json = String(data: data, encoding: .utf8) else { return "" }
        return json
    }

    /// 音声チャンク 1 個の append JSON（base64 で載せる）
    private func appendJSON(_ pcm: Data) -> String {
        // 手組みするのは JSONSerialization のオーバーヘッドを避けるため（チャンクごとに走る）。
        // base64 は JSON で安全な文字集合なのでエスケープは不要。
        return "{\"type\":\"input_audio_buffer.append\",\"audio\":\"\(pcm.base64EncodedString())\"}"
    }

    /// 16kHz モノラル Float32 チャンクを送信する（audio スレッドから呼ばれる）。
    /// 24kHz へリサンプル → Int16 LE → base64 の順に変換して JSON で送る。
    func send(_ samples: [Float]) {
        guard !samples.isEmpty else { return }
        let upsampled = upsample(samples)
        guard !upsampled.isEmpty else { return }

        var pcm = Data(capacity: upsampled.count * 2)
        for s in upsampled {
            let clipped = max(-1.0, min(1.0, s))
            var v = Int16(clipped * 32767).littleEndian
            withUnsafeBytes(of: &v) { pcm.append(contentsOf: $0) }
        }

        lock.lock()
        if cancelled || done {
            lock.unlock()
            return
        }
        guard let task else {
            pending.append(pcm)
            lock.unlock()
            return
        }
        lock.unlock()
        task.send(.string(appendJSON(pcm))) { error in
            if let error { log.debug("送信エラー: \(error.localizedDescription)") }
        }
    }

    /// 16kHz → 24kHz の線形補間リサンプル（audio スレッドからのみ呼ばれる＝ロック不要）。
    /// 前チャンク末尾のサンプルと出力位置を持ち越すことで、チャンク境界に段差
    /// （＝クリック音になり認識精度を落とす）を作らない。
    /// 境界の連続性は OpenAILiveResampleTests で検証している（internal はそのため）。
    func upsample(_ samples: [Float]) -> [Float] {
        let step = Double(Self.inputRate) / Double(Self.outputRate)  // 16000/24000 = 2/3
        // index 0 = 前チャンク末尾、index 1... = 今回のチャンク
        let buf = [prevSample] + samples
        var out: [Float] = []
        out.reserveCapacity(samples.count * 3 / 2 + 2)
        var pos = resamplePos
        // 補間には buf[i] と buf[i+1] が要るので、右端 buf[count-1] は次回の左端として残す
        let limit = Double(buf.count - 1)
        while pos < limit {
            let i = Int(pos)
            let frac = Float(pos - Double(i))
            out.append(buf[i] * (1 - frac) + buf[i + 1] * frac)
            pos += step
        }
        // 次チャンクの連結配列（index 0 = 今回の末尾）での座標に読み替える
        resamplePos = pos - Double(samples.count)
        prevSample = samples[samples.count - 1]
        return out
    }

    /// 送信を打ち切り、確定テキストを返す（ホットキーを離したときに呼ぶ）。
    /// 接続済みなら commit を送って確定を最大 3 秒待つ。未接続なら close 要求を立て、
    /// 接続確立後に connect() が pending フラッシュ後 commit を送る。
    /// 未接続かつ音声ゼロなら確定は永遠に来ないので即空解決する。
    func finish() async -> String {
        let (connectedTask, immediateEmpty) = requestClose()
        connectedTask?.send(.string("{\"type\":\"input_audio_buffer.commit\"}")) { _ in }

        if immediateEmpty {
            resolveFinish(reason: "empty-noconnect")
        } else {
            await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
                lock.lock()
                if done {
                    lock.unlock()
                    cont.resume()
                    return
                }
                finishContinuation = cont
                lock.unlock()
                // 確定が来ない場合の最終防衛（接続ハング・commit 未達対策）。
                // 実測の確定は 649-708ms なので 3 秒あれば通常は取りこぼさない。
                DispatchQueue.global().asyncAfter(deadline: .now() + 3) { [weak self] in
                    self?.resolveFinish(reason: "timeout")
                }
            }
        }
        markCancelled()?.cancel(with: .normalClosure, reason: nil)
        session.finishTasksAndInvalidate()
        return TextNormalize.stripCJKSpaces(currentText())
    }

    /// finish() の入口: close 要求を立て、(接続済み task, 即空解決すべきか) を lock 下で判定する。
    private func requestClose() -> (task: URLSessionWebSocketTask?, immediateEmpty: Bool) {
        lock.lock(); defer { lock.unlock() }
        closeRequested = true
        if let task { return (task, false) }
        return (nil, pending.isEmpty)
    }

    /// finish/cancel 確定: cancelled を立て、その時点の task を返す
    @discardableResult
    private func markCancelled() -> URLSessionWebSocketTask? {
        lock.lock(); defer { lock.unlock() }
        cancelled = true
        return task
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let t = task
        lock.unlock()
        t?.cancel(with: .normalClosure, reason: nil)
        session.finishTasksAndInvalidate()
        resolveFinish(reason: "cancelled")
    }

    /// 現在の全文（確定があればそれ、無ければ delta の累積）
    private func currentText() -> String {
        lock.lock(); defer { lock.unlock() }
        return finalText.isEmpty ? deltas.joined() : finalText
    }

    // MARK: - 受信

    private func receiveLoop() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                self.resolveFinish(reason: "disconnect")
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

    /// Realtime のイベント（必要なフィールドのみ）
    private struct RTMessage: Decodable {
        let type: String?
        let delta: String?
        let transcript: String?
        let error: ErrorBody?
        struct ErrorBody: Decodable { let message: String? }
    }

    private func handle(_ json: String) {
        guard let data = json.data(using: .utf8),
              let msg = try? JSONDecoder().decode(RTMessage.self, from: data),
              let type = msg.type else { return }

        if type == "error" {
            log.error("OpenAI ライブ: \(msg.error?.message ?? "不明なエラー", privacy: .public)")
            resolveFinish(reason: "error")
            return
        }

        // 途中経過（delta）と確定（completed）。イベント名は
        // conversation.item.input_audio_transcription.{delta,completed}
        if type.hasSuffix("transcription.delta") {
            guard let delta = msg.delta, !delta.isEmpty else { return }
            logFirstResultOnce()
            lock.lock()
            deltas.append(delta)
            let snapshot = deltas.joined()
            lock.unlock()
            onInterim?(TextNormalize.stripCJKSpaces(snapshot))
            return
        }

        if type.hasSuffix("transcription.completed") {
            let transcript = (msg.transcript ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !transcript.isEmpty {
                logFirstResultOnce()
                lock.lock()
                finalText = transcript
                lock.unlock()
                onInterim?(TextNormalize.stripCJKSpaces(transcript))
            }
            // 手動 commit なので completed = このターンの確定＝終端
            resolveFinish(reason: "completed")
        }
    }

    /// 録音開始から「最初の文字が出るまで」の体感遅延を 1 度だけログ（Deepgram 側と対称）
    private func logFirstResultOnce() {
        lock.lock()
        let alreadyLogged = firstResultLogged
        firstResultLogged = true
        lock.unlock()
        if !alreadyLogged {
            log.notice("OpenAI ライブ 最初の文字まで \(Int(Date().timeIntervalSince(self.createdAt) * 1000), privacy: .public)ms（録音開始から）")
        }
    }

    /// finish() の継続を一度だけ解決する（completed / 切断 / エラー / タイムアウトのいずれか）
    private func resolveFinish(reason: String = "unknown") {
        lock.lock()
        let cont = finishContinuation
        finishContinuation = nil
        let wasDone = done
        done = true
        lock.unlock()
        if !wasDone {
            log.notice("OpenAI ライブ finish 解決: \(reason, privacy: .public)")
        }
        cont?.resume()
    }
}
