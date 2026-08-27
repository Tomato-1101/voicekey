/// Google Meet 議事録ボット
///
/// 会議 URL を渡すと、**専用プロファイルの Chrome を裏で起動して会議に参加し**、
/// Meet の字幕（話者名つき）を読んで `TranscriptRecorder` へ保存する。
///
/// **なぜ Meet の内蔵字幕を読むのか**（2026-08-28 の設計判断）:
/// - 話者名が取れる。議事録で「誰が言ったか」は本文と同じくらい重要。
/// - 会議の音声を別途タップしなくて済む。voicekey 本体のマイク／システム音声の経路に
///   一切触らないので、ディクテーションやライブ字幕と同時に動かしても干渉しない。
/// - 追加の API 課金がゼロ（Meet 側の認識をそのまま使う）。
///
/// **Chrome の使い方**: 普段使いの Chrome とは別の `--user-data-dir` を使う。作業中のタブを
/// 巻き込まないためと、ボットのログイン状態を分けて持てるようにするため。初回だけは
/// `showLoginWindow()` で見える Chrome を出し、本人に Google ログインしてもらう
/// （ログイン操作は本人にしかできない）。ログインしないままでもゲストとして参加を
/// リクエストできるが、ホストの承認が要る。
import AppKit
import Foundation
import OSLog

/// ボットの状態
enum MeetBotState: Equatable {
    /// 何もしていない
    case idle
    /// Chrome の起動・接続中
    case launching
    /// 会議に入ろうとしている（承認待ちを含む）
    case joining(String)
    /// 参加して字幕を記録している
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

/// Meet に入って字幕を記録するボット
@MainActor
final class MeetBotService {

    private let logger = makeCaptionLogger("MeetBot")

    /// DevTools のポート（普段使いの Chrome と衝突しない値にしておく）
    private static let devToolsPort = 9333

    /// 会議で表示されるボットの名前（ゲスト参加のときに入力される）
    private static let botDisplayName = "voicekey 議事録ボット"

    /// 字幕を読みにいく間隔
    ///
    /// Meet の字幕は書きながら伸びるので、短すぎると同じ文を何度も見ることになる。
    /// 1 秒あれば伸び方を追える。
    private static let pollInterval: TimeInterval = 1.0

    /// この秒数だけ更新が止まったら、その発言を確定として記録する
    private static let settleSeconds: TimeInterval = 2.5

    /// ボット専用の Chrome プロファイル
    static var profileDirectory: URL {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return support.appendingPathComponent("voicekey/meetbot-profile", isDirectory: true)
    }

    private var chrome: Process?
    private var devTools: ChromeDevTools?
    private var pollTimer: Timer?
    private let recorder = TranscriptRecorder()

    /// 記録中の会議名（議事録のヘッダに残す）
    private var meetingName = "Google Meet"

    /// 話者ごとの「まだ確定していない発言」
    private var pending: [String: PendingLine] = [:]

    /// 記録が動いている間の App Nap 抑止
    ///
    /// ポーリングは Timer なので、nap に入ると字幕を取りこぼす（voicekey で実績のある事故）。
    private var antiNapActivity: NSObjectProtocol?

    /// 状態が変わったときに呼ばれる（メニュー更新用）
    var onStateChange: ((MeetBotState) -> Void)?

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
        // 見えている途中の発言を取りこぼさないよう、閉じる前に全部書き出す
        flushAllPending()
        recorder.endSession()
        Task { [weak self] in
            guard let self else { return }
            try? await self.devTools?.evaluate(Self.leaveScript)
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
            let process = try ChromeDevTools.launchChrome(
                port: Self.devToolsPort, profileDirectory: Self.profileDirectory, headless: false
            )
            chrome = process
            Task { [weak self] in
                try? await ChromeDevTools.waitForDevTools(port: Self.devToolsPort)
                let tools = ChromeDevTools()
                if let socket = try? await ChromeDevTools.openTab(
                    url: "https://accounts.google.com/", port: Self.devToolsPort
                ) {
                    tools.connect(to: socket)
                }
                self?.devTools = tools
            }
        } catch {
            state = .failed(String(describing: error))
        }
    }

    /// アプリ終了時の後片付け（Chrome を残さない）
    func shutdown() {
        flushAllPending()
        recorder.endSession()
        teardown()
    }

    // MARK: - 参加の流れ

    /// Chrome を起動して会議に入るまで
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

        // 承認待ちのことがあるので、字幕領域が現れるまで粘る
        state = .joining("会議への入室を待っています")
        let entered = try await waitUntilInMeeting(tools: tools, timeout: 120)
        guard entered else {
            throw ChromeDevToolsError.protocolError("会議に入れませんでした（ホストの承認待ちのままか、URL が違います）")
        }

        state = .joining("字幕をオンにしています")
        _ = try? await tools.evaluate(Self.enableCaptionsScript)
        try await Task.sleep(for: .seconds(2))

        if let title = try? await tools.evaluate("document.title") as? String, !title.isEmpty {
            meetingName = title.replacingOccurrences(of: " - Google Meet", with: "")
        }
        state = .recording(meetingName)
        startPolling()
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

    // MARK: - 字幕の読み取り

    /// 字幕のポーリングを始める
    private func startPolling() {
        pollTimer?.invalidate()
        let timer = Timer.scheduledTimer(withTimeInterval: Self.pollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.pollCaptions() }
        }
        // 会議中はメニュー操作などで RunLoop のモードが変わるので common に載せる
        RunLoop.main.add(timer, forMode: .common)
        pollTimer = timer
    }

    /// 字幕を 1 回読んで、落ち着いた発言を記録する
    private func pollCaptions() async {
        guard let tools = devTools, case .recording = state else { return }
        guard let raw = try? await tools.evaluate(Self.captionScript),
              let payload = raw as? [String: Any],
              let entries = payload["entries"] as? [[String: Any]] else { return }

        let now = Date()
        for entry in entries {
            let speaker = ((entry["speaker"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let text = ((entry["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            let key = speaker.isEmpty ? "（話者不明）" : speaker

            if var current = pending[key] {
                if text == current.text {
                    // 変化なし（このあと settle を過ぎたら確定する）
                } else if text.hasPrefix(current.text) {
                    // 同じ発言が伸びている
                    current.text = text
                    current.lastChanged = now
                    pending[key] = current
                } else {
                    // 別の発言に切り替わった。前の発言をここで確定する。
                    record(current, speaker: key)
                    pending[key] = PendingLine(text: text, lastChanged: now)
                }
            } else {
                pending[key] = PendingLine(text: text, lastChanged: now)
            }
        }

        // 一定時間伸びなくなった発言を確定する
        for (key, line) in pending where now.timeIntervalSince(line.lastChanged) >= Self.settleSeconds {
            record(line, speaker: key)
            pending.removeValue(forKey: key)
        }
    }

    /// 保留中の発言をすべて書き出す（退出時・終了時）
    private func flushAllPending() {
        for (key, line) in pending { record(line, speaker: key) }
        pending.removeAll()
    }

    /// 1 発言を議事録へ書く
    private func record(_ line: PendingLine, speaker: String) {
        guard CaptionSettings.savesTranscript else { return }
        logger.notice("\(speaker, privacy: .public): \(line.text, privacy: .public)")
        recorder.record(
            text: line.text,
            speaker: speaker == "（話者不明）" ? nil : speaker,
            context: "Google Meet / \(meetingName)",
            language: "会議の字幕"
        )
    }

    // MARK: - 後片付け

    /// Chrome と接続を閉じる
    private func teardown() {
        pollTimer?.invalidate()
        pollTimer = nil
        devTools?.close()
        devTools = nil
        if let chrome, chrome.isRunning {
            chrome.terminate()
        }
        chrome = nil
        endAntiNap()
    }

    /// 記録中だけ App Nap を止める
    private func beginAntiNap() {
        guard antiNapActivity == nil else { return }
        antiNapActivity = ProcessInfo.processInfo.beginActivity(
            options: .background, reason: "meet bot caption polling"
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

    /// 確定待ちの発言
    private struct PendingLine {
        var text: String
        var lastChanged: Date
    }
}
