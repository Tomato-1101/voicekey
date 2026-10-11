/// Meet ボットの「いつ諦めるか・いつ知らせるか」と、その周りの純ロジックの回帰テスト
///
/// ブラウザ・Meet・課金 API には一切触れない（CDP の時間切れは、手元に立てた「応答しない」WebSocket で確かめる）。
import Network
import XCTest

@testable import voicekey

final class MeetBotPoliciesTests: XCTestCase {

    // MARK: - Chrome の時間切れ

    /// 応答が来ない命令は、期限で「Chrome が応答しません」として失敗する（await が永久に返らない、を防ぐ）
    func testSendTimesOutWhenChromeDoesNotAnswer() async throws {
        let server = try SilentWebSocketServer()
        defer { server.stop() }
        let port = try await server.start()

        let tools = ChromeDevTools()
        tools.connect(to: try XCTUnwrap(URL(string: "ws://127.0.0.1:\(port)/devtools/page/test")))
        defer { tools.close() }

        let started = Date()
        do {
            _ = try await tools.send("Runtime.evaluate", ["expression": "1"], timeout: 0.3)
            XCTFail("応答が無いのに成功してはいけない")
        } catch ChromeDevToolsError.timedOut {
            XCTAssertLessThan(Date().timeIntervalSince(started), 5, "期限で返ること")
        }
        XCTAssertEqual(String(describing: ChromeDevToolsError.timedOut), "Chrome が応答しません")
    }

    /// 時間切れは上限回数「続いた」ときだけ諦める（間に応答が来たら数え直し）
    func testTimeoutStreakNeedsConsecutiveTimeouts() {
        var streak = TimeoutStreak(limit: 3)
        XCTAssertFalse(streak.record(timedOut: true))
        XCTAssertFalse(streak.record(timedOut: true))
        XCTAssertFalse(streak.record(timedOut: false), "応答が来たら数え直す")
        XCTAssertFalse(streak.record(timedOut: true))
        XCTAssertFalse(streak.record(timedOut: true))
        XCTAssertTrue(streak.record(timedOut: true))
    }

    // MARK: - 記録中の会議画面の見張り

    /// 「外された」は 2 回続いたら確定する（1 回の読み違いで閉じない）
    func testRemovedNeedsTwoConsecutiveReads() {
        var watch = MeetingPresenceWatch()
        XCTAssertNil(watch.observe(.removed))
        XCTAssertNil(watch.observe(.inMeeting), "会議画面に戻ったら数え直す")
        XCTAssertNil(watch.observe(.removed))
        XCTAssertEqual(watch.observe(.removed), .removed)
    }

    /// 会議画面でない状態は 3 回続いたら「終了」とみなす
    func testNotInMeetingNeedsThreeReads() {
        var watch = MeetingPresenceWatch()
        XCTAssertNil(watch.observe(.other))
        XCTAssertNil(watch.observe(.ended))
        XCTAssertEqual(watch.observe(.other), .ended)
    }

    // MARK: - 字幕が取れていない警告

    /// 参加から 60 秒ずっと字幕欄が読めなければ 1 回だけ知らせ、読めたら取り消す
    func testCaptionMissingWarnsOnceAfterSixtySeconds() {
        let start = Date()
        var watch = CaptionPresenceWatch(start: start)
        XCTAssertNil(watch.observe(found: false, now: start.addingTimeInterval(30)))
        XCTAssertEqual(watch.observe(found: false, now: start.addingTimeInterval(61)), .warn)
        XCTAssertNil(watch.observe(found: false, now: start.addingTimeInterval(120)), "繰り返し知らせない")
        XCTAssertEqual(watch.observe(found: true, now: start.addingTimeInterval(125)), .recovered)
        XCTAssertNil(watch.observe(found: true, now: start.addingTimeInterval(130)))
    }

    /// 読めている間に途切れても、途切れてから 60 秒たつまでは知らせない
    func testCaptionMissingCountsFromWhenItDisappeared() {
        let start = Date()
        var watch = CaptionPresenceWatch(start: start)
        XCTAssertNil(watch.observe(found: true, now: start.addingTimeInterval(10)))
        XCTAssertNil(watch.observe(found: false, now: start.addingTimeInterval(65)))
        XCTAssertNil(watch.observe(found: false, now: start.addingTimeInterval(100)))
        XCTAssertEqual(watch.observe(found: false, now: start.addingTimeInterval(126)), .warn)
    }

    /// 回復後にまた読めなくなっても、同じ会議では 2 回目の警告を出さない（無言の時間のたびに鳴らさない）
    func testCaptionMissingWarnsOnlyOncePerMeeting() {
        let start = Date()
        var watch = CaptionPresenceWatch(start: start)
        XCTAssertEqual(watch.observe(found: false, now: start.addingTimeInterval(61)), .warn)
        XCTAssertEqual(watch.observe(found: true, now: start.addingTimeInterval(70)), .recovered)
        XCTAssertFalse(watch.warning)
        XCTAssertNil(watch.observe(found: false, now: start.addingTimeInterval(80)))
        XCTAssertNil(watch.observe(found: false, now: start.addingTimeInterval(300)), "回復後の再欠落では再通知しない")
        // 回復の表示消去は従来どおり（再欠落では warning が立たないので recovered も出ない）
        XCTAssertNil(watch.observe(found: true, now: start.addingTimeInterval(310)))
    }

    /// 通知文は「誰も話していないだけなら大丈夫」の旨を含む
    func testCaptionMissingNoticeReassuresWhenNobodySpeaks() {
        XCTAssertTrue(MeetBotText.captionMissingNotice.contains("誰も話していないだけなら"))
    }

    // MARK: - 参加ボタンの押し直し

    /// ロビーにいる間は間隔をあけて上限回数まで押し直す
    func testJoinRetryPressesUpToLimitWithInterval() {
        var retry = JoinRetry()
        let start = Date()
        var presses = 0
        for second in stride(from: 0.0, through: 60.0, by: 2.0) {
            let now = start.addingTimeInterval(second)
            if retry.shouldPress(status: .other, now: now) {
                retry.recordPress(found: false, now: now)
                presses += 1
            }
        }
        XCTAssertEqual(presses, JoinRetry.maxAttempts)
        XCTAssertFalse(retry.buttonFound)
    }

    /// ロビー以外（承認待ち・会議中）では押さない
    func testJoinRetryOnlyPressesInLobby() {
        let retry = JoinRetry()
        XCTAssertFalse(retry.shouldPress(status: .waitingApproval, now: Date()))
        XCTAssertFalse(retry.shouldPress(status: .inMeeting, now: Date()))
        XCTAssertTrue(retry.shouldPress(status: .other, now: Date()))
    }

    /// 90 秒で入れなかったときの失敗を、実際の状況で出し分ける
    func testJoinTimeoutReasons() {
        XCTAssertEqual(MeetBotError.joinTimeout(waitingSeen: true, joinButtonFound: true), .approvalTimeout)
        XCTAssertEqual(MeetBotError.joinTimeout(waitingSeen: false, joinButtonFound: true), .couldNotEnter)
        XCTAssertEqual(MeetBotError.joinTimeout(waitingSeen: false, joinButtonFound: false), .joinButtonNotFound)
    }

    // MARK: - 終了通知の文言

    /// 「保存できなかった」「0 行だった」「保存した」「保存オフ」を区別する
    func testClosingMessageDistinguishesOutcomes() {
        let reason = "会議から退出しました"
        XCTAssertEqual(
            MeetBotText.closingMessage(reason: reason, savesTranscript: false, fileStarted: false, lines: 0, saveError: nil),
            "会議から退出しました。議事録の保存はオフでした。"
        )
        XCTAssertEqual(
            MeetBotText.closingMessage(reason: reason, savesTranscript: true, fileStarted: true, lines: 3, saveError: "書き込めません"),
            "会議から退出しました。議事録を保存できませんでした: 書き込めません"
        )
        XCTAssertTrue(
            MeetBotText.closingMessage(reason: reason, savesTranscript: true, fileStarted: true, lines: 0, saveError: nil)
                .contains("議事録の記録は 0 行でした")
        )
        XCTAssertEqual(
            MeetBotText.closingMessage(reason: reason, savesTranscript: true, fileStarted: true, lines: 12, saveError: nil),
            "会議から退出しました。議事録を保存しました（12 行）。"
        )
    }

    // MARK: - 退出時の順序（最後の 1 文を捨てない）

    /// 停止処理の中で確定した最後の 1 文が、議事録を閉じる前に書かれて行数に入る
    func testStopIsAwaitedBeforeClosingTranscript() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-stop-order-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let recorder = TranscriptRecorder(directory: directory)
        recorder.record(text: "最初の発言", context: "Google Meet", language: "日本語")

        let stopped = await StopWaiter.wait(limit: 3) {
            // 認識器の締めで最後の 1 文が確定する、を模す
            try? await Task.sleep(for: .milliseconds(200))
            recorder.record(text: "最後の発言", context: "Google Meet", language: "日本語")
        }
        XCTAssertTrue(stopped)

        let summary = await withCheckedContinuation { (continuation: CheckedContinuation<TranscriptRecorder.SessionSummary, Never>) in
            recorder.endSession { continuation.resume(returning: $0) }
        }
        XCTAssertEqual(summary.lines, 2, "最後の 1 文も数えること")
        XCTAssertNil(summary.error)
        let url = try XCTUnwrap(summary.url)
        XCTAssertTrue(try String(contentsOf: url, encoding: .utf8).contains("最後の発言"))
    }

    /// 停止が固まっても、上限時間で打ち切って先へ進む（ボットごと止まらない）
    func testStopWaiterGivesUpAtLimit() async {
        let started = Date()
        let stopped = await StopWaiter.wait(limit: 0.3) {
            try? await Task.sleep(for: .seconds(10))
        }
        XCTAssertFalse(stopped)
        XCTAssertLessThan(Date().timeIntervalSince(started), 3)
    }

    /// 保存に失敗した会議は、集計に失敗の理由が入る（0 行と区別できる）
    func testSessionSummaryCarriesWriteFailure() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-summary-fail-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        try Data().write(to: directory)  // 同名のファイルを置いてフォルダを作れなくする
        let recorder = TranscriptRecorder(directory: directory)
        recorder.record(text: "保存できない発言", context: nil, language: "日本語")
        let summary = await withCheckedContinuation { (continuation: CheckedContinuation<TranscriptRecorder.SessionSummary, Never>) in
            recorder.endSession { continuation.resume(returning: $0) }
        }
        XCTAssertEqual(summary.lines, 0)
        XCTAssertNotNil(summary.error)
    }

    // MARK: - 議事録の書き込み

    /// 別の人が続けて同じ言葉を言ったら両方残す（同じ人の再送だけを弾く）
    func testDuplicateFilterComparesSpeaker() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-speaker-dedup-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let recorder = TranscriptRecorder(directory: directory)
        let start = Date()
        recorder.record(text: "はい", speaker: "山田", context: nil, language: "日本語", now: start)
        recorder.record(text: "はい", speaker: "佐藤", context: nil, language: "日本語", now: start.addingTimeInterval(1))
        recorder.record(text: "はい", speaker: "佐藤", context: nil, language: "日本語", now: start.addingTimeInterval(2))
        recorder.waitUntilWritten()
        let content = try String(contentsOf: try XCTUnwrap(recorder.currentFileURL), encoding: .utf8)
        XCTAssertTrue(content.contains("**山田**: はい"))
        XCTAssertEqual(content.components(separatedBy: "**佐藤**: はい").count - 1, 1, content)
    }

    /// 書き込み結果には会議の番号が付く（前の会議の結果を新しい会議と取り違えない）
    func testWriteOutcomeCarriesMeetingTag() {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-tag-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let recorder = TranscriptRecorder(directory: directory)
        let tags = TagBox()
        recorder.onWrite = { _, tag in tags.append(tag) }
        recorder.beginMeeting(code: "abc-defg-hij", context: "Google Meet", language: "日本語", tag: 7)
        recorder.record(text: "一行目", context: "Google Meet", language: "日本語")
        recorder.endSession()
        recorder.beginMeeting(code: "xyz-wxyz-xyz", context: "Google Meet", language: "日本語", tag: 8)
        recorder.waitUntilWritten()
        XCTAssertEqual(tags.values, [7, 7, 8])
    }

    // MARK: - 会議画面の文言

    /// 「外された」は特定の言い回しだけで判定する（チャットの「削除されました」等で誤って閉じない）
    func testRemovedNeedsSpecificWording() {
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "通話から削除されました"), .removed)
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "Someone removed you from the meeting"), .removed)
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "メッセージが削除されました"), .other)
    }

    /// 会議コードが違うときは「見つからない」と分かる（90 秒待たせない）
    func testMeetingNotFoundStatus() {
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "会議コードを確認してください"), .notFound)
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "Check your meeting code"), .notFound)
    }

    // MARK: - 取り残し Chrome の片付け対象

    /// プロファイルのパスの後ろに空白で別の名前が続くもの（別のフォルダ）は選ばない
    func testStaleChromeRequiresArgumentBoundary() {
        let profile = "/Users/me/meetbot-profile"
        let ps = """
          401 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=\(profile) --headless=new
          402 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=\(profile) 2 --headless=new
          403 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=\(profile)-old
        """
        XCTAssertEqual(ChromeDevTools.staleChromePIDs(psOutput: ps, profilePath: profile), [401])
    }

    // MARK: - ログイン

    /// 見張り中は、ログインできたと分かるまで「ログイン待ち」のまま（点滅させない）
    func testLoginStatusWhileWatching() {
        XCTAssertEqual(MeetBotLoginStatus.whileWatching(loggedIn: true), .loggedIn)
        XCTAssertEqual(MeetBotLoginStatus.whileWatching(loggedIn: false), .waitingForLogin)
        XCTAssertEqual(MeetBotLoginStatus.whileWatching(loggedIn: nil), .waitingForLogin)
    }

    /// ログイン済みと分かっていない間は、参加より先にログインへ案内する
    func testNeedsLoginBeforeJoin() {
        XCTAssertTrue(MeetBotLoginStatus.unknown.needsLogin)
        XCTAssertTrue(MeetBotLoginStatus.loggedOut.needsLogin)
        XCTAssertTrue(MeetBotLoginStatus.waitingForLogin.needsLogin)
        XCTAssertFalse(MeetBotLoginStatus.loggedIn.needsLogin)
        XCTAssertFalse(MeetBotLoginStatus.checking.needsLogin)
    }

    // MARK: - 通知を押したときに開く先

    /// 議事録が残っていればそれ、消えていればフォルダ、フォルダも無ければ何も開かない
    func testNotificationTarget() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-notify-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("2026-10-12_1000.md")

        XCTAssertNil(NotificationDelegate.target(path: file.path, directory: directory))
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        XCTAssertEqual(NotificationDelegate.target(path: file.path, directory: directory), directory)
        XCTAssertEqual(NotificationDelegate.target(path: nil, directory: directory), directory)
        try Data("# 議事録".utf8).write(to: file)
        XCTAssertEqual(NotificationDelegate.target(path: file.path, directory: directory)?.path, file.path)
    }
}

/// 会議 URL の形の判定（`MeetBotService` は macOS 26 限定なので別クラスにする）
@available(macOS 26.0, *)
@MainActor
final class MeetBotMeetingPathTests: XCTestCase {

    /// 会議コードか lookup のときだけ通す（トップページなどでボットを起動しない）
    func testRequiresMeetingCodeOrLookup() {
        XCTAssertNotNil(MeetBotService.normalizedMeetURL("https://meet.google.com/abc-defg-hij"))
        XCTAssertNotNil(MeetBotService.normalizedMeetURL("https://meet.google.com/abcdefghij"))
        XCTAssertNotNil(MeetBotService.normalizedMeetURL("https://meet.google.com/lookup/team-standup"))
        XCTAssertNil(MeetBotService.normalizedMeetURL("https://meet.google.com/"))
        XCTAssertNil(MeetBotService.normalizedMeetURL("https://meet.google.com/landing"))
        XCTAssertNil(MeetBotService.normalizedMeetURL("https://meet.google.com/abc-defg-hij/extra"))
    }
}

/// `onWrite` の会議番号を集める（記録用キューから呼ばれるので鍵で守る）
private final class TagBox: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [Int] = []
    var values: [Int] { lock.withLock { stored } }
    func append(_ value: Int) { lock.withLock { stored.append(value) } }
}

/// 接続は受けるが、何を送られても答えない WebSocket サーバー（固まった Chrome の代わり）
///
/// 127.0.0.1 だけで待ち受ける（外から繋がらない・ファイアウォールの確認を出さない）。
private final class SilentWebSocketServer: @unchecked Sendable {
    private let listener: NWListener
    private let queue = DispatchQueue(label: "SilentWebSocketServer")
    private var connections: [NWConnection] = []

    init() throws {
        let parameters = NWParameters.tcp
        let options = NWProtocolWebSocket.Options()
        options.autoReplyPing = true
        parameters.defaultProtocolStack.applicationProtocols.insert(options, at: 0)
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: .any)
        listener = try NWListener(using: parameters)
    }

    /// 待ち受けを始めて、決まったポート番号を返す
    func start() async throws -> UInt16 {
        listener.newConnectionHandler = { [weak self] connection in
            guard let self else { return }
            self.connections.append(connection)
            connection.start(queue: self.queue)
            self.drain(connection)
        }
        return try await withCheckedThrowingContinuation { continuation in
            var resumed = false
            listener.stateUpdateHandler = { [weak self] state in
                guard !resumed else { return }
                switch state {
                case .ready:
                    resumed = true
                    continuation.resume(returning: self?.listener.port?.rawValue ?? 0)
                case let .failed(error):
                    resumed = true
                    continuation.resume(throwing: error)
                default:
                    break
                }
            }
            listener.start(queue: queue)
        }
    }

    /// 届いたものを読み捨てる（答えない）
    private func drain(_ connection: NWConnection) {
        connection.receiveMessage { [weak self] _, _, _, error in
            guard error == nil else { return }
            self?.drain(connection)
        }
    }

    func stop() {
        listener.cancel()
        queue.sync { connections.forEach { $0.cancel() } }
    }
}
