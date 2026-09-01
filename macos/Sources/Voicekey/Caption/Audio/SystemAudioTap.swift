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
    /// ただし「誰も音を出していない」だけでもフレームは 1 つも来ない。
    /// 当初は「グローバルタップなら無音でも IO サイクルごとに配られる」と考えていたが、
    /// 実測では内蔵スピーカーでも BT でも**無音時はフレームが 0**だった（2026-08-10）。
    /// そのため作り直す前に必ず「本当に誰かが鳴らしているか」を確かめる。
    private static let frameStallThreshold: CFAbsoluteTime = 3.0

    /// 鳴っているのにフレームが来ない状態での作り直しを、この回数まで許す
    ///
    /// **無制限に作り直してはいけない**（2026-08-10 の実事故）。上の前提が誤っていたため、
    /// 無音のあいだ 4 秒おきに Process Tap と Aggregate Device を作り直し続け、
    /// その churn が既定デバイスの再評価を繰り返し起こして coreaudiod を詰まらせた。
    private static let maxStallRebuilds = 3

    /// 再構成に失敗したときの最初の再試行間隔（秒）
    ///
    /// 失敗が続くたびに倍にして `maxRetryInterval` で頭打ちにする（指数バックオフ）。
    private static let retryInterval: TimeInterval = 2.0

    /// 再試行間隔の上限（秒）
    private static let maxRetryInterval: TimeInterval = 30.0

    /// 連続失敗をこの回数まで許す。超えたら諦めて停止する
    ///
    /// **無制限に再試行してはいけない**（2026-08-10 の実事故）。CoreAudio が不調になると
    /// タップ生成が必ず失敗するようになり、固定 2 秒間隔の再試行が
    /// `AudioHardwareCreateProcessTap` を延々と撃ち続けて coreaudiod を詰まらせた。
    /// その結果、同じ Mac の**全プロセス**でオーディオが応答不能になった
    /// （無関係な afplay すら HAL の初期化でハングし、復旧に coreaudiod の再起動が要った）。
    /// 諦めたあとはユーザーが字幕を開始し直せば再挑戦できる。
    private static let maxRebuildAttempts = 5

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
    /// 連続で構築に失敗した回数（成功で 0 に戻す）
    private var consecutiveFailures = 0
    /// 上限まで失敗して再試行を諦めたか（start() し直すまで何もしない）
    private var hasGivenUp = false
    /// フレーム途絶で連続して作り直した回数（本物のフレームが届いたら 0 に戻す）
    private var consecutiveStallRebuilds = 0
    /// 直近の作り直し直後に記録したフレーム到達時刻（これが動いていれば本当に届いた）
    private var stallBaselineArrival: CFAbsoluteTime = 0
    /// Aggregate Device を組んだときの既定出力デバイス（本当に変わったかの判定基準）
    private var builtOutputDeviceID = AudioObjectID(kAudioObjectUnknown)

    /// 最後にフレームが届いた時刻。IO キューが書き、watchdog（controlQueue）が読む。
    private let lastFrameArrival = OSAllocatedUnfairLock<CFAbsoluteTime>(initialState: 0)

    /// タップを作り直した回数（回帰ハーネスの判定材料。controlQueue が書き、他スレッドが読む）
    private let rebuildCounter = OSAllocatedUnfairLock(initialState: 0)

    /// タップを作り直した回数
    ///
    /// Process Tap と Aggregate Device の作り直しは HAL 全体に波及するため、
    /// 無音のあいだにこれが増えるのは異常（2026-08-10 の事故の直接原因）。
    var rebuildCount: Int { rebuildCounter.withLock { $0 } }

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
            // 前回諦めた状態を持ち越さない（ユーザーの開始し直しが再挑戦の契機になる）
            self.consecutiveFailures = 0
            self.hasGivenUp = false
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
        guard !hasGivenUp else { return }
        do {
            let format = try createTapAndAggregateDevice()
            try startIOProc(format: format)
            running = true
            consecutiveFailures = 0
            lastFrameArrival.withLock { $0 = CFAbsoluteTimeGetCurrent() }
            currentFormatDescription = describe(format)
            logger.notice("システム音声タップを開始しました format=\(self.currentFormatDescription, privacy: .public)")
        } catch {
            running = false
            teardownResources()
            consecutiveFailures += 1
            logger.error(
                "タップの構築に失敗(\(self.consecutiveFailures, privacy: .public)/\(Self.maxRebuildAttempts, privacy: .public)): \(String(describing: error), privacy: .public)"
            )

            // 上限を超えたら諦める。撃ち続けると coreaudiod ごと詰まらせて
            // Mac 全体のオーディオを巻き添えにするため（実事故あり）。
            guard consecutiveFailures < Self.maxRebuildAttempts else {
                hasGivenUp = true
                stopWatchdog()
                removeDefaultOutputDeviceListener()
                logger.error(
                    "タップの構築に \(Self.maxRebuildAttempts, privacy: .public) 回続けて失敗したため再試行を停止します（字幕を開始し直すと再挑戦します）"
                )
                return
            }

            // 出力デバイスの切替直後などは一時的に失敗する。指数バックオフで間隔を空けて再試行する。
            let delay = min(
                Self.retryInterval * pow(2.0, Double(consecutiveFailures - 1)),
                Self.maxRetryInterval
            )
            logger.notice("タップの再構築を \(delay, privacy: .public) 秒後に再試行します")
            controlQueue.asyncAfter(deadline: .now() + delay) { [weak self] in
                guard let self, !self.running, !self.hasGivenUp else { return }
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
        builtOutputDeviceID = outputDeviceID
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
        ActionLog.shared.write("caption.SystemAudioTap", "タップを再構成 理由=\(reason)")
        rebuildCounter.withLock { $0 += 1 }
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
    /// タップは**対象が黙っている間はフレームを 1 つも配らない**（グローバルタップでも同じ）。
    /// そのため「届かない＝壊れた」とは言えず、対象が実際に出力中のときだけ黙死とみなす。
    /// ここを取り違えると、無音のあいだ延々とタップを作り直し続けることになる。
    private func checkFrameFlow() {
        dispatchPrecondition(condition: .onQueue(controlQueue))
        guard running else { return }
        let last = lastFrameArrival.withLock { $0 }
        let elapsed = CFAbsoluteTimeGetCurrent() - last
        guard elapsed > Self.frameStallThreshold else { return }
        // 誰も鳴らしていないだけなら正常。ここで作り直すと数秒ごとに張り替え続けてしまう。
        guard isAnyTargetEmitting() else { return }

        // 前回の作り直し以降に本物のフレームが届いていたなら、連続回数は数え直す
        if last != stallBaselineArrival { consecutiveStallRebuilds = 0 }

        consecutiveStallRebuilds += 1
        guard consecutiveStallRebuilds <= Self.maxStallRebuilds else {
            logger.error(
                "鳴っているのにフレームが届かない状態が \(Self.maxStallRebuilds, privacy: .public) 回の作り直しでも直らないため監視を止めます（字幕を開始し直してください）"
            )
            stopWatchdog()
            return
        }
        rebuild(reason: String(format: "フレームが %.1f 秒届いていない", elapsed))
        stallBaselineArrival = lastFrameArrival.withLock { $0 }
    }

    /// いまタップ対象のプロセスが実際に音を出しているか
    ///
    /// - Returns: 出力中のプロセスがあれば true
    private func isAnyTargetEmitting() -> Bool {
        let processes = readAudioProcesses()
        switch scope {
        case .allExcludingSelf:
            // 自プロセスはタップから除外しているので、自分が鳴っていても対象にはならない
            let ownPID = getpid()
            return processes.contains { $0.isRunningOutput && $0.pid != ownPID }
        case let .processes(objectIDs):
            return processes.contains { objectIDs.contains($0.objectID) && $0.isRunningOutput }
        }
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
            guard let self else { return }
            // HAL は中身が変わっていなくても通知を投げてくる。作り直しは HAL に負荷を掛けるので、
            // Aggregate Device を組んだときのデバイスと実際に違うときだけ張り替える。
            let current = (try? readDefaultSystemOutputDeviceID()) ?? AudioObjectID(kAudioObjectUnknown)
            guard current != self.builtOutputDeviceID else { return }
            self.rebuild(reason: "既定システム出力デバイスの変更")
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
