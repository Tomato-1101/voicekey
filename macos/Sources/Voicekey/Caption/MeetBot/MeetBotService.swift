/// Google Meet 議事録ボット
///
/// 会議 URL を渡すと、**専用プロファイルの Chrome を裏で起動して会議に参加し**、
/// 文字起こしを議事録として保存する。
///
/// **文字起こしの取り方は 2 つあり、切り替えられる**（`CaptionSettings.meetTranscriptSource`）:
/// - `meetCaptions`（**既定**・2026-08-31 ユーザー判断「Google Meet にもともとあるのか。じゃあそれで」）:
///   Meet の内蔵字幕を読む。**話者名が取れる**のが最大の利点で、Mac 側の音声経路にも触らない。
///   認識そのものは Google 側で行われる。Chrome は `--mute-audio` で起こし、会議の音を Mac から鳴らさない。
/// - `localSpeech`（2026-08-28 の「全部ローカルで」に対応した経路。残してある）:
///   Chrome が鳴らしている会議音声を **ボットの Chrome のプロセスの木に限定したシステム音声タップ**で拾い、
///   Apple のオンデバイス認識にかける。音声もテキストも外へ出ないが、話者名は
///   会議画面から読めたときだけ添える。
///   ヘッドレスの Chrome でも音は出る（実測 RMS 0.21 / 156,775 フレーム・`--meetbot-audio-test`）。
///
/// **Chrome の使い方**: 普段使いの Chrome とは別の `--user-data-dir` を使う。作業中のタブを
/// 巻き込まないためと、ボットのログイン状態を分けて持てるようにするため。初回だけは
/// `showLoginWindow()` で見える Chrome を出し、本人に Google ログインしてもらう
/// （ログイン操作は本人にしかできない）。カメラとマイクは Chrome 側で拒否し、オフのまま入る。
///
/// **参加の試行には世代番号を振る**。退出や再参加のあとに古い試行の続き（await の先）が
/// 戻ってきても、世代が違えば何もせずに捨てる。
import AppKit
import Foundation
import OSLog
import os

/// ボットの状態
enum MeetBotState: Equatable {
    /// 何もしていない
    case idle
    /// Chrome の起動・接続中
    case launching
    /// 会議に入ろうとしている（ログイン確認・ボタン操作など）
    case joining(String)
    /// ホストの承認を待っている
    case waitingForApproval
    /// 参加して文字起こしを記録している
    case recording(String)
    /// 会議が終わった・外された（理由）
    case ended(String)
    /// 失敗して止まった
    case failed(String)

    /// メニューに出す 1 行
    var menuTitle: String {
        switch self {
        case .idle: return "参加していません"
        case .launching: return "ブラウザを準備中…"
        case let .joining(detail): return "参加の準備中 — \(detail)"
        case .waitingForApproval: return "ホストの承認待ち"
        case let .recording(name): return "参加中 — \(name)"
        case let .ended(reason): return "終了 — \(reason)"
        case let .failed(message): return "エラー — \(message)"
        }
    }

    /// 動作中扱いか（メニューの「参加/退出」の出し分けに使う）
    var isActive: Bool {
        switch self {
        case .idle, .ended, .failed: return false
        case .launching, .joining, .waitingForApproval, .recording: return true
        }
    }
}

/// ボット用ブラウザの Google ログイン状態
enum MeetBotLoginStatus: Equatable {
    /// まだ確かめていない
    case unknown
    /// 確認中
    case checking
    /// ログイン済み
    case loggedIn
    /// ログインしていない
    case loggedOut

    /// 設定画面に出す表示名
    var title: String {
        switch self {
        case .unknown: return "未確認"
        case .checking: return "確認中…"
        case .loggedIn: return "ログイン済み"
        case .loggedOut: return "ログインしていません"
        }
    }
}

/// 参加の試行の世代（古い試行の続きを捨てるため）
///
/// `begin` で新しい試行を始め、`invalidate` で今の試行を無効にする（退出・終了・アプリ終了）。
/// 非同期の続きは `isCurrent` で自分がまだ有効かを確かめてから状態に触る。
struct JoinGeneration {
    /// いま有効な世代
    private(set) var current = 0

    /// 新しい試行を始めて、その世代を返す
    mutating func begin() -> Int {
        current += 1
        return current
    }

    /// 今の試行を無効にする（以後、その世代の続きは何もしない）
    mutating func invalidate() {
        current += 1
    }

    /// その世代がまだ有効か
    func isCurrent(_ generation: Int) -> Bool {
        generation == current
    }
}

/// ボット固有の失敗（通知の文言を出し分けるため種類を持つ）
enum MeetBotError: Error, CustomStringConvertible, Equatable {
    /// 退出・再参加で古くなった試行（表には出さない）
    case superseded
    /// ボット用ブラウザが Google にログインしていない
    case notLoggedIn
    /// 参加を断られた
    case denied
    /// 会議から外された
    case removed
    /// 会議がもう終わっていた
    case callEnded
    /// 承認を待ちきれなかった
    case approvalTimeout
    /// 会議画面にたどり着けなかった
    case couldNotEnter
    /// DevTools のポートを別のアプリが使っている
    case portBusy(Int)

    var description: String {
        switch self {
        case .superseded: return "取り消されました"
        case .notLoggedIn: return "ボット用ブラウザが Google にログインしていません。設定の「議事録・会議」でログインしてください"
        case .denied: return "参加を断られました（ホストが拒否したか、外部の参加を受け付けない会議です）"
        case .removed: return "会議から外されました"
        case .callEnded: return "会議はすでに終わっています"
        case .approvalTimeout: return "10 分待っても参加を承認されませんでした"
        case .couldNotEnter: return "会議に入れませんでした（URL が違うか、Meet の画面が変わった可能性があります）"
        case let .portBusy(port): return "ポート \(port) を別のアプリが使っています（Chrome のリモートデバッグなど）"
        }
    }
}

/// Meet に入って会議の文字起こしを議事録に残すボット
@available(macOS 26.0, *)
@MainActor
final class MeetBotService: ObservableObject {

    private let logger = makeCaptionLogger("MeetBot")

    /// DevTools のポート（普段使いの Chrome と衝突しない値にしておく）
    static let devToolsPort = 9333

    /// 状態が変わったときに流す通知（メニューバーの親項目の表示を更新するため）
    static let stateDidChangeNotification = Notification.Name("MeetBotService.stateDidChange")

    /// 会議で表示されるボットの名前（ゲスト参加のときに入力される）
    private static let botDisplayName = "voicekey 議事録ボット"

    /// 「いま誰が話しているか」を見にいく間隔
    ///
    /// 話者は発話中に何度も入れ替わるので短めに。DOM を 1 回読むだけなので負荷は軽い。
    private static let speakerPollInterval: TimeInterval = 0.8

    /// 字幕を読みにいく間隔
    ///
    /// Meet の字幕は書きながら伸びるので、短すぎると同じ文を何度も見ることになる。
    /// 1 秒あれば伸び方を追える。
    private static let captionPollInterval: TimeInterval = 1.0

    /// この秒数だけ更新が止まったら、その発言を確定として記録する
    private static let settleSeconds: TimeInterval = 2.5

    /// 会議が続いているかを確かめる間隔
    private static let statusPollInterval: TimeInterval = 3.0

    /// 会議画面でない状態がこの回数続いたら「会議が終わった」とみなす
    ///
    /// 1 回だけだと画面の切り替わり途中を拾って誤判定する（約 9 秒続いたら確定）。
    private static let notInMeetingLimit = 3

    /// ログイン状態の保存キー（次に起動したときも設定画面に出せるように）
    private enum DefaultsKey {
        static let loggedIn = "meetBotLoggedIn"
        static let checkedAt = "meetBotLoginCheckedAt"
    }

    /// ボット専用の Chrome プロファイル
    static var profileDirectory: URL {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return support.appendingPathComponent("voicekey/meetbot-profile", isDirectory: true)
    }

    private var chrome: Process?
    /// いまの Chrome がログイン用の見えるウィンドウか（参加時は見えない Chrome で起こし直す）
    private var chromeIsVisible = false
    private var devTools: ChromeDevTools?
    private var pipeline: CapturePipeline?
    private var speakerTimer: Timer?
    private var captionTimer: Timer?
    private var statusTimer: Timer?
    private let recorder = TranscriptRecorder()
    private let notifier = MeetBotNotifier()

    /// 参加の試行の世代
    private var generation = JoinGeneration()

    /// ログイン用ウィンドウの見張り（閉じられたらログイン状態を確かめて Chrome を畳む）
    private var loginWatcher: Task<Void, Never>?

    /// 会議画面でない状態が続いた回数
    private var notInMeetingCount = 0

    /// この会議の議事録ファイルを作ったか（保存オフのまま入ったときはファイルを作らない）
    private var meetingFileStarted = false

    /// 字幕の確定判定（Meet 字幕モードでだけ使う）
    private var settleTracker = CaptionSettleTracker(settleSeconds: MeetBotService.settleSeconds)

    /// 記録中の会議名（議事録のヘッダに残す）
    private var meetingName = "Google Meet"
    /// 会議コード（議事録のファイル名に入れる）
    private var meetingCode: String?
    /// この会議で使う文字起こしの取り方と言語（参加した時点の設定で固定する）
    private var activeSource: MeetTranscriptSource = .meetCaptions
    private var activeLanguage: CaptionLanguage = .japanese

    /// いま話している参加者（DOM から拾えたときだけ入る）
    ///
    /// 認識コールバックは音声スレッドから来るので lock 付きで持つ。
    private let speakerNow = OSAllocatedUnfairLock<String?>(initialState: nil)
    /// いま認識中の発話に添える話者（確定したらリセットする）
    private let speakerForCurrentSegment = OSAllocatedUnfairLock<String?>(initialState: nil)

    /// 記録が動いている間の App Nap 抑止
    private var antiNapActivity: NSObjectProtocol?

    /// 状態が変わったときに呼ばれる（メニュー更新用）
    var onStateChange: ((MeetBotState) -> Void)?

    /// 会議の音声を拾い始める直前に呼ばれる
    ///
    /// システム音声タップを二重に張らないため、ライブ字幕が動いていたら止めてもらう
    /// （同じ Chrome の音を字幕とボットの両方が拾うと、議事録が二重になるという実害もある）。
    var onWillStartRecording: (() -> Void)?

    /// 現在の状態
    @Published private(set) var state: MeetBotState = .idle {
        didSet {
            guard state != oldValue else { return }
            onStateChange?(state)
            NotificationCenter.default.post(name: Self.stateDidChangeNotification, object: self)
        }
    }

    /// この会議で議事録に書いた行数
    @Published private(set) var recordedLines = 0
    /// 議事録の保存に失敗していれば、その理由（書けたら消える）
    @Published private(set) var saveError: String?
    /// いま書いている（直近に書いた）議事録ファイル
    @Published private(set) var currentTranscript: URL?
    /// ボット用ブラウザのログイン状態
    @Published private(set) var loginStatus: MeetBotLoginStatus = .unknown
    /// ログイン状態を最後に確かめた時刻
    @Published private(set) var loginCheckedAt: Date?

    /// ボットが会議に関わっている最中か（Chrome の準備から退出まで。承認待ちも含む）
    ///
    /// 読み取り専用。ほかの機能が「いまボットが動いているか」を見るための窓口。
    var isRecording: Bool { state.isActive }

    /// Chrome が使えるか（メニューの有効/無効に使う）
    static var isAvailable: Bool { ChromeDevTools.isChromeInstalled }

    init() {
        let defaults = UserDefaults.standard
        if let checkedAt = defaults.object(forKey: DefaultsKey.checkedAt) as? Date {
            loginCheckedAt = checkedAt
            loginStatus = defaults.bool(forKey: DefaultsKey.loggedIn) ? .loggedIn : .loggedOut
        }
        // 書き込みの結果は記録用のキューから来るので、メインへ戻して画面に出す
        recorder.onWrite = { [weak self] outcome in
            Task { @MainActor [weak self] in self?.handleWrite(outcome) }
        }
    }

    /// 状態を 1 行で（保存オフ・保存失敗・記録行数も含めて出す）
    var statusLine: String {
        guard case let .recording(name) = state else { return state.menuTitle }
        if !CaptionSettings.savesTranscript { return "参加中（議事録の保存はオフ） — \(name)" }
        if let saveError { return "議事録を保存できません — \(saveError)" }
        return "記録中 \(recordedLines) 行 — \(name)"
    }

    // MARK: - 公開操作

    /// 会議に参加して記録を始める
    ///
    /// - Parameter urlString: Meet の会議 URL（`https://meet.google.com/xxx-yyyy-zzz`）
    func join(urlString: String) {
        guard !state.isActive else { return }
        guard ChromeDevTools.isChromeInstalled else {
            state = .failed("Google Chrome が必要です")
            return
        }
        guard let url = Self.normalizedMeetURL(urlString) else {
            state = .failed("Meet の URL ではありません")
            return
        }
        meetingCode = Self.meetingCode(from: url)
        meetingName = meetingCode ?? "Google Meet"
        activeSource = CaptionSettings.meetTranscriptSource
        activeLanguage = CaptionSettings.meetBotLanguage
        recordedLines = 0
        saveError = nil
        currentTranscript = nil
        meetingFileStarted = false
        notInMeetingCount = 0
        let current = generation.begin()
        loginWatcher?.cancel()
        loginWatcher = nil
        notifier.prepare()
        ActionLog.shared.write(
            "caption.MeetBot",
            "Meet ボット開始 (会議=\(meetingName), 文字起こし=\(activeSource.rawValue), 言語=\(activeLanguage.rawValue))"
        )
        state = .launching
        beginAntiNap()

        Task { [weak self] in
            guard let self else { return }
            do {
                try await self.startChromeAndJoin(url: url, generation: current)
            } catch {
                // 退出・再参加で古くなった試行は黙って捨てる（新しい試行の状態を壊さない）
                guard self.generation.isCurrent(current) else { return }
                let message = String(describing: error)
                self.logger.error("会議への参加に失敗: \(message, privacy: .public)")
                ActionLog.shared.write("caption.MeetBot", "Meet ボット参加失敗: \(message)")
                if error as? MeetBotError == .notLoggedIn { self.setLoginStatus(false) }
                self.finish(.failed(message))
                if error as? MeetBotError == .denied {
                    self.notifier.post("参加を断られました。ホストに承認してもらうか、会議の URL を確かめてください。")
                } else {
                    self.notifier.post("会議に参加できませんでした: \(message)")
                }
            }
        }
    }

    /// 会議から退出して記録を閉じる
    func leave() {
        guard state.isActive else { return }
        ActionLog.shared.write("caption.MeetBot", "Meet ボット終了（退出）")
        let wasRecording = { if case .recording = state { return true } else { return false } }()
        finish(.idle, leaveMeetingFirst: true)
        if wasRecording { notifyClosed(reason: "会議から退出しました") }
    }

    /// 初回ログイン用に、見える Chrome を開く
    ///
    /// Google のログインは本人にしかできない。ボット用プロファイルで一度ログインしておくと、
    /// 以後は自分の会議へ承認なしで入れる。ウィンドウが閉じられたらログインできたかを確かめ、
    /// Chrome を畳む（参加するときは見えない Chrome で起こし直す）。
    ///
    /// ログイン画面は **DevTools を経由せず** URL を起動引数で渡して開く（2026-09-08）。
    /// 以前は「Chrome 起動 → DevTools 応答待ち → `/json/new`」の 3 段だったが、
    /// どこかで止まると**画面に何も出ないまま黙る**（メニューを押しても反応が無いように見える）。
    /// 起動引数なら Chrome が自分で最初のタブに開くので、途中で黙る段が無い。
    func showLoginWindow() {
        guard !state.isActive else { return }
        guard ChromeDevTools.isChromeInstalled else {
            state = .failed("Google Chrome が必要です")
            return
        }
        logger.notice("ログイン用の Chrome を開きます")
        ActionLog.shared.write("caption.MeetBot", "ボット用ブラウザで Google のログイン画面を開く")
        loginWatcher?.cancel()
        let previous = chrome
        previous?.terminationHandler = nil
        chrome = nil
        chromeIsVisible = false
        devTools?.close()
        devTools = nil
        if case .failed = state { state = .idle }
        loginWatcher = Task { [weak self] in
            // 直前まで裏で動いていた（見えない）Chrome が同じプロファイルを掴んだままだと、
            // 新しく起こした Chrome はそちらへ URL を渡して即終了し、画面には何も出ない。
            // 完全に終わるのを待ってから起こす。前回の取り残しも同じ理由で片付ける。
            if let previous { await Self.terminateAndWait(previous) }
            await ChromeDevTools.terminateStaleChrome(profileDirectory: Self.profileDirectory)
            guard let self, !Task.isCancelled else { return }
            do {
                let process = try ChromeDevTools.launchChrome(
                    port: Self.devToolsPort, profileDirectory: Self.profileDirectory,
                    headless: false, initialURL: "https://accounts.google.com/"
                )
                self.chrome = process
                self.chromeIsVisible = true
                self.logger.notice("ログイン用の Chrome を起動しました pid=\(process.processIdentifier)")
                // Process で起こしたアプリは LaunchServices を通らないので前面に出ないことがある
                await Self.activateChrome(pid: process.processIdentifier)
                await self.watchLoginWindow(process)
            } catch {
                let message = String(describing: error)
                self.logger.error("ログイン用の Chrome を起動できません: \(message, privacy: .public)")
                self.state = .failed("ブラウザを開けません: \(message)")
            }
        }
    }

    /// アプリ終了時の後片付け（Chrome を残さない・議事録を書き切る）
    func shutdown() {
        if state.isActive {
            ActionLog.shared.write("caption.MeetBot", "Meet ボット終了（アプリ終了）")
        }
        loginWatcher?.cancel()
        loginWatcher = nil
        flushPendingCaptions()
        recorder.endSession()
        generation.invalidate()
        let pipeline = detachResources(leaveMeetingFirst: false)
        // タップと Aggregate Device は終了前に確実に畳む（残すと HAL にゴーストデバイスが溜まる）
        if let pipeline {
            let semaphore = DispatchSemaphore(value: 0)
            Task.detached {
                await pipeline.stop()
                semaphore.signal()
            }
            _ = semaphore.wait(timeout: .now() + 3.0)
        }
        // キューに残った追記をプロセスと一緒に消さない
        recorder.waitUntilWritten()
    }

    // MARK: - 参加の流れ

    /// Chrome を起動して会議に入り、記録を始めるまで
    ///
    /// await のたびに `ensureCurrent` で世代を確かめる。退出・再参加・アプリ終了のあとに
    /// 古い試行が状態を書き換えたり、記録を始めたりしないようにするため。
    private func startChromeAndJoin(url: URL, generation current: Int) async throws {
        // ログイン用に開いた見える Chrome が残っていれば閉じる（見える窓で会議に入らない）
        if let existing = chrome {
            existing.terminationHandler = nil
            chrome = nil
            chromeIsVisible = false
            await Self.terminateAndWait(existing)
        }
        devTools?.close()
        devTools = nil

        // 前回落ちたときの取り残し（ボット用プロファイルを使う Chrome だけ）を片付ける
        let stale = await ChromeDevTools.terminateStaleChrome(profileDirectory: Self.profileDirectory)
        try ensureCurrent(current)
        if stale > 0 { logger.notice("取り残されていたボット用 Chrome を終了しました（\(stale) 個）") }
        if await ChromeDevTools.isAnswering(port: Self.devToolsPort) {
            throw MeetBotError.portBusy(Self.devToolsPort)
        }
        try ensureCurrent(current)

        let process = try ChromeDevTools.launchChrome(
            port: Self.devToolsPort, profileDirectory: Self.profileDirectory,
            headless: true, muteAudio: activeSource == .meetCaptions
        )
        chrome = process
        process.terminationHandler = { [weak self] _ in
            Task { @MainActor [weak self] in self?.chromeDidExit(generation: current) }
        }
        try await ChromeDevTools.waitForDevTools(port: Self.devToolsPort)
        try ensureCurrent(current)

        // 未ログインのまま会議ページへ行くと紹介サイトへ飛ばされるだけなので、先に確かめる
        state = .joining("ログインを確認しています")
        if let loggedIn = await checkLogin() {
            try ensureCurrent(current)
            guard loggedIn else { throw MeetBotError.notLoggedIn }
        }
        await denyMediaPermissions()
        try ensureCurrent(current)

        let socketURL = try await ChromeDevTools.openTab(url: url.absoluteString, port: Self.devToolsPort)
        try ensureCurrent(current)
        let tools = ChromeDevTools()
        tools.onDisconnect = { [weak self] in
            Task { @MainActor [weak self] in self?.devToolsDidDisconnect(generation: current) }
        }
        tools.connect(to: socketURL)
        devTools = tools

        state = .joining("会議を開いています")
        // ロビー画面が組み上がるまで待つ（DOM は非同期に描かれる）
        try await Task.sleep(for: .seconds(4))
        try ensureCurrent(current)

        // 未ログインのプロファイルでは Cookie 同意が先に挟まる。押すと遷移するので先に片付ける。
        if let consent = try? await tools.evaluate(Self.consentScript),
           let clicked = (consent as? [String: Any])?["clicked"] as? String {
            logger.notice("同意画面を閉じました: \(clicked, privacy: .public)")
            try await Task.sleep(for: .seconds(3))
        }
        try ensureCurrent(current)

        // 未ログインだと Meet は会議コードのページから紹介サイトへ飛ばす（実測: /about → apps.google.com/meet）。
        if let left = try? await tools.evaluate(Self.leftMeetingPageScript) as? Bool, left {
            throw MeetBotError.notLoggedIn
        }
        try ensureCurrent(current)

        state = .joining("参加ボタンを押しています")
        let joinResult = try await tools.evaluate(Self.joinScript(displayName: Self.botDisplayName))
        try ensureCurrent(current)
        let joined = (joinResult as? [String: Any])?["clicked"] as? String
        logger.notice("参加操作: \(joined ?? "ボタンが見つからない", privacy: .public)")

        state = .joining("会議への入室を待っています")
        try await waitUntilInMeeting(tools: tools, generation: current)

        if let title = try? await tools.evaluate("document.title") as? String, !title.isEmpty {
            meetingName = title.replacingOccurrences(of: " - Google Meet", with: "")
        }
        // Chrome 側で拒否しているので通常は何もしない。オンで入っていたら切る。
        if let unmuted = try? await tools.evaluate(Self.ensureMutedScript) as? Int, unmuted > 0 {
            logger.notice("会議に入った時点でオンだったマイク/カメラを切りました（\(unmuted) 個）")
        }
        try ensureCurrent(current)

        var captionsOn = true
        switch activeSource {
        case .meetCaptions:
            state = .joining("字幕をオンにしています")
            captionsOn = await enableCaptions(tools: tools, generation: current)
            try ensureCurrent(current)
            if captionsOn {
                let result = (try? await tools.evaluate(Self.captionLanguageScript(activeLanguage)) as? String) ?? "error"
                logger.notice("字幕の言語: \(result, privacy: .public)")
                try ensureCurrent(current)
            }
            startMeetingFileIfNeeded()
            state = .recording(meetingName)
            startCaptionPolling()
        case .localSpeech:
            try await startRecognition(generation: current)
            try ensureCurrent(current)
            startMeetingFileIfNeeded()
            state = .recording(meetingName)
            startSpeakerPolling()
        }
        startStatusPolling()
        ActionLog.shared.write("caption.MeetBot", "Meet ボット入室・記録開始")

        if !CaptionSettings.savesTranscript {
            notifier.post("会議に参加しました。議事録の保存はオフなので、何も保存しません。")
        } else if !captionsOn {
            notifier.post("会議に参加しましたが、Meet の字幕をオンにできませんでした。議事録に何も残らない可能性があります。")
        } else {
            notifier.post("会議に参加しました（\(meetingName)）。議事録を記録しています。")
        }
    }

    /// 試行がまだ有効かを確かめる（古ければ投げて、続きを打ち切る）
    private func ensureCurrent(_ current: Int) throws {
        guard generation.isCurrent(current) else { throw MeetBotError.superseded }
    }

    /// 会議に入れるまで待つ（承認待ち・拒否・終了を見分ける）
    ///
    /// 承認待ちに入ったら待ち時間を 10 分に延ばし、通知で知らせる。
    private func waitUntilInMeeting(tools: ChromeDevTools, generation current: Int) async throws {
        let start = Date()
        var deadline = start.addingTimeInterval(90)
        var waitingNotified = false
        while true {
            try ensureCurrent(current)
            if let raw = try? await tools.evaluate(Self.pageStatusScript) as? [String: Any] {
                try ensureCurrent(current)
                let status = MeetBotRules.pageStatus(
                    inMeeting: (raw["inMeeting"] as? Bool) ?? false, text: (raw["text"] as? String) ?? ""
                )
                switch status {
                case .inMeeting:
                    return
                case .waitingApproval:
                    if !waitingNotified {
                        waitingNotified = true
                        deadline = start.addingTimeInterval(600)
                        state = .waitingForApproval
                        ActionLog.shared.write("caption.MeetBot", "Meet ボット承認待ち")
                        notifier.post("ホストの承認を待っています。会議の画面でボットの参加を承認してください。")
                    }
                case .denied: throw MeetBotError.denied
                case .removed: throw MeetBotError.removed
                case .ended: throw MeetBotError.callEnded
                case .other: break
                }
            }
            guard Date() < deadline else {
                throw waitingNotified ? MeetBotError.approvalTimeout : MeetBotError.couldNotEnter
            }
            try? await Task.sleep(for: .seconds(2))
        }
    }

    /// Meet の字幕をオンにする（既にオンなら触らない）
    ///
    /// 状態を読んでから押す。押した後も読み直し、オンになったのを確かめる（試行は 4 回まで）。
    ///
    /// - Returns: オンにできた（オンだと確かめられた）か
    private func enableCaptions(tools: ChromeDevTools, generation current: Int) async -> Bool {
        var shortcutTried = false
        for _ in 0..<4 {
            guard generation.isCurrent(current) else { return false }
            if let raw = try? await tools.evaluate(Self.captionStateScript) as? [String: Any] {
                let buttons = ((raw["buttons"] as? [[String: Any]]) ?? []).map {
                    MeetCaptionButton(label: ($0["label"] as? String) ?? "", pressed: $0["pressed"] as? Bool)
                }
                let decision = MeetBotRules.captionToggle(
                    buttons: buttons, regionVisible: (raw["regionVisible"] as? Bool) ?? false,
                    shortcutTried: shortcutTried
                )
                switch decision {
                case .alreadyOn:
                    logger.notice("Meet の字幕はオンです")
                    return true
                case let .click(label):
                    logger.notice("字幕ボタンを押します: \(label, privacy: .public)")
                    _ = try? await tools.evaluate(Self.clickCaptionButtonScript(label))
                case .shortcut:
                    logger.notice("字幕ボタンが見つからないのでショートカットを 1 回だけ送ります")
                    shortcutTried = true
                    _ = try? await tools.evaluate(Self.captionShortcutScript)
                case .unknown:
                    logger.error("Meet の字幕の状態が分かりません")
                    return false
                }
            }
            try? await Task.sleep(for: .seconds(1.5))
        }
        return false
    }

    // MARK: - ログイン

    /// ボット用ブラウザが Google にログインしているかを Cookie で確かめる
    ///
    /// Cookie は名前と期限だけを見る（値は読まない・ログにも出さない）。
    ///
    /// - Returns: ログイン済みか（確かめられなければ nil）
    private func checkLogin() async -> Bool? {
        let previous = loginStatus
        loginStatus = .checking
        do {
            let socketURL = try await ChromeDevTools.browserSocketURL(port: Self.devToolsPort)
            let browser = ChromeDevTools()
            browser.connect(to: socketURL)
            defer { browser.close() }
            let result = try await browser.send("Storage.getCookies")
            let cookies = (result["cookies"] as? [[String: Any]]) ?? []
            let loggedIn = MeetBotRules.isLoggedIn(cookies: cookies)
            setLoginStatus(loggedIn)
            logger.notice("ボット用ブラウザのログイン状態: \(loggedIn ? "ログイン済み" : "未ログイン", privacy: .public)")
            return loggedIn
        } catch {
            loginStatus = previous
            logger.error("ログイン状態を確かめられません: \(String(describing: error), privacy: .public)")
            return nil
        }
    }

    /// ログイン状態を記録する（次回の起動でも設定画面に出せるよう保存する）
    private func setLoginStatus(_ loggedIn: Bool) {
        let now = Date()
        loginStatus = loggedIn ? .loggedIn : .loggedOut
        loginCheckedAt = now
        UserDefaults.standard.set(loggedIn, forKey: DefaultsKey.loggedIn)
        UserDefaults.standard.set(now, forKey: DefaultsKey.checkedAt)
    }

    /// ログイン用ウィンドウを見張る
    ///
    /// ウィンドウが全部閉じられた（ページが 0 になった）か Chrome が終わったら、
    /// ログインできたかを確かめて Chrome を畳む。見張りは 30 分で打ち切る（開きっぱなしで放置されても回り続けない）。
    private func watchLoginWindow(_ process: Process) async {
        var sawPage = false
        var round = 0
        let deadline = Date().addingTimeInterval(30 * 60)
        while !Task.isCancelled, process.isRunning, Date() < deadline {
            try? await Task.sleep(for: .seconds(2))
            guard let pages = try? await ChromeDevTools.listPageURLs(port: Self.devToolsPort) else { continue }
            if pages.isEmpty {
                if sawPage { break }
                continue
            }
            sawPage = true
            round += 1
            // ログインが済んだら設定画面にすぐ出す（6 秒ごと・済んだら確認をやめる）
            if round % 3 == 0, loginStatus != .loggedIn { _ = await checkLogin() }
        }
        guard !Task.isCancelled, chrome === process else { return }
        if process.isRunning {
            _ = await checkLogin()
            guard !Task.isCancelled, chrome === process else { return }
            chrome = nil
            chromeIsVisible = false
            await Self.terminateAndWait(process)
        } else {
            chrome = nil
            chromeIsVisible = false
        }
        logger.notice("ログイン用の Chrome を閉じました")
    }

    /// Chrome にカメラとマイクを使わせない（会議にはオフのまま入る）
    ///
    /// 起動引数の `--deny-permission-prompts` に加えて、Meet の画面に対して明示的に拒否しておく。
    /// 失敗しても参加は続ける（引数側で拒否が効いているため）。
    private func denyMediaPermissions() async {
        guard let socketURL = try? await ChromeDevTools.browserSocketURL(port: Self.devToolsPort) else { return }
        let browser = ChromeDevTools()
        browser.connect(to: socketURL)
        defer { browser.close() }
        for name in ["camera", "microphone"] {
            _ = try? await browser.send(
                "Browser.setPermission",
                ["permission": ["name": name], "setting": "denied", "origin": "https://meet.google.com"]
            )
        }
    }

    // MARK: - 会議の終わりに気づく

    /// 会議が続いているかの見張りを始める
    private func startStatusPolling() {
        statusTimer?.invalidate()
        notInMeetingCount = 0
        let timer = Timer.scheduledTimer(withTimeInterval: Self.statusPollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.pollStatus() }
        }
        RunLoop.main.add(timer, forMode: .common)
        statusTimer = timer
    }

    /// 会議画面かを 1 回確かめる（外された・終わった・画面から消えたら記録を閉じる）
    private func pollStatus() async {
        guard let tools = devTools, case .recording = state else { return }
        let current = generation.current
        guard let raw = try? await tools.evaluate(Self.pageStatusScript) as? [String: Any] else { return }
        guard generation.isCurrent(current), case .recording = state else { return }
        let status = MeetBotRules.pageStatus(
            inMeeting: (raw["inMeeting"] as? Bool) ?? false, text: (raw["text"] as? String) ?? ""
        )
        switch status {
        case .inMeeting:
            notInMeetingCount = 0
        case .removed:
            endMeeting(reason: "会議から外されました")
        case .denied, .ended, .waitingApproval, .other:
            notInMeetingCount += 1
            if notInMeetingCount >= Self.notInMeetingLimit {
                endMeeting(reason: "会議が終了しました")
            }
        }
    }

    /// 会議の終わりを受けて記録を閉じる
    private func endMeeting(reason: String) {
        ActionLog.shared.write("caption.MeetBot", "Meet ボット終了（\(reason)）")
        logger.notice("会議の終了を検出: \(reason, privacy: .public)")
        finish(.ended(reason))
        notifyClosed(reason: reason)
    }

    /// Chrome が終わった（落ちた・外から終了された）
    private func chromeDidExit(generation current: Int) {
        guard generation.isCurrent(current), state.isActive else { return }
        failUnexpectedly("ブラウザが終了しました")
    }

    /// DevTools の接続が向こうから切れた
    private func devToolsDidDisconnect(generation current: Int) {
        guard generation.isCurrent(current), state.isActive else { return }
        failUnexpectedly("ブラウザとの接続が切れました")
    }

    /// 記録中に止まってしまったときの後始末
    private func failUnexpectedly(_ reason: String) {
        let wasRecording = { if case .recording = state { return true } else { return false } }()
        ActionLog.shared.write("caption.MeetBot", "Meet ボット停止（\(reason)）")
        logger.error("Meet ボットが止まりました: \(reason, privacy: .public)")
        finish(.failed(reason))
        if wasRecording {
            notifyClosed(reason: reason)
        } else {
            notifier.post("会議に参加できませんでした: \(reason)")
        }
    }

    /// 記録を閉じたことを知らせる（何行残ったか・どこに保存したか）
    private func notifyClosed(reason: String) {
        if !CaptionSettings.savesTranscript || !meetingFileStarted {
            notifier.post("\(reason)。議事録の保存はオフでした。")
        } else {
            notifier.post("\(reason)。議事録を保存しました（\(recordedLines) 行）。")
        }
    }

    // MARK: - Meet の内蔵字幕を読む（既定）

    /// 字幕のポーリングを始める
    private func startCaptionPolling() {
        captionTimer?.invalidate()
        let timer = Timer.scheduledTimer(withTimeInterval: Self.captionPollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.pollCaptions() }
        }
        // 会議中はメニュー操作などで RunLoop のモードが変わるので common に載せる
        RunLoop.main.add(timer, forMode: .common)
        captionTimer = timer
    }

    /// 字幕を 1 回読んで、確定した発言を記録する
    ///
    /// 伸びている途中か言い終わったかの判定は `CaptionSettleTracker` に任せる
    /// （純粋なロジックなのでブラウザ無しで回帰テストできる）。
    private func pollCaptions() async {
        guard let tools = devTools, case .recording = state else { return }
        let current = generation.current
        guard let raw = try? await tools.evaluate(Self.captionScript),
              let payload = raw as? [String: Any],
              let rows = payload["entries"] as? [[String: Any]] else { return }
        // 読んでいる間に退出・終了していたら書かない
        guard generation.isCurrent(current), case .recording = state else { return }

        let entries = rows.map {
            CaptionSettleTracker.Entry(
                speaker: ($0["speaker"] as? String) ?? "", text: ($0["text"] as? String) ?? ""
            )
        }
        for line in settleTracker.ingest(entries, now: Date()) { recordCaption(line) }
    }

    /// 保留中の発言をすべて書き出す（退出時・終了時に取りこぼさないため）
    private func flushPendingCaptions() {
        for line in settleTracker.flush() { recordCaption(line) }
    }

    /// 字幕 1 件を議事録へ書く（本文はログに出さない）
    private func recordCaption(_ line: CaptionSettleTracker.Line) {
        guard CaptionSettings.savesTranscript else { return }
        startMeetingFileIfNeeded()
        recorder.record(
            text: line.text,
            speaker: line.speaker,
            context: "Google Meet / \(meetingName)",
            language: transcriptLanguageLabel
        )
    }

    /// この会議の議事録ファイルを作る（保存オンのときだけ・1 会議 1 回）
    ///
    /// 保存オフで入った後でオンにしたときも、ここで会議用のファイルが作られる。
    private func startMeetingFileIfNeeded() {
        guard CaptionSettings.savesTranscript, !meetingFileStarted else { return }
        meetingFileStarted = true
        recorder.beginMeeting(
            code: meetingCode, context: "Google Meet / \(meetingName)", language: transcriptLanguageLabel
        )
    }

    /// 議事録のヘッダに書く言語
    private var transcriptLanguageLabel: String {
        switch activeSource {
        case .meetCaptions: return "Meet の字幕（\(activeLanguage.displayName)）"
        case .localSpeech: return activeLanguage.displayName
        }
    }

    /// 議事録の書き込み結果を受ける
    private func handleWrite(_ outcome: TranscriptRecorder.WriteOutcome) {
        switch outcome {
        case let .written(url, lines):
            currentTranscript = url
            recordedLines = lines
            saveError = nil
        case let .failed(message):
            // 失敗は会議中に 1 回だけ知らせる（毎行ごとに通知を出さない）
            if saveError == nil {
                notifier.post("議事録を保存できません: \(message)")
            }
            saveError = message
        }
    }

    // MARK: - ローカル文字起こし

    /// ボットの Chrome が鳴らしている音声を拾って、オンデバイス認識を始める
    ///
    /// 対象は **ボットの Chrome とその子プロセスだけ**（最前面の追従はしない・同じバンドル ID の
    /// 普段使いの Chrome は拾わない）。会議中に他のアプリを触っても、拾うのは会議の音だけになる。
    private func startRecognition(generation current: Int) async throws {
        guard let chromePID = chrome?.processIdentifier else {
            throw ChromeDevToolsError.protocolError("Chrome のプロセスが見つかりません")
        }
        // 同じ音を字幕とボットの両方で拾わないよう、先に字幕を止めてもらう
        onWillStartRecording?()

        state = .joining("音声認識を準備しています")
        let language = activeLanguage
        let pipeline = CapturePipeline(locale: language.locale, fixedPID: chromePID, processTreeOnly: true)
        pipeline.onSegment = { [weak self] segment in
            guard let self else { return }
            if segment.isFinal {
                self.recordFinal(segment.text)
            } else {
                // 発話中に見えている話者を覚えておく（確定時にはもう切り替わっていることがある）
                if let speaker = self.speakerNow.withLock({ $0 }) {
                    self.speakerForCurrentSegment.withLock { $0 = speaker }
                }
            }
        }
        try await pipeline.start()
        // 準備している間に退出していたら、張ったタップをすぐ畳む（残すと HAL にゴーストが溜まる）
        guard generation.isCurrent(current) else {
            await pipeline.stop()
            throw MeetBotError.superseded
        }
        self.pipeline = pipeline
        logger.notice("会議音声のローカル認識を開始 pid=\(chromePID, privacy: .public) locale=\(language.rawValue, privacy: .public)")
    }

    /// 確定した文字起こしを議事録へ書く（本文はログに出さない）
    ///
    /// - Parameter text: 認識テキスト
    nonisolated private func recordFinal(_ text: String) {
        let speaker = speakerForCurrentSegment.withLock { value -> String? in
            let current = value
            value = nil
            return current
        } ?? speakerNow.withLock { $0 }
        Task { @MainActor [weak self] in
            guard let self, case .recording = self.state, CaptionSettings.savesTranscript else { return }
            self.startMeetingFileIfNeeded()
            self.recorder.record(
                text: text,
                speaker: speaker,
                context: "Google Meet / \(self.meetingName)",
                language: self.transcriptLanguageLabel
            )
        }
    }

    // MARK: - 話者名（ベストエフォート）

    /// 「いま誰が話しているか」を定期的に読む
    ///
    /// **文字起こしには使わない**（それはローカル認識の担当）。議事録の行頭に添える名前を
    /// 取るためだけに Meet の DOM を見る。取れなくても記録は続く（話者名なしの行になる）。
    private func startSpeakerPolling() {
        speakerTimer?.invalidate()
        let timer = Timer.scheduledTimer(withTimeInterval: Self.speakerPollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.pollSpeaker() }
        }
        RunLoop.main.add(timer, forMode: .common)
        speakerTimer = timer
    }

    /// 話者を 1 回読む
    private func pollSpeaker() async {
        guard let tools = devTools, case .recording = state else { return }
        guard let value = try? await tools.evaluate(Self.activeSpeakerScript) else { return }
        let name = (value as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let name, !name.isEmpty else { return }
        speakerNow.withLock { $0 = name }
    }

    // MARK: - 後片付け

    /// 記録を閉じて Chrome・タップ・タイマーを畳み、状態を移す
    ///
    /// - Parameters:
    ///   - newState: 移る先の状態
    ///   - leaveMeetingFirst: 「通話から退出」を押してから Chrome を閉じるか（自分で退出したとき）
    private func finish(_ newState: MeetBotState, leaveMeetingFirst: Bool = false) {
        flushPendingCaptions()
        recorder.endSession()
        generation.invalidate()
        let pipeline = detachResources(leaveMeetingFirst: leaveMeetingFirst)
        // タップと Aggregate Device は必ず畳む（残すと HAL にゴーストデバイスが溜まる）
        if let pipeline { Task { await pipeline.stop() } }
        state = newState
    }

    /// Chrome・接続・タイマーを手放す（すぐ再参加しても新しい Chrome を巻き込まないよう、先に参照を外す）
    ///
    /// - Parameter leaveMeetingFirst: 退出ボタンを押してから閉じるか
    /// - Returns: 止める必要のある音声パイプライン（呼び出し側が止め方を選ぶ）
    private func detachResources(leaveMeetingFirst: Bool) -> CapturePipeline? {
        speakerTimer?.invalidate()
        speakerTimer = nil
        captionTimer?.invalidate()
        captionTimer = nil
        statusTimer?.invalidate()
        statusTimer = nil
        _ = settleTracker.flush()
        let pipeline = self.pipeline
        self.pipeline = nil
        let tools = devTools
        devTools = nil
        tools?.onDisconnect = nil
        let process = chrome
        chrome = nil
        chromeIsVisible = false
        process?.terminationHandler = nil
        speakerNow.withLock { $0 = nil }
        speakerForCurrentSegment.withLock { $0 = nil }
        endAntiNap()

        if leaveMeetingFirst, let tools {
            Task {
                // 退出ボタンを押して、Meet が離脱を相手に伝える間だけ待ってから閉じる
                let leaving = Task { _ = try? await tools.evaluate(Self.leaveScript) }
                try? await Task.sleep(for: .seconds(1.5))
                tools.close()
                leaving.cancel()
                if let process, process.isRunning { process.terminate() }
            }
        } else {
            tools?.close()
            if let process, process.isRunning { process.terminate() }
        }
        return pipeline
    }

    /// Chrome を終わらせ、終わるまで待つ（上限 5 秒）
    private static func terminateAndWait(_ process: Process) async {
        guard process.isRunning else { return }
        process.terminate()
        let deadline = Date().addingTimeInterval(5)
        while process.isRunning, Date() < deadline {
            try? await Task.sleep(for: .milliseconds(100))
        }
    }

    /// 起動した Chrome を前面に出す（起動直後は NSRunningApplication に載っていないので少し待つ）
    private static func activateChrome(pid: Int32) async {
        let deadline = Date().addingTimeInterval(8)
        while Date() < deadline {
            if let app = NSRunningApplication(processIdentifier: pid), app.isFinishedLaunching {
                app.activate(from: .current, options: [.activateAllWindows])
                return
            }
            try? await Task.sleep(for: .milliseconds(200))
        }
    }

    /// 記録中だけ App Nap を止める
    private func beginAntiNap() {
        guard antiNapActivity == nil else { return }
        antiNapActivity = ProcessInfo.processInfo.beginActivity(
            options: .background, reason: "meet bot recording"
        )
    }

    /// App Nap の抑止を解除する
    private func endAntiNap() {
        guard let activity = antiNapActivity else { return }
        ProcessInfo.processInfo.endActivity(activity)
        antiNapActivity = nil
    }

    // MARK: - URL の扱い

    /// Meet の URL として正しいものだけを通す
    ///
    /// - Parameter raw: 入力文字列（`meet.google.com/abc-defg-hij` のように scheme 無しでも受ける）
    /// - Returns: 正規化した URL（Meet でなければ nil）
    static func normalizedMeetURL(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") { text = "https://" + text }
        guard let url = URL(string: text), let host = url.host else { return nil }
        guard host == "meet.google.com" else { return nil }
        return url
    }

    /// URL から会議コードを取り出す（`abc-defg-hij`）
    static func meetingCode(from url: URL) -> String? {
        let code = url.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return code.isEmpty ? nil : code
    }
}
