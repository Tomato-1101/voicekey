/// Google Meet 議事録ボット
///
/// 会議 URL を渡すと、**専用プロファイルの Chrome を裏で起動して会議に参加し**、
/// その Chrome が鳴らしている会議音声を拾って**この Mac の中だけで文字起こし**して保存する。
///
/// **文字起こしは全部ローカル計算で行う**（2026-08-28 ユーザー指示）。
/// 認識は Apple のオンデバイス音声認識（`SpeechTranscriber`）で、音声もテキストも外へ出さない。
/// 最初の実装では Meet の内蔵字幕（＝ Google 側の認識）を読んでいたが、この指示により
/// **音声を Process Tap で拾ってローカル認識する方式へ差し替えた**。Meet の DOM を読むのは
/// 「いま誰が話しているか」を議事録の話者名に添えるためだけで、文字起こしには使わない。
///
/// **音声の取り方**: voicekey のシステム音声タップ（`CapturePipeline`）を、ボットが起動した
/// Chrome の PID に固定して張る。ヘッドレスの Chrome でも音は出ており、実測で
/// RMS 0.26 / 188,911 フレームを拾えることを確認済み（`--meetbot-audio-test`）。
///
/// **Chrome の使い方**: 普段使いの Chrome とは別の `--user-data-dir` を使う。作業中のタブを
/// 巻き込まないためと、ボットのログイン状態を分けて持てるようにするため。初回だけは
/// `showLoginWindow()` で見える Chrome を出し、本人に Google ログインしてもらう
/// （ログイン操作は本人にしかできない）。
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
    /// 会議に入ろうとしている（承認待ちを含む）
    case joining(String)
    /// 参加して文字起こしを記録している
    case recording(String)
    /// 失敗して止まった
    case failed(String)

    /// メニューに出す 1 行
    var menuTitle: String {
        switch self {
        case .idle: return "参加していません"
        case .launching: return "ブラウザを準備中…"
        case let .joining(detail): return "参加中 — \(detail)"
        case let .recording(name): return "記録中 — \(name)"
        case let .failed(message): return "エラー — \(message)"
        }
    }

    /// 動作中扱いか（メニューの「参加/退出」の出し分けに使う）
    var isActive: Bool {
        switch self {
        case .idle, .failed: return false
        case .launching, .joining, .recording: return true
        }
    }
}

/// Meet に入って会議の音声をローカル認識し、議事録に残すボット
@available(macOS 26.0, *)
@MainActor
final class MeetBotService {

    private let logger = makeCaptionLogger("MeetBot")

    /// DevTools のポート（普段使いの Chrome と衝突しない値にしておく）
    private static let devToolsPort = 9333

    /// 会議で表示されるボットの名前（ゲスト参加のときに入力される）
    private static let botDisplayName = "voicekey 議事録ボット"

    /// 「いま誰が話しているか」を見にいく間隔
    ///
    /// 話者は発話中に何度も入れ替わるので短めに。DOM を 1 回読むだけなので負荷は軽い。
    private static let speakerPollInterval: TimeInterval = 0.8

    /// ボット専用の Chrome プロファイル
    static var profileDirectory: URL {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return support.appendingPathComponent("voicekey/meetbot-profile", isDirectory: true)
    }

    private var chrome: Process?
    private var devTools: ChromeDevTools?
    private var pipeline: CapturePipeline?
    private var speakerTimer: Timer?
    private let recorder = TranscriptRecorder()

    /// 記録中の会議名（議事録のヘッダに残す）
    private var meetingName = "Google Meet"

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
    private(set) var state: MeetBotState = .idle {
        didSet { onStateChange?(state) }
    }

    /// Chrome が使えるか（メニューの有効/無効に使う）
    static var isAvailable: Bool { ChromeDevTools.isChromeInstalled }

    // MARK: - 公開操作

    /// 会議に参加して記録を始める
    ///
    /// - Parameter urlString: Meet の会議 URL（`https://meet.google.com/xxx-yyyy-zzz`）
    func join(urlString: String) {
        guard !state.isActive else { return }
        guard let url = Self.normalizedMeetURL(urlString) else {
            state = .failed("Meet の URL ではありません")
            return
        }
        meetingName = Self.meetingCode(from: url) ?? "Google Meet"
        state = .launching
        beginAntiNap()

        Task { [weak self] in
            guard let self else { return }
            do {
                try await self.startChromeAndJoin(url: url)
            } catch {
                let message = (error as? CustomStringConvertible)?.description ?? String(describing: error)
                self.logger.error("会議への参加に失敗: \(message, privacy: .public)")
                self.teardown()
                self.state = .failed(message)
            }
        }
    }

    /// 会議から退出して記録を閉じる
    func leave() {
        guard state.isActive else { return }
        recorder.endSession()
        let tools = devTools
        Task { [weak self] in
            guard let self else { return }
            try? await tools?.evaluate(Self.leaveScript)
            self.teardown()
            self.state = .idle
        }
    }

    /// 初回ログイン用に、見える Chrome を開く
    ///
    /// Google のログインは本人にしかできない。ボット用プロファイルで一度ログインしておくと、
    /// 以後は自分の会議へ承認なしで入れる。
    func showLoginWindow() {
        guard !state.isActive else { return }
        do {
            chrome = try ChromeDevTools.launchChrome(
                port: Self.devToolsPort, profileDirectory: Self.profileDirectory, headless: false
            )
            Task { [weak self] in
                try? await ChromeDevTools.waitForDevTools(port: Self.devToolsPort)
                guard let socket = try? await ChromeDevTools.openTab(
                    url: "https://accounts.google.com/", port: Self.devToolsPort
                ) else { return }
                let tools = ChromeDevTools()
                tools.connect(to: socket)
                self?.devTools = tools
            }
        } catch {
            state = .failed(String(describing: error))
        }
    }

    /// アプリ終了時の後片付け（Chrome を残さない）
    func shutdown() {
        recorder.endSession()
        teardown()
    }

    // MARK: - 参加の流れ

    /// Chrome を起動して会議に入り、音声の記録を始めるまで
    private func startChromeAndJoin(url: URL) async throws {
        if chrome == nil || chrome?.isRunning != true {
            chrome = try ChromeDevTools.launchChrome(
                port: Self.devToolsPort, profileDirectory: Self.profileDirectory, headless: true
            )
            try await ChromeDevTools.waitForDevTools(port: Self.devToolsPort)
        }

        let socketURL = try await ChromeDevTools.openTab(url: url.absoluteString, port: Self.devToolsPort)
        let tools = ChromeDevTools()
        tools.connect(to: socketURL)
        devTools = tools

        state = .joining("会議を開いています")
        // ロビー画面が組み上がるまで待つ（DOM は非同期に描かれる）
        try await Task.sleep(for: .seconds(4))

        // 未ログインのプロファイルでは Cookie 同意が先に挟まる。押すと遷移するので先に片付ける。
        if let consent = try? await tools.evaluate(Self.consentScript),
           let clicked = (consent as? [String: Any])?["clicked"] as? String {
            logger.notice("同意画面を閉じました: \(clicked, privacy: .public)")
            try await Task.sleep(for: .seconds(3))
        }

        state = .joining("参加ボタンを探しています")
        let joinResult = try await tools.evaluate(Self.joinScript(displayName: Self.botDisplayName))
        let joined = (joinResult as? [String: Any])?["clicked"] as? String
        logger.notice("参加操作: \(joined ?? "ボタンが見つからない", privacy: .public)")

        // 承認待ちのことがあるので、会議画面になるまで粘る
        state = .joining("会議への入室を待っています")
        let entered = try await waitUntilInMeeting(tools: tools, timeout: 120)
        guard entered else {
            throw ChromeDevToolsError.protocolError("会議に入れませんでした（ホストの承認待ちのままか、URL が違います）")
        }

        if let title = try? await tools.evaluate("document.title") as? String, !title.isEmpty {
            meetingName = title.replacingOccurrences(of: " - Google Meet", with: "")
        }

        try await startRecognition()
        state = .recording(meetingName)
        startSpeakerPolling()
    }

    /// 会議に入れたか（会議画面の要素が出たか）を待つ
    private func waitUntilInMeeting(tools: ChromeDevTools, timeout: TimeInterval) async throws -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let value = try? await tools.evaluate(Self.inMeetingScript), let inMeeting = value as? Bool, inMeeting {
                return true
            }
            try? await Task.sleep(for: .seconds(2))
        }
        return false
    }

    // MARK: - ローカル文字起こし

    /// ボットの Chrome が鳴らしている音声を拾って、オンデバイス認識を始める
    ///
    /// 対象は **Chrome の PID に固定**する（最前面の追従はしない）。会議中に他のアプリを
    /// 触っても、拾うのは会議の音だけになる。
    private func startRecognition() async throws {
        guard let chromePID = chrome?.processIdentifier else {
            throw ChromeDevToolsError.protocolError("Chrome のプロセスが見つかりません")
        }
        // 同じ音を字幕とボットの両方で拾わないよう、先に字幕を止めてもらう
        onWillStartRecording?()

        state = .joining("音声認識を準備しています")
        let language = CaptionSettings.language
        let pipeline = CapturePipeline(locale: language.locale, fixedPID: chromePID)
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
        self.pipeline = pipeline
        logger.notice("会議音声のローカル認識を開始 pid=\(chromePID, privacy: .public) locale=\(language.rawValue, privacy: .public)")
    }

    /// 確定した文字起こしを議事録へ書く
    ///
    /// - Parameter text: 認識テキスト
    nonisolated private func recordFinal(_ text: String) {
        guard CaptionSettings.savesTranscript else { return }
        let speaker = speakerForCurrentSegment.withLock { value -> String? in
            let current = value
            value = nil
            return current
        } ?? speakerNow.withLock { $0 }
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.logger.notice("確定: \(speaker ?? "話者不明", privacy: .public): \(text, privacy: .public)")
            self.recorder.record(
                text: text,
                speaker: speaker,
                context: "Google Meet / \(self.meetingName)",
                language: CaptionSettings.language.displayName
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

    /// Chrome・タップ・タイマーを閉じる
    private func teardown() {
        speakerTimer?.invalidate()
        speakerTimer = nil
        let pipeline = self.pipeline
        self.pipeline = nil
        devTools?.close()
        devTools = nil
        if let chrome, chrome.isRunning {
            chrome.terminate()
        }
        chrome = nil
        speakerNow.withLock { $0 = nil }
        speakerForCurrentSegment.withLock { $0 = nil }
        endAntiNap()
        // タップと Aggregate Device は必ず畳む（残すと HAL にゴーストデバイスが溜まる）
        Task { await pipeline?.stop() }
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
