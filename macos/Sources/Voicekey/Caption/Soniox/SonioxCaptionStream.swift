/// ライブ字幕用の Soniox リアルタイム接続（認識と翻訳を 1 本の WebSocket で行う）
///
/// 2026-10-12 ユーザー指示「ライブ字幕は遅い。精度が落ちてもいいから速さ重視。リアルタイムがいい」
/// 「エンジンは全部選べ」で追加した字幕エンジン。端末内の認識（確定まで中央値 4.3 秒）を待たず、
/// Soniox の途中結果と訳をそのまま字幕に流す。**課金あり**（ストリームを開いている時間ぶん）なので
/// 既定にはせず、ユーザーが選んだときだけ使う。
///
/// - 音声は CapturePipeline が 16kHz モノラル Float32 に変換したものを受け取り、pcm_s16le で送る。
/// - 接続先（JP/US）とキーは音声入力の Soniox と同じもの（`SonioxRegion` / `Keychain`）を使う。
///   キーの値はログにも画面にも出さない。
/// - 切断したら上限付きの指数バックオフで張り直す（`SonioxReconnectPolicy`）。上限を超えたら
///   `onGiveUp` で知らせ、呼び出し側が Apple へ切り替える。HAL には一切触れない（音声の取り込みは
///   CapturePipeline のまま）。
import AVFoundation
import Foundation
import OSLog

/// 再接続の方針（純ロジック・単体テスト用に分けている）
struct SonioxReconnectPolicy {

    /// 連続で失敗してよい回数（これを超えたら諦める）
    static let maxAttempts = 5
    /// 最初の待ち時間（秒）。以後 2 倍ずつ
    static let baseDelay: TimeInterval = 0.5
    /// 待ち時間の上限（秒）
    static let maxDelay: TimeInterval = 8
    /// これだけ安定して受信できたら、失敗の回数を数え直す（秒）
    static let healthyDuration: TimeInterval = 30

    /// 直近の連続失敗回数
    private(set) var failures = 0

    /// 失敗を 1 回数え、次に張り直すまでの待ち時間を返す
    ///
    /// - Parameter isFatal: 認証エラーなど、張り直しても直らない失敗か
    /// - Returns: 待ち時間。もう諦めるなら nil
    mutating func nextDelay(isFatal: Bool) -> TimeInterval? {
        guard !isFatal else { return nil }
        failures += 1
        guard failures <= Self.maxAttempts else { return nil }
        return min(Self.baseDelay * pow(2, Double(failures - 1)), Self.maxDelay)
    }

    /// 接続が安定したことを伝える（失敗の回数を戻す）
    mutating func noteHealthy() {
        failures = 0
    }

    /// 張り直しても直らないエラーか
    ///
    /// 401（キーが無効）・402（残高不足）・403（権限なし）は何度つないでも同じなので即諦める。
    static func isFatal(errorCode: Int?) -> Bool {
        guard let errorCode else { return false }
        return [400, 401, 402, 403].contains(errorCode)
    }
}

/// ライブ字幕用の Soniox ストリーム
@available(macOS 26.0, *)
final class SonioxCaptionStream: @unchecked Sendable {

    /// 字幕で使うモデル（リアルタイム用）
    static let model = "stt-rt-v5"
    /// 送る音声のサンプルレート（CapturePipeline の外部向け形式と同じ）
    private static let sampleRate = 16000
    /// エンドポイント検出の最大待ち（ミリ秒）
    ///
    /// Soniox の既定は 2000ms。字幕は速さ重視なので許される最小の 500ms にする
    /// （話し終わりから行の確定までの待ちがそのまま字幕の遅れになるため）。
    private static let maxEndpointDelayMs = 500
    /// 音声を送らない間のキープアライブ間隔（秒）
    ///
    /// 20 秒以上なにも送らないと切断されるので、余裕を見て 10 秒ごとに送る。
    /// タップは対象アプリが黙っている間フレームを配らないため、黙っている時間は普通にある。
    private static let keepaliveInterval: TimeInterval = 10
    /// 切れ目が来てから訳を待つ上限（秒）。過ぎたら訳の有無にかかわらず行を確定する
    private static let translationWaitLimit: TimeInterval = 2
    /// 接続前に溜めておく音声の上限（バイト。16kHz・Int16 で約 3 秒）
    private static let maxPendingBytes = sampleRate * 2 * 3

    private let logger = makeCaptionLogger("SonioxCaption")
    /// 接続をまたいで使い回す（作っては捨てると CFNetwork 側に残骸が溜まるため。音声入力の Soniox と同じ作法）
    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30
        return URLSession(configuration: config)
    }()

    private let translates: Bool
    private let language: String
    private let queue = DispatchQueue(label: "com.voicekey.caption.soniox")

    // 以下はすべて queue 上で触る
    private var apiKey: String?
    private var endpoint: URL?
    private var task: URLSessionWebSocketTask?
    /// 接続の世代（古い接続の受信・切断通知を捨てるため）
    private var connection = 0
    private var assembler: SonioxCaptionAssembler
    private var reconnect = SonioxReconnectPolicy()
    private var pending: [Data] = []
    private var pendingBytes = 0
    private var lastSentAt: CFAbsoluteTime = 0
    private var connectedAt: CFAbsoluteTime = 0
    private var keepaliveTimer: DispatchSourceTimer?
    private var translationWaitTimer: DispatchSourceTimer?
    private var reconnectTimer: DispatchSourceTimer?
    private var isStopped = false

    /// ライブ行の更新（原文, 訳）。任意スレッドから呼ぶ
    var onLive: (@Sendable (String, String) -> Void)?
    /// 1 行の確定（原文, 訳）。任意スレッドから呼ぶ
    var onConfirmed: (@Sendable (String, String) -> Void)?
    /// 張り直しを諦めた（ユーザー向けの理由）。任意スレッドから 1 回だけ呼ぶ
    var onGiveUp: (@Sendable (String) -> Void)?

    /// - Parameters:
    ///   - translates: 英→日の翻訳もするか（翻訳モード）。false なら文字起こしだけ
    ///   - language: 認識言語のヒント（"en" / "ja"）
    init(translates: Bool, language: String) {
        self.translates = translates
        self.language = language
        self.assembler = SonioxCaptionAssembler(translates: translates)
    }

    /// 接続直後に送る設定 JSON（テストから組み立てを確かめるため static に出している）
    static func configJSON(apiKey: String, translates: Bool, language: String) -> String {
        var payload: [String: Any] = [
            "api_key": apiKey,
            "model": model,
            "audio_format": "pcm_s16le",
            "sample_rate": sampleRate,
            "num_channels": 1,
            "language_hints": [language],
            "enable_endpoint_detection": true,
            "max_endpoint_delay_ms": maxEndpointDelayMs,
        ]
        if translates {
            payload["translation"] = ["type": "one_way", "target_language": "ja"]
        }
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
              let json = String(data: data, encoding: .utf8) else { return "" }
        return json
    }

    /// 開始する
    ///
    /// - Returns: キー未設定・配布版など、開始できないとき false（呼び出し側は Apple へ回す）
    func start() -> Bool {
        // 配布版には Soniox を提供しない（音声入力の Soniox と同じ扱い）
        guard !EmbeddedKeys.isDist else { return false }
        guard let key = Keychain.apiKey(for: .soniox), !key.isEmpty else { return false }
        queue.async { [self] in
            apiKey = key
            if let region = SonioxRegion.cached(for: key) {
                endpoint = region.websocketURL
                connectLocked()
            } else {
                // 初回だけ JP/US のどちらで通るかを問い合わせる（届いた音声は pending に溜まる）
                Task { [self] in
                    let region = await SonioxRegion.detect(apiKey: key) ?? .us
                    self.queue.async { [self] in
                        guard !isStopped else { return }
                        endpoint = region.websocketURL
                        connectLocked()
                    }
                }
            }
            startKeepaliveLocked()
        }
        return true
    }

    /// 音声を送る（CapturePipeline の IO スレッドから呼ばれる）
    ///
    /// - Parameter buffer: 16kHz モノラル Float32
    func send(_ buffer: AVAudioPCMBuffer) {
        guard let channel = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
        let samples = Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
        let pcm = SonioxLiveTranscriber.pcm16(samples)
        queue.async { [self] in
            guard !isStopped else { return }
            guard let task else {
                // 接続待ち・張り直し中は少しだけ溜める（古い分から捨てる）
                pending.append(pcm)
                pendingBytes += pcm.count
                while pendingBytes > Self.maxPendingBytes, !pending.isEmpty {
                    pendingBytes -= pending.removeFirst().count
                }
                return
            }
            lastSentAt = CFAbsoluteTimeGetCurrent()
            task.send(.data(pcm)) { _ in }
        }
    }

    /// 停止する
    ///
    /// 残っている分（未確定を含む）を 1 行に確定してから閉じる。停止の瞬間に喋っていた
    /// 最後の文を議事録から落とさないため。呼び出し後はコールバックを呼ばない。
    func stop() {
        queue.sync { [self] in
            guard !isStopped else { return }
            if let line = assembler.flush() { emit([line]) }
            isStopped = true
            connection += 1
            keepaliveTimer?.cancel(); keepaliveTimer = nil
            translationWaitTimer?.cancel(); translationWaitTimer = nil
            reconnectTimer?.cancel(); reconnectTimer = nil
            task?.cancel(with: .normalClosure, reason: nil)
            task = nil
            pending.removeAll()
            pendingBytes = 0
        }
    }

    // MARK: - 内部処理（queue 上）

    /// WebSocket を開き、設定 → 溜めた音声 の順に送って受信を始める
    private func connectLocked() {
        guard !isStopped, let apiKey, let endpoint else { return }
        connection += 1
        let current = connection
        let task = Self.session.webSocketTask(with: endpoint)
        task.resume()
        // 設定は最初のフレームでなければならない（音声より先に届く必要がある）
        task.send(.string(Self.configJSON(apiKey: apiKey, translates: translates, language: language))) { _ in }
        for chunk in pending { task.send(.data(chunk)) { _ in } }
        pending.removeAll()
        pendingBytes = 0
        self.task = task
        connectedAt = CFAbsoluteTimeGetCurrent()
        lastSentAt = connectedAt
        logger.notice("Soniox 字幕を接続 翻訳=\(self.translates, privacy: .public) 言語=\(self.language, privacy: .public)")
        receive(task, connection: current)
    }

    /// 1 件ずつ受信して組み立てる
    private func receive(_ task: URLSessionWebSocketTask, connection current: Int) {
        task.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                guard current == self.connection, !self.isStopped else { return }
                switch result {
                case let .success(message):
                    let data: Data?
                    switch message {
                    case let .string(text): data = text.data(using: .utf8)
                    case let .data(raw): data = raw
                    @unknown default: data = nil
                    }
                    if let data, let response = SonioxCaptionAssembler.parse(data) {
                        guard self.handleLocked(response) else { return }
                    }
                    self.receive(task, connection: current)
                case let .failure(error):
                    self.logger.notice("Soniox 字幕の受信が切れました: \(error.localizedDescription, privacy: .public)")
                    self.scheduleReconnectLocked(isFatal: false, reason: "接続が切れました")
                }
            }
        }
    }

    /// 応答 1 件を処理する
    ///
    /// - Returns: 受信を続けるなら true
    private func handleLocked(_ response: SonioxCaptionAssembler.Response) -> Bool {
        if let code = response.errorCode {
            logger.error("Soniox 字幕のエラー code=\(code, privacy: .public) \(response.errorMessage ?? "", privacy: .public)")
            scheduleReconnectLocked(
                isFatal: SonioxReconnectPolicy.isFatal(errorCode: code),
                reason: code == 401 ? "Soniox の API キーが無効です" : "Soniox がエラーを返しました（\(code)）"
            )
            return false
        }
        // しばらく安定して受信できていれば、失敗の回数を数え直す
        if CFAbsoluteTimeGetCurrent() - connectedAt >= SonioxReconnectPolicy.healthyDuration {
            reconnect.noteHealthy()
        }
        emit(assembler.apply(response))
        updateTranslationWaitLocked()
        if response.finished {
            scheduleReconnectLocked(isFatal: false, reason: "Soniox がストリームを閉じました")
            return false
        }
        return true
    }

    /// 組み立て結果をコールバックへ流す
    private func emit(_ events: [SonioxCaptionAssembler.Event]) {
        for event in events {
            switch event {
            case let .live(source, translation): onLive?(source, translation)
            case let .confirmed(source, translation): onConfirmed?(source, translation)
            }
        }
    }

    /// 切れ目の後に訳を待っている間だけ、時間切れの確定を仕掛ける
    private func updateTranslationWaitLocked() {
        guard assembler.isWaitingForTranslation else {
            translationWaitTimer?.cancel()
            translationWaitTimer = nil
            return
        }
        guard translationWaitTimer == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + Self.translationWaitLimit)
        timer.setEventHandler { [weak self] in
            guard let self, !self.isStopped else { return }
            self.translationWaitTimer = nil
            if let line = self.assembler.flush() { self.emit([line]) }
        }
        timer.resume()
        translationWaitTimer = timer
    }

    /// 張り直しを予約する（上限を超えたら諦めて知らせる）
    private func scheduleReconnectLocked(isFatal: Bool, reason: String) {
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        connection += 1
        // 切れる直前までの分は確定させておく（張り直しで話が飛ばないように）
        if let line = assembler.flush() { emit([line]) }
        guard let delay = reconnect.nextDelay(isFatal: isFatal) else {
            logger.error("Soniox 字幕の再接続を諦めます: \(reason, privacy: .public)")
            isStopped = true
            keepaliveTimer?.cancel(); keepaliveTimer = nil
            onGiveUp?(reason)
            return
        }
        logger.notice("Soniox 字幕を \(delay, privacy: .public) 秒後に再接続（\(self.reconnect.failures, privacy: .public) 回目）")
        reconnectTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + delay)
        timer.setEventHandler { [weak self] in
            self?.reconnectTimer = nil
            self?.connectLocked()
        }
        timer.resume()
        reconnectTimer = timer
    }

    /// 音声が途切れている間のキープアライブ
    private func startKeepaliveLocked() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + Self.keepaliveInterval, repeating: Self.keepaliveInterval)
        timer.setEventHandler { [weak self] in
            guard let self, !self.isStopped, let task = self.task else { return }
            guard CFAbsoluteTimeGetCurrent() - self.lastSentAt >= Self.keepaliveInterval else { return }
            self.lastSentAt = CFAbsoluteTimeGetCurrent()
            task.send(.string("{\"type\":\"keepalive\"}")) { _ in }
        }
        timer.resume()
        keepaliveTimer = timer
    }
}
