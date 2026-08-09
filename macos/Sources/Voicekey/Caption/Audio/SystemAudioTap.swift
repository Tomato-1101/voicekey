/// システム音声のキャプチャ（Core Audio Process Tap）
///
/// ScreenCaptureKit は録画インジケータが出るため使わず、macOS 14.4+ の Process Tap
/// (`AudioHardwareCreateProcessTap`) と Aggregate Device を組み合わせてシステム音声全体を取る。
///
/// **不変条件（CLAUDE.md）**: 将来 TTS（翻訳音声の読み上げ）を入れたときに
/// 「読み上げ → 再認識 → 再翻訳」の無限ループへ落ちないよう、タップは最初から
/// **自プロセスを除外したグローバルタップ**として構成する。
import AVFoundation
import CoreAudio
import Foundation
import OSLog
import os

/// システム音声タップ。自プロセスを除外したグローバルタップを張り、PCM を継続的に流す。
///
/// スレッド設計:
/// - 生成・破棄・再構成はすべて `controlQueue`（直列）上で行う。
/// - PCM の配送は `ioQueue` 上（CoreAudio の IOProc ブロック）で行う。
/// - 両者をまたぐ状態は `lastFrameArrival` だけで、これは unfair lock で保護する。
@available(macOS 26.0, *)
final class SystemAudioTap: @unchecked Sendable {

    /// PCM バッファ 1 ブロックを受け取るハンドラ。`ioQueue` 上で呼ばれる。
    typealias BufferHandler = @Sendable (AVAudioPCMBuffer) -> Void

    /// フレームがこの秒数届かなければ「黙死」とみなしてタップを作り直す
    ///
    /// Aggregate Device は無音でも IO サイクルごとにバッファを配るため、
    /// 「届かない」は音が無いのではなく経路が壊れている（BT の A2DP↔HFP 切替等）ことを意味する。
    private static let frameStallThreshold: CFAbsoluteTime = 3.0

    /// 再構成に失敗したときの再試行間隔（秒）
    private static let retryInterval: TimeInterval = 2.0

    private let logger = makeCaptionLogger("SystemAudioTap")
    private let controlQueue = DispatchQueue(label: "com.voicekey.caption.tap.control")
    private let ioQueue = DispatchQueue(label: "com.voicekey.caption.tap.io", qos: .userInitiated)
    private let handler: BufferHandler

    // 以下 controlQueue 専有の状態
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateDeviceID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var running = false
    private var watchdogTimer: DispatchSourceTimer?
    private var deviceListenerBlock: AudioObjectPropertyListenerBlock?

    /// 最後にフレームが届いた時刻。IO キューが書き、watchdog（controlQueue）が読む。
    private let lastFrameArrival = OSAllocatedUnfairLock<CFAbsoluteTime>(initialState: 0)

    /// タップが現在使っているフォーマット（デバッグ表示用）。controlQueue 専有。
    private(set) var currentFormatDescription: String = "(未確定)"

    /// 拾う範囲。controlQueue 専有。
    private var scope: CaptureScope

    /// - Parameters:
    ///   - scope: 拾う範囲（既定はすべてのアプリ／自プロセス除外）
    ///   - handler: PCM を受け取るハンドラ。IO キュー上で高頻度に呼ばれるため軽い処理のみ。
    init(scope: CaptureScope = .allExcludingSelf, handler: @escaping BufferHandler) {
        self.scope = scope
        self.handler = handler
    }

    deinit {
        // deinit は任意スレッドで走るため、同期的に後始末する（controlQueue には積まない）
        teardownResources()
    }

    // MARK: - 公開 API

    /// タップを開始する（非同期。結果はログに出る）
    func start() {
        controlQueue.async { [weak self] in
            guard let self, !self.running else { return }
            self.installDefaultOutputDeviceListener()
            self.setUpAndRun()
            self.startWatchdog()
        }
    }

    /// 拾う範囲を差し替える（変わっていればタップを張り替える）
    ///
    /// 張り替えの間は認識ストリームが一瞬途切れるが、無音アプリへの切替では
    /// 呼ばれない（`CaptureScopeTracker` 側で安定化している）ため頻度は低い。
    ///
    /// - Parameter newScope: 新しい範囲
    func setScope(_ newScope: CaptureScope) {
        controlQueue.async { [weak self] in
            guard let self, self.scope != newScope else { return }
            self.scope = newScope
            guard self.running else { return }
            self.rebuild(reason: "キャプチャ対象の変更")
        }
    }

    /// タップを停止して資源を解放する。呼び出しは完了までブロックする。
    func stop() {
        controlQueue.sync { [weak self] in
            guard let self else { return }
            guard self.running || self.tapID != AudioObjectID(kAudioObjectUnknown) else { return }
            self.logger.notice("システム音声タップを停止します")
            self.stopWatchdog()
            self.removeDefaultOutputDeviceListener()
            self.teardownResources()
            self.running = false
        }
    }

    // MARK: - 構築・破棄（controlQueue 上でのみ呼ぶ）

    /// タップと Aggregate Device を作り、IOProc を開始する
    private func setUpAndRun() {
        dispatchPrecondition(condition: .onQueue(controlQueue))
        do {
            let format = try createTapAndAggregateDevice()
            try startIOProc(format: format)
            running = true
            lastFrameArrival.withLock { $0 = CFAbsoluteTimeGetCurrent() }
            currentFormatDescription = describe(format)
            logger.notice("システム音声タップを開始しました format=\(self.currentFormatDescription, privacy: .public)")
        } catch {
            running = false
            teardownResources()
            logger.error("タップの構築に失敗: \(String(describing: error), privacy: .public)")
            // 出力デバイスの切替直後などは一時的に失敗する。あきらめず再試行する。
            controlQueue.asyncAfter(deadline: .now() + Self.retryInterval) { [weak self] in
                guard let self, !self.running else { return }
                self.logger.notice("タップの再構築を再試行します")
                self.setUpAndRun()
            }
        }
    }

    /// Process Tap → Aggregate Device の順に構築する
    ///
    /// - Returns: タップが出力する PCM のフォーマット
    /// - Throws: CoreAudio 呼び出しが失敗した場合
    private func createTapAndAggregateDevice() throws -> AVAudioFormat {
        dispatchPrecondition(condition: .onQueue(controlQueue))

        let description = makeTapDescription()
        // isExclusive はロックの有無ではなく「向き」を表す:
        // true = processes/bundleIDs を除外、false = それらだけを対象。
        // 反転しても API はエラーを返さず、無言で意図と逆のタップになるため必ず記録する。
        logger.notice(
            """
            タップ構成: 範囲=\(self.scopeDescription, privacy: .public) \
            exclusive=\(description.isExclusive, privacy: .public) \
            processes=\(description.processes.map(String.init).joined(separator: ","), privacy: .public) \
            bundleIDs=\(description.bundleIDs, privacy: .public)
            """
        )

        var newTapID = AudioObjectID(kAudioObjectUnknown)
        let tapStatus = AudioHardwareCreateProcessTap(description, &newTapID)
        guard tapStatus == noErr else {
            throw CoreAudioError.osStatus(operation: "Process Tap の生成", status: tapStatus)
        }
        tapID = newTapID

        var streamDescription = try readTapStreamDescription(newTapID)

        // --- Aggregate Device を組む ---
        // main sub-device に実ハードウェアを据え、タップは taps リストに載せる。
        // タップを main sub-device にすると HAL は無言でゼロサンプルを返す。
        let outputDeviceID = try readDefaultSystemOutputDeviceID()
        let outputUID = try readDeviceUID(outputDeviceID)
        let aggregateDescription: [String: Any] = [
            kAudioAggregateDeviceNameKey: "voicekey-caption-aggregate",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [
                [kAudioSubDeviceUIDKey: outputUID]
            ],
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapDriftCompensationKey: true,
                    kAudioSubTapUIDKey: description.uuid.uuidString
                ]
            ]
        ]

        var newAggregateID = AudioObjectID(kAudioObjectUnknown)
        let aggregateStatus = AudioHardwareCreateAggregateDevice(
            aggregateDescription as CFDictionary, &newAggregateID
        )
        guard aggregateStatus == noErr else {
            throw CoreAudioError.osStatus(operation: "Aggregate Device の生成", status: aggregateStatus)
        }
        aggregateDeviceID = newAggregateID
        logger.notice("Aggregate Device を生成 (main sub-device=\(outputUID, privacy: .public))")

        // IO サイクルは Aggregate Device（＝main sub-device）のクロックで回るため、
        // 実際に届くフレームレートはタップ ASBD の値ではなくこちらになる。
        // 実測: タップ ASBD が 48000Hz でも内蔵スピーカーが 44100Hz なら 44100 相当しか来ず、
        // 48000 として扱うと約 8% 遅い（低い）音を認識器へ渡すことになる。
        let aggregateRate = try readNominalSampleRate(newAggregateID)
        if aggregateRate > 0, aggregateRate != streamDescription.mSampleRate {
            logger.notice(
                "タップ ASBD のレート \(streamDescription.mSampleRate, privacy: .public)Hz を Aggregate Device の \(aggregateRate, privacy: .public)Hz に合わせます"
            )
            streamDescription.mSampleRate = aggregateRate
        }

        guard let format = AVAudioFormat(streamDescription: &streamDescription) else {
            throw CoreAudioError.message("タップ出力フォーマットを AVAudioFormat に変換できません")
        }
        return format
    }

    /// 現在の範囲に応じたタップ記述を作る
    ///
    /// - すべて: 自プロセスを除外したグローバルタップ（TTS 再キャプチャループ禁止の不変条件）。
    ///   PID→プロセスオブジェクトは「まだ一度も音を出していないプロセス」には存在しないため、
    ///   `bundleIDs`（macOS 26+）による除外も必ず併用する。
    /// - 対象限定: 指定プロセスだけのミックスダウン。自プロセスは元々含まれない。
    private func makeTapDescription() -> CATapDescription {
        dispatchPrecondition(condition: .onQueue(controlQueue))
        let description: CATapDescription
        switch scope {
        case .allExcludingSelf:
            let ownProcessObject = translatePIDToAudioProcessObject(getpid())
            description = CATapDescription(
                stereoGlobalTapButExcludeProcesses: ownProcessObject.map { [$0] } ?? []
            )
            if let bundleID = Bundle.main.bundleIdentifier {
                description.bundleIDs = [bundleID]
            }
        case let .processes(objectIDs):
            description = CATapDescription(stereoMixdownOfProcesses: objectIDs)
        }
        description.name = "voicekey caption system tap"
        description.uuid = UUID()
        description.isPrivate = true              // 他プロセスからは見えないタップにする
        description.muteBehavior = .unmuted       // ユーザーには今まで通り音を聞かせる
        return description
    }

    /// ログ用の範囲表示
    private var scopeDescription: String {
        switch scope {
        case .allExcludingSelf: return "すべて（自プロセス除外）"
        case let .processes(objectIDs):
            return objectIDs.isEmpty ? "対象なし（無音）" : "対象 \(objectIDs.count) プロセス"
        }
    }

    /// IOProc を登録して再生を開始する
    ///
    /// - Parameter format: タップ出力フォーマット（IO ブロックがバッファを包むのに使う）
    private func startIOProc(format: AVAudioFormat) throws {
        dispatchPrecondition(condition: .onQueue(controlQueue))

        let handler = self.handler
        let arrival = self.lastFrameArrival
        // IO ブロックは高頻度で呼ばれるため self を強く握らず、必要な値だけ捕捉する
        let ioBlock: AudioDeviceIOBlock = { _, inInputData, _, _, _ in
            guard let buffer = AVAudioPCMBuffer(
                pcmFormat: format, bufferListNoCopy: inInputData, deallocator: nil
            ) else { return }
            arrival.withLock { $0 = CFAbsoluteTimeGetCurrent() }
            handler(buffer)
        }

        // 第 3 引数の queue に nil を渡すと macOS 26 では無言で登録に失敗する
        var newProcID: AudioDeviceIOProcID?
        let createStatus = AudioDeviceCreateIOProcIDWithBlock(
            &newProcID, aggregateDeviceID, ioQueue, ioBlock
        )
        guard createStatus == noErr, let newProcID else {
            throw CoreAudioError.osStatus(operation: "IOProc の生成", status: createStatus)
        }
        ioProcID = newProcID

        let startStatus = AudioDeviceStart(aggregateDeviceID, newProcID)
        guard startStatus == noErr else {
            throw CoreAudioError.osStatus(operation: "Aggregate Device の開始", status: startStatus)
        }
    }

    /// タップ・Aggregate Device・IOProc を規定の順序で破棄する
    ///
    /// 順序を守らないと HAL 側にゴーストデバイスが残る:
    /// Stop → DestroyIOProcID → DestroyAggregateDevice → DestroyProcessTap
    private func teardownResources() {
        if aggregateDeviceID != AudioObjectID(kAudioObjectUnknown) {
            if let ioProcID {
                let stopStatus = AudioDeviceStop(aggregateDeviceID, ioProcID)
                if stopStatus != noErr {
                    logger.notice("Aggregate Device の停止に失敗 status=\(stopStatus, privacy: .public)")
                }
                let destroyStatus = AudioDeviceDestroyIOProcID(aggregateDeviceID, ioProcID)
                if destroyStatus != noErr {
                    logger.notice("IOProc の破棄に失敗 status=\(destroyStatus, privacy: .public)")
                }
                self.ioProcID = nil
            }
            let destroyStatus = AudioHardwareDestroyAggregateDevice(aggregateDeviceID)
            if destroyStatus != noErr {
                logger.notice("Aggregate Device の破棄に失敗 status=\(destroyStatus, privacy: .public)")
            }
            aggregateDeviceID = AudioObjectID(kAudioObjectUnknown)
        }

        if tapID != AudioObjectID(kAudioObjectUnknown) {
            let destroyStatus = AudioHardwareDestroyProcessTap(tapID)
            if destroyStatus != noErr {
                logger.notice("Process Tap の破棄に失敗 status=\(destroyStatus, privacy: .public)")
            }
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
    }

    /// タップを作り直す（デバイス変更・黙死検知の共通処理）
    private func rebuild(reason: String) {
        dispatchPrecondition(condition: .onQueue(controlQueue))
        logger.notice("タップを再構成します 理由=\(reason, privacy: .public)")
        teardownResources()
        running = false
        setUpAndRun()
    }

    // MARK: - watchdog（フレーム到達監視）

    /// 1 秒周期でフレーム到達を監視するタイマーを起動する
    private func startWatchdog() {
        dispatchPrecondition(condition: .onQueue(controlQueue))
        guard watchdogTimer == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: controlQueue)
        timer.schedule(deadline: .now() + 1.0, repeating: 1.0)
        timer.setEventHandler { [weak self] in self?.checkFrameFlow() }
        timer.resume()
        watchdogTimer = timer
    }

    /// watchdog タイマーを止める
    private func stopWatchdog() {
        dispatchPrecondition(condition: .onQueue(controlQueue))
        watchdogTimer?.cancel()
        watchdogTimer = nil
    }

    /// フレームが途切れていないか確認し、途切れていればタップを作り直す
    ///
    /// 対象プロセス限定のタップは、**対象が黙っている間はフレームを 1 つも配らない**
    /// （グローバルタップは無音でも IO サイクルごとに配るので、そこが決定的に違う）。
    /// そのため「届かない＝壊れた」とは言えず、対象が実際に出力中のときだけ黙死とみなす。
    private func checkFrameFlow() {
        dispatchPrecondition(condition: .onQueue(controlQueue))
        guard running else { return }
        let last = lastFrameArrival.withLock { $0 }
        let elapsed = CFAbsoluteTimeGetCurrent() - last
        guard elapsed > Self.frameStallThreshold else { return }
        if case let .processes(objectIDs) = scope {
            let emitting = readAudioProcesses().contains {
                objectIDs.contains($0.objectID) && $0.isRunningOutput
            }
            // 対象が鳴っていないだけなら正常。ここで作り直すと 3 秒ごとに張り替え続けてしまう。
            guard emitting else { return }
        }
        rebuild(reason: String(format: "フレームが %.1f 秒届いていない", elapsed))
    }

    // MARK: - 既定出力デバイスの変更監視

    /// 既定システム出力デバイスの変更リスナを登録する
    ///
    /// Aggregate Device は生成時の出力デバイスに固定されるため、ユーザーが
    /// BT ヘッドホンへ切り替えたら作り直さないと音が来なくなる。
    private func installDefaultOutputDeviceListener() {
        dispatchPrecondition(condition: .onQueue(controlQueue))
        guard deviceListenerBlock == nil else { return }
        var address = globalPropertyAddress(kAudioHardwarePropertyDefaultSystemOutputDevice)
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            // リスナは controlQueue 上で呼ばれる（登録時に指定しているため）
            self?.rebuild(reason: "既定システム出力デバイスの変更")
        }
        let status = AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject), &address, controlQueue, block
        )
        if status == noErr {
            deviceListenerBlock = block
        } else {
            logger.notice("出力デバイス変更リスナの登録に失敗 status=\(status, privacy: .public)")
        }
    }

    /// 既定システム出力デバイスの変更リスナを外す
    private func removeDefaultOutputDeviceListener() {
        dispatchPrecondition(condition: .onQueue(controlQueue))
        guard let block = deviceListenerBlock else { return }
        var address = globalPropertyAddress(kAudioHardwarePropertyDefaultSystemOutputDevice)
        AudioObjectRemovePropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject), &address, controlQueue, block
        )
        deviceListenerBlock = nil
    }

    // MARK: - ユーティリティ

    /// フォーマットをログ向けの 1 行に整形する
    private func describe(_ format: AVAudioFormat) -> String {
        String(
            format: "%.0fHz %dch %@",
            format.sampleRate,
            format.channelCount,
            format.isInterleaved ? "interleaved" : "non-interleaved"
        )
    }
}
