/// キャプチャ対象（どのアプリの音を拾うか）の決定
///
/// 2026-08-09 ユーザー要望:「バックグラウンドで音楽を流していて、フロントで YouTube を
/// 見ていたら、一番手前に表示されているアプリだけの音声を取って翻訳するようにしてほしい」。
/// 裏で鳴っている音楽が認識に混ざるのを避けるのが目的。
///
/// 難しいのは「最前面アプリの PID」と「実際に音を出しているプロセス」が一致しないこと。
/// Chrome の音は Google Chrome Helper、Safari の音は com.apple.WebKit.GPU が出している。
/// そこで音声プロセスごとに責任 PID（`responsibility_get_pid_responsible_for_pid`）を辿り、
/// 最前面アプリの PID と一致するものを対象にする。バンドル ID の前方一致も併用する。
import AppKit
import CoreAudio
import Foundation
import OSLog

/// タップが拾う範囲
@available(macOS 26.0, *)
enum CaptureScope: Equatable {
    /// すべてのアプリ（自プロセスだけ除外）。従来の動作。
    case allExcludingSelf
    /// 指定した音声プロセスだけ
    case processes([AudioObjectID])
}

/// 現在の対象アプリの情報（メニュー表示用）
@available(macOS 26.0, *)
struct CaptureTarget: Equatable {
    /// 対象アプリの PID
    let pid: pid_t
    /// 画面に出す名前（例「Google Chrome」）
    let name: String
    /// 対象に含めた音声プロセス
    let processes: [AudioOutputProcess]
}

/// 最前面アプリを追いかけ、タップの対象プロセス集合を決める
///
/// スレッド設計: 通知は main で受け、HAL リスナは `queue` で受ける。状態の更新と
/// コールバックはすべて `queue`（直列）に集約する。
@available(macOS 26.0, *)
final class CaptureScopeTracker: @unchecked Sendable {

    private let logger = makeCaptionLogger("CaptureScope")
    private let queue = DispatchQueue(label: "com.voicekey.caption.capturescope")

    /// 対象が変わったときに呼ばれる（`queue` 上）
    var onScopeChanged: (@Sendable (CaptureScope, CaptureTarget?) -> Void)?
    /// 対象がまだ決まらず待っている間に呼ばれる（アプリ名。ロックできたら nil）
    var onWaitingForAudio: (@Sendable (String?) -> Void)?

    /// 直近に通知した待機状態（同じ内容を何度も通知しないための記憶）
    private var lastWaitingName: String??

    /// いまの対象（メニュー表示用。任意スレッドから読む）
    private let currentTarget = OSAllocatedUnfairLock<CaptureTarget?>(initialState: nil)
    /// いまの対象アプリ（読み出し用）
    var target: CaptureTarget? { currentTarget.withLock { $0 } }

    /// テスト用に固定する PID（環境変数 `VOICEKEY_CAPTION_TARGET_PID`）
    private let fixedPID: pid_t?

    private var activationObserver: NSObjectProtocol?
    private var processListListener: AudioObjectPropertyListenerBlock?
    /// 直近に対象とした音声プロセスのオブジェクト ID（張り替えの要否判定に使う）
    private var appliedObjectIDs: [AudioObjectID] = []
    private var isRunning = false

    /// - Parameter fixedPID: 最前面の代わりに常にこの PID を対象にする（テスト用）
    init(fixedPID: pid_t? = nil) {
        self.fixedPID = fixedPID
    }

    deinit {
        stop()
    }

    /// 追従を開始する（開始直後に 1 回評価する）
    func start() {
        queue.async { [weak self] in
            guard let self, !self.isRunning else { return }
            self.isRunning = true
            self.installObservers()
            self.evaluate(reason: "開始")
        }
    }

    /// 追従を停止する
    func stop() {
        queue.sync { [weak self] in
            guard let self, self.isRunning else { return }
            self.isRunning = false
            self.removeObservers()
            self.appliedObjectIDs = []
            self.lastWaitingName = nil
            self.currentTarget.withLock { $0 = nil }
        }
    }

    // MARK: - 監視

    /// 最前面アプリの変更と、HAL の音声プロセス一覧の変更を監視する
    ///
    /// プロセス一覧の監視が要るのは、再生を始めた瞬間にヘルパプロセスが**後から**生まれるため。
    /// これを見ていないと「対象アプリなのに音が拾えない」状態のまま止まってしまう。
    private func installObservers() {
        dispatchPrecondition(condition: .onQueue(queue))

        activationObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil, queue: .main
        ) { [weak self] _ in
            self?.queue.async { self?.evaluate(reason: "最前面アプリの変更") }
        }

        var address = globalPropertyAddress(kAudioHardwarePropertyProcessObjectList)
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            self?.evaluate(reason: "音声プロセス一覧の変更")
        }
        let status = AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject), &address, queue, block
        )
        if status == noErr {
            processListListener = block
        } else {
            logger.notice("音声プロセス一覧リスナの登録に失敗 status=\(status, privacy: .public)")
        }
    }

    /// 監視を外す
    private func removeObservers() {
        dispatchPrecondition(condition: .onQueue(queue))
        if let activationObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(activationObserver)
            self.activationObserver = nil
        }
        if let block = processListListener {
            var address = globalPropertyAddress(kAudioHardwarePropertyProcessObjectList)
            AudioObjectRemovePropertyListenerBlock(
                AudioObjectID(kAudioObjectSystemObject), &address, queue, block
            )
            processListListener = nil
        }
    }

    // MARK: - 対象の決定

    /// いまの最前面アプリから対象プロセスを決め、変わっていれば通知する
    ///
    /// **無音アプリへの切り替えでは対象を替えない**。Warp や Finder に一瞬フォーカスしただけで
    /// 字幕が途切れるのを避けるため、音を出しているアプリへ移ったときだけ対象を移す。
    ///
    /// - Parameter reason: ログに残す評価理由
    private func evaluate(reason: String) {
        dispatchPrecondition(condition: .onQueue(queue))
        guard isRunning else { return }

        guard let candidate = resolveCandidate() else {
            // 最前面アプリが分からない場合は何もしない（直前の対象を維持）
            return
        }

        let processes = audioProcesses(for: candidate.pid)
        let isEmitting = processes.contains(where: \.isRunningOutput)
        let hasTarget = target != nil
        // ロック条件は「対象が無いとき」と「乗り換えるとき」で変える。
        //
        // - 対象が無いとき: 音声プロセスが 1 つでもあれば **出力中を待たずに即ロック**する。
        //   無音のプロセスをタップしても害はなく、再生が始まった瞬間からフレームが来る。
        //   出力中を待つ実装では、一時停止中のタブを開いた状態で字幕を開始すると
        //   再生を押すまで何も起きず、実測で 34 秒間なにも表示されなかった。
        // - 既に対象があるとき: 出力中でなければ乗り換えない。Finder や Warp に一瞬
        //   フォーカスしただけで字幕が途切れるのを防ぐための安定化（本来の目的）。
        // 同じアプリが対象のまま増減しただけなら、出力中かどうかに関係なく取り込む。
        // 再生開始時にヘルパプロセスが後から生まれるため（実測: Safari が 1 個 → 2 個に増え、
        // ここで弾いてしまうと音を出す方のプロセスを掴み損ねて何も認識できなくなった）。
        let isSameApp = target?.pid == candidate.pid
        let canLock = (hasTarget && !isSameApp) ? isEmitting : !processes.isEmpty
        logger.notice(
            """
            対象候補=\(candidate.name, privacy: .public) 音声プロセス=\(processes.count, privacy: .public) \
            出力中=\(isEmitting ? "はい" : "いいえ", privacy: .public) \
            同一アプリ=\(isSameApp ? "はい" : "いいえ", privacy: .public) \
            ロック=\(canLock ? "する" : "しない", privacy: .public)
            """
        )
        guard canLock else {
            // 音を出していないアプリ（Finder 等）へ移った：直前の対象を維持する
            if let kept = target {
                logger.notice(
                    "\(candidate.name, privacy: .public) は音を出していないため対象を維持します 対象=\(kept.name, privacy: .public)"
                )
            } else {
                logger.notice(
                    "対象候補 \(candidate.name, privacy: .public) はまだ音声プロセスを持っていません（再生開始を待ちます）"
                )
                // 対象が 1 つも無い間は「何も拾わない」タップにする（裏の音楽を拾わないため）
                notifyWaiting(candidate.name)
                applyIfChanged(objectIDs: [], target: nil, reason: reason)
            }
            return
        }

        let objectIDs = processes.map(\.objectID).sorted()
        let newTarget = CaptureTarget(pid: candidate.pid, name: candidate.name, processes: processes)
        notifyWaiting(nil)
        applyIfChanged(objectIDs: objectIDs, target: newTarget, reason: reason)
    }

    /// 待機状態を通知する（内容が変わったときだけ）
    ///
    /// 評価は音声プロセス一覧が変わるたびに走るので、そのまま通知すると同じ案内を
    /// 何度も出してしまう。
    ///
    /// - Parameter name: 待っている対象アプリ名。ロックできたら nil
    private func notifyWaiting(_ name: String?) {
        dispatchPrecondition(condition: .onQueue(queue))
        if let last = lastWaitingName, last == name { return }
        lastWaitingName = .some(name)
        onWaitingForAudio?(name)
    }

    /// 対象が変わっていればタップの張り替えを依頼する
    private func applyIfChanged(objectIDs: [AudioObjectID], target newTarget: CaptureTarget?, reason: String) {
        dispatchPrecondition(condition: .onQueue(queue))
        let previousName = target?.name
        currentTarget.withLock { $0 = newTarget }
        guard objectIDs != appliedObjectIDs else { return }
        appliedObjectIDs = objectIDs
        logger.notice(
            """
            キャプチャ対象を更新しました 理由=\(reason, privacy: .public) \
            対象=\(newTarget?.name ?? "（なし）", privacy: .public) \
            前=\(previousName ?? "（なし）", privacy: .public) \
            プロセス=\(newTarget?.processes.map(\.description).joined(separator: ",") ?? "", privacy: .public)
            """
        )
        onScopeChanged?(.processes(objectIDs), newTarget)
    }

    /// 対象にすべきアプリ（テスト指定があればそちら、無ければ最前面アプリ）
    private func resolveCandidate() -> (pid: pid_t, name: String)? {
        if let fixedPID {
            let name = NSRunningApplication(processIdentifier: fixedPID)?.localizedName ?? "pid \(fixedPID)"
            return (fixedPID, name)
        }
        guard let app = NSWorkspace.shared.frontmostApplication else { return nil }
        // 自分が最前面になっても対象を自分に移さない（自分の読み上げを拾わないため）
        guard app.processIdentifier != getpid() else { return nil }
        return (app.processIdentifier, app.localizedName ?? "pid \(app.processIdentifier)")
    }

    /// 指定アプリが出している音声プロセスを集める
    ///
    /// 1) 責任 PID が一致するもの（Chrome Helper → Google Chrome）
    /// 2) バンドル ID の前方一致（責任 PID を引けない環境向けの保険）
    ///
    /// - Parameter appPID: 最前面アプリの PID
    /// - Returns: 対象にする音声プロセス（見つからなければ空）
    private func audioProcesses(for appPID: pid_t) -> [AudioOutputProcess] {
        let appBundleID = NSRunningApplication(processIdentifier: appPID)?.bundleIdentifier
        return readAudioProcesses().filter { process in
            guard process.pid > 0, process.pid != getpid() else { return false }
            if process.pid == appPID { return true }
            if responsiblePID(for: process.pid) == appPID { return true }
            if let appBundleID, !appBundleID.isEmpty, !process.bundleID.isEmpty,
               process.bundleID == appBundleID || process.bundleID.hasPrefix(appBundleID + ".") {
                return true
            }
            return false
        }
    }
}
