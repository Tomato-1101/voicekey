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
/// - **音が来ない間は切る**（`SonioxIdleTracker`）。課金はストリームを開いている時間ぶんなので、
///   対象が黙っている間も keepalive で繋ぎ続けると無音に課金され続ける。WebSocket だけを閉じ、
///   タップは張ったままにする（HAL を叩かない）。張り直しは次の有音フレームで行い、失敗には数えない。
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

    /// 諦めたときにユーザーへ出す理由
    ///
    /// 402 / 401・403 は張り直しでは直らず本人が手を打つ必要があるので、何が起きたかが一目で分かる言葉にする。
    static func giveUpReason(errorCode: Int) -> String {
        switch errorCode {
        case 402: return "Soniox の残高不足"
        case 401, 403: return "Soniox のキーが無効"
        default: return "Soniox がエラーを返しました（\(errorCode)）"
        }
    }
}

/// 無音での切断と、有音での張り直しの判定（純ロジック・単体テスト用に分けている）
///
/// 再接続の失敗回数（`SonioxReconnectPolicy`）とは独立させている。アイドル切断は失敗ではないので、
/// 何度切っても張り直しても諦めの上限に近づかない。
struct SonioxIdleTracker {

    /// 無音がこれだけ続いたら切る（秒）
    ///
    /// 会話の間（数秒〜十数秒）では切らず、動画の一時停止や会議の休憩のような
    /// 「しばらく誰も喋らない」ときだけ切る長さ。判定は keepalive の 10 秒刻みなので実際は 30〜40 秒で切れる。
    static let idleDisconnectInterval: TimeInterval = 30
    /// 有音とみなす RMS（字幕の切れ目判定 `PauseFinalizeTrigger.silenceRMS` と同じ基準）
    static let voiceRMS: Float = PauseFinalizeTrigger.silenceRMS

    /// 最後に有音のフレームが届いた（または接続した）時刻
    private(set) var lastVoiceAt: CFAbsoluteTime = 0
    /// 無音のため切っている
    private(set) var isIdle = false

    /// 接続した（ここから無音を数える）
    mutating func noteConnected(now: CFAbsoluteTime) {
        isIdle = false
        lastVoiceAt = max(lastVoiceAt, now)
    }

    /// フレームが届いた
    ///
    /// - Returns: 無音で切っていて、有音が来たので張り直すべきとき true
    mutating func noteFrame(isVoiced: Bool, now: CFAbsoluteTime) -> Bool {
        guard isVoiced else { return false }
        lastVoiceAt = now
        guard isIdle else { return false }
        isIdle = false
        return true
    }

    /// 接続中に、無音が続いたので切るべきか（true を返したら切った扱いになる）
    mutating func shouldDisconnect(now: CFAbsoluteTime) -> Bool {
        guard !isIdle, now - lastVoiceAt >= Self.idleDisconnectInterval else { return false }
        isIdle = true
        return true
    }

    /// フレームの RMS
    static func rms(_ samples: [Float]) -> Float {
        guard !samples.isEmpty else { return 0 }
        return (samples.reduce(Float(0)) { $0 + $1 * $1 } / Float(samples.count)).squareRoot()
    }
}

/// Soniox 接続の状態（メニューの状態行に出す）
enum SonioxCaptionLinkState: Equatable {
    /// 接続している（課金中）
    case connected
    /// 無音が続いたので切っている（課金なし・音が来たら張り直す）
    case idle
    /// 切れたので張り直しを待っている
    case reconnecting

    /// メニューに出す 1 行
    var menuText: String {
        switch self {
        case .connected: return "Soniox 接続中"
        case .idle: return "Soniox 待機中（無音のため切断）"
        case .reconnecting: return "Soniox 再接続中…"
        }
    }

    /// Soniox を諦めて Apple に切り替えている間の 1 行
    static func fallbackMenuText(reason: String) -> String {
        "Soniox → Apple に切替中（\(reason)）"
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
    /// 停止時に、終端を送ってから残りの確定（`finished`）を待つ上限（秒）
    ///
    /// 音声入力の Soniox と同じく、ふだんは数百 ms で返る。字幕の停止操作を長く固めないよう短めに切る。
    static let finishWaitLimit: TimeInterval = 1.5
    /// 停止時に送る終端（未確定トークンを確定させる finalize → 音声の終わりを示す長さ 0 のフレーム）
    ///
    /// 音声入力の Soniox（`SonioxLiveTranscriber`）と同じ作法。テストから中身を確かめるため static に出している。
    static let endOfStreamMessages: [URLSessionWebSocketTask.Message] = [
        .string("{\"type\":\"finalize\"}"),
        .data(Data()),
    ]
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
    private var idle = SonioxIdleTracker()
    private var pending: [Data] = []
    private var pendingBytes = 0
    private var lastSentAt: CFAbsoluteTime = 0
    private var connectedAt: CFAbsoluteTime = 0
    private var keepaliveTimer: DispatchSourceTimer?
    private var translationWaitTimer: DispatchSourceTimer?
    private var reconnectTimer: DispatchSourceTimer?
    private var isStopped = false
    /// 停止のために終端を送り、残りの確定を待っている（この間は音声を足さない・張り直さない）
    private var isFinishing = false
    /// 停止待ちの継続（`finished`・切断・時間切れのどれかで 1 回だけ再開する）
    private var finishWaiter: CheckedContinuation<Void, Never>?

    /// ライブ行の更新（原文, 訳）。任意スレッドから呼ぶ
    var onLive: (@Sendable (String, String) -> Void)?
    /// 1 行の確定（原文, 訳）。任意スレッドから呼ぶ
    var onConfirmed: (@Sendable (String, String) -> Void)?
    /// 張り直しを諦めた（ユーザー向けの理由）。任意スレッドから 1 回だけ呼ぶ
    var onGiveUp: (@Sendable (String) -> Void)?
    /// 接続状態が変わった（メニュー表示用）。任意スレッドから呼ぶ
    var onLinkStateChange: (@Sendable (SonioxCaptionLinkState) -> Void)?

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
        let isVoiced = SonioxIdleTracker.rms(samples) >= SonioxIdleTracker.voiceRMS
        queue.async { [self] in
            // 終端を送った後は音声を足さない（Soniox は終端の後の音声を受け付けない）
            guard !isStopped, !isFinishing else { return }
            let shouldReconnect = idle.noteFrame(isVoiced: isVoiced, now: CFAbsoluteTimeGetCurrent())
            guard let task else {
                // 接続待ち・張り直し中・無音で切断中は少しだけ溜める（古い分から捨てる）
                pending.append(pcm)
                pendingBytes += pcm.count
                while pendingBytes > Self.maxPendingBytes, !pending.isEmpty {
                    pendingBytes -= pending.removeFirst().count
                }
                // 無音で切っていたなら、有音が来た時点で張り直す。溜めた音声ごと送るので話し始めは欠けない。
                // 失敗ではないので再接続の回数（reconnect）には触れない。
                if shouldReconnect {
                    logger.notice("音が戻ったので Soniox 字幕を張り直します")
                    connectLocked()
                }
                return
            }
            lastSentAt = CFAbsoluteTimeGetCurrent()
            task.send(.data(pcm)) { _ in }
        }
    }

    /// 停止する
    ///
    /// 接続中なら終端（finalize → 長さ 0 のフレーム）を送り、Soniox が残りを確定して `finished` を返すのを
    /// 最大 `finishWaitLimit` 秒待つ。そのあと残っている分（未確定を含む）を 1 行に確定してから閉じる。
    /// 停止の瞬間に喋っていた最後の語を議事録から落とさないため。戻った後はコールバックを呼ばない。
    func stop() async {
        let waits: Bool = queue.sync { [self] in
            guard !isStopped, !isFinishing else { return false }
            keepaliveTimer?.cancel(); keepaliveTimer = nil
            reconnectTimer?.cancel(); reconnectTimer = nil
            guard let task else { return false }
            isFinishing = true
            for message in Self.endOfStreamMessages { task.send(message) { _ in } }
            return true
        }
        if waits {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                queue.async { [self] in
                    // 待ちを登録する前に finished が届いていたら待たない
                    guard isFinishing else { continuation.resume(); return }
                    finishWaiter = continuation
                    queue.asyncAfter(deadline: .now() + Self.finishWaitLimit) { [self] in
                        resumeFinishWaiterLocked()
                    }
                }
            }
        }
        queue.sync { [self] in
            guard !isStopped else { return }
            if let line = assembler.flush() { emit([line]) }
            isStopped = true
            isFinishing = false
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

    /// 停止待ちを起こす（`finished`・切断・エラー・時間切れのどれが先でも 1 回だけ効く）
    private func resumeFinishWaiterLocked() {
        isFinishing = false
        finishWaiter?.resume()
        finishWaiter = nil
    }

    /// WebSocket を開き、設定 → 溜めた音声 の順に送って受信を始める
    private func connectLocked() {
        guard !isStopped, !isFinishing, let apiKey, let endpoint else { return }
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
        idle.noteConnected(now: connectedAt)
        logger.notice("Soniox 字幕を接続 翻訳=\(self.translates, privacy: .public) 言語=\(self.language, privacy: .public)")
        onLinkStateChange?(.connected)
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
                reason: SonioxReconnectPolicy.giveUpReason(errorCode: code)
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
            // 停止のために送った終端への返事なら、待っている stop() を起こすだけ（張り直さない）
            if isFinishing {
                resumeFinishWaiterLocked()
                return false
            }
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
            // 確定済みの分だけを行にする。未確定分は接続が続く限り後で確定トークンとして届くので、
            // ここで行にすると同じ文が二重に記録される（停止時の flush だけが未確定分まで出す）
            if let line = self.assembler.flushConfirmed() { self.emit([line]) }
        }
        timer.resume()
        translationWaitTimer = timer
    }

    /// 張り直しを予約する（上限を超えたら諦めて知らせる）
    private func scheduleReconnectLocked(isFatal: Bool, reason: String) {
        // 停止のための終端待ちで切れたのなら、張り直さずに stop() へ戻す
        if isFinishing {
            resumeFinishWaiterLocked()
            return
        }
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
        onLinkStateChange?(.reconnecting)
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

    /// 音声が途切れている間のキープアライブと、無音が続いたときの切断
    private func startKeepaliveLocked() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + Self.keepaliveInterval, repeating: Self.keepaliveInterval)
        timer.setEventHandler { [weak self] in
            guard let self, !self.isStopped, !self.isFinishing, let task = self.task else { return }
            let now = CFAbsoluteTimeGetCurrent()
            if self.idle.shouldDisconnect(now: now) {
                self.disconnectForIdleLocked(task)
                return
            }
            guard now - self.lastSentAt >= Self.keepaliveInterval else { return }
            self.lastSentAt = now
            task.send(.string("{\"type\":\"keepalive\"}")) { _ in }
        }
        timer.resume()
        keepaliveTimer = timer
    }

    /// 無音が続いたので WebSocket だけ閉じる（タップ・HAL には触れない）
    ///
    /// 失敗ではないので再接続の回数には数えない。次に有音のフレームが来たら `send` が張り直す。
    private func disconnectForIdleLocked(_ task: URLSessionWebSocketTask) {
        // 世代を進めて、閉じた接続の受信失敗を「切れた」として数えないようにする
        connection += 1
        self.task = nil
        task.cancel(with: .normalClosure, reason: nil)
        translationWaitTimer?.cancel(); translationWaitTimer = nil
        // 30 秒の無音の後なので未確定はまず無いが、残っていれば落とさず確定させる
        if let line = assembler.flush() { emit([line]) }
        logger.notice("無音が続いたため Soniox 字幕を切断（音が来たら張り直す）")
        onLinkStateChange?(.idle)
    }
}
