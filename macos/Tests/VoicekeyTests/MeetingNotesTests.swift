/// 議事録の一覧・解析、要約の段取り、ゲスト参加の判断の回帰テスト
///
/// どれも純粋なロジック。Groq への通信は差し替えた関数で代用し、外へは一切出ない。
/// 議事録フォルダは一時ディレクトリを使う（実ユーザーの ~/Documents を読まない・汚さない）。
import XCTest

@testable import voicekey

/// 通信の代わり（呼ばれた内容を覚え、決めた順に答える）
private final class FakeChat: @unchecked Sendable {
    private let lock = NSLock()
    private var answers: [Result<String, LLMChatError>]
    private(set) var calls: [(system: String, user: String)] = []

    /// - Parameter answers: 呼ばれた順に返す答え（尽きたら最後の答えを返し続ける）
    init(_ answers: [Result<String, LLMChatError>]) {
        self.answers = answers
    }

    func ask(_ system: String, _ user: String) throws -> String {
        lock.lock()
        defer { lock.unlock() }
        calls.append((system, user))
        let answer = answers.count > 1 ? answers.removeFirst() : answers[0]
        return try answer.get()
    }

    var chat: TranscriptSummarizer.Chat {
        { [self] system, user in try ask(system, user) }
    }
}

/// 順番に足していける入れ物（進み具合・待ち時間の記録用）
private final class Box<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var items: [Value] = []

    func add(_ value: Value) {
        lock.lock()
        items.append(value)
        lock.unlock()
    }

    var values: [Value] {
        lock.lock()
        defer { lock.unlock() }
        return items
    }
}

final class MeetingNotesTests: XCTestCase {

    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-notes-test-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    /// 発言 `count` 行ぶんの議事録の本文を作る
    private func transcript(lines count: Int, text: String = "来週までに数字をまとめます") -> String {
        var content = "# 議事録 2026-11-11 14:05\n\n- 記録元: Google Meet\n- 言語: 日本語\n\n"
        for index in 0..<count {
            content += "- `14:05:\(String(format: "%02d", index % 60))` **山田**: \(text)\n"
        }
        return content
    }

    // MARK: - ファイル名

    /// いまの形（秒まで）と会議コードを読む
    func testParsesFileNameWithMeetingCode() throws {
        let parsed = TranscriptLibrary.parseFileName("2026-10-11_140512-abc-defg-hij")
        XCTAssertEqual(parsed.code, "abc-defg-hij")
        let parts = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute, .second], from: try XCTUnwrap(parsed.date))
        XCTAssertEqual([parts.year, parts.month, parts.day, parts.hour, parts.minute, parts.second], [2026, 10, 11, 14, 5, 12])
    }

    /// 会議コードなし・以前の形（分まで）も読む
    func testParsesFileNameWithoutCodeAndOldFormat() {
        let plain = TranscriptLibrary.parseFileName("2026-10-11_140512")
        XCTAssertNotNil(plain.date)
        XCTAssertNil(plain.code)

        let old = TranscriptLibrary.parseFileName("2026-08-28_1432")
        XCTAssertNotNil(old.date)
        XCTAssertNil(old.code)
    }

    /// 同名回避の連番（-2）は会議コードに含めない
    func testDropsDuplicateSuffixFromFileName() {
        XCTAssertNil(TranscriptLibrary.parseFileName("2026-10-11_140512-2").code)
        XCTAssertEqual(TranscriptLibrary.parseFileName("2026-10-11_140512-abc-defg-hij-2").code, "abc-defg-hij")
    }

    /// 議事録の名前でないものは日時なしで返す（呼び出し側が更新日時を使う）
    func testRejectsUnknownFileName() {
        XCTAssertNil(TranscriptLibrary.parseFileName("memo").date)
        XCTAssertNil(TranscriptLibrary.parseFileName("2026-10-11_abc").date)
    }

    // MARK: - 本文の解析

    /// 話者つき・話者なしの行を読み、ヘッダは飛ばす
    func testParsesLines() {
        let content = """
            # 議事録 2026-11-11 14:05

            - 記録元: Google Meet
            - 言語: 日本語

            - `14:05:01` **山田**: 始めます
            - `14:05:09` 話者なしの行
            - `14:05:12` **佐藤 花子**: 時刻は 10:30: からです
            """
        let lines = TranscriptLibrary.parseLines(content)
        XCTAssertEqual(lines, [
            TranscriptLine(id: 1, time: "14:05:01", speaker: "山田", text: "始めます"),
            TranscriptLine(id: 2, time: "14:05:09", speaker: nil, text: "話者なしの行"),
            TranscriptLine(id: 3, time: "14:05:12", speaker: "佐藤 花子", text: "時刻は 10:30: からです"),
        ])
    }

    /// 記録側（TranscriptRecorder）が書いた行を、そのまま読み戻せる
    func testParsesWhatRecorderWrites() throws {
        let recorder = TranscriptRecorder(directory: directory)
        let received = Box<TranscriptLine>()
        recorder.onLine = { line, _ in received.add(line) }
        recorder.record(text: "よろしくお願いします", speaker: "山田", context: "Google Meet", language: "日本語")
        recorder.record(text: "はい", context: "Google Meet", language: "日本語")
        recorder.waitUntilWritten()

        let url = try XCTUnwrap(recorder.currentFileURL)
        let parsed = TranscriptLibrary.parseLines(try String(contentsOf: url, encoding: .utf8))
        XCTAssertEqual(parsed.map(\.speaker), ["山田", nil])
        XCTAssertEqual(parsed.map(\.text), ["よろしくお願いします", "はい"])

        // 画面へ流す行（onLine）は、ファイルに書いた行と同じ中身
        let deadline = Date().addingTimeInterval(2)
        while received.values.count < 2, Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.01))
        }
        XCTAssertEqual(received.values, parsed)
    }

    // MARK: - 一覧

    /// 新しい順に並び、要約ファイルは一覧に出さず「要約あり」として数える
    func testListsTranscriptsNewestFirst() throws {
        try transcript(lines: 2).write(
            to: directory.appendingPathComponent("2026-11-10_090000-abc-defg-hij.md"), atomically: true, encoding: .utf8)
        try transcript(lines: 5).write(
            to: directory.appendingPathComponent("2026-11-11_140500.md"), atomically: true, encoding: .utf8)
        try "## 要点\n- なし".write(
            to: directory.appendingPathComponent("2026-11-10_090000-abc-defg-hij.summary.md"), atomically: true, encoding: .utf8)
        try "関係ないファイル".write(
            to: directory.appendingPathComponent("memo.txt"), atomically: true, encoding: .utf8)

        let list = TranscriptLibrary.list(in: directory)
        XCTAssertEqual(list.map(\.url.lastPathComponent), ["2026-11-11_140500.md", "2026-11-10_090000-abc-defg-hij.md"])
        XCTAssertEqual(list.map(\.lineCount), [5, 2])
        XCTAssertEqual(list.map(\.meetingCode), [nil, "abc-defg-hij"])
        XCTAssertEqual(list.map(\.hasSummary), [false, true])
        XCTAssertEqual(list[1].characterCount, 2 * "来週までに数字をまとめます".count)
        XCTAssertEqual(TranscriptLibrary.detail(for: list[1]), "abc-defg-hij・2 行・26 字")
    }

    /// フォルダが無くても落ちずに空を返す
    func testListIsEmptyWhenDirectoryIsMissing() {
        XCTAssertEqual(TranscriptLibrary.list(in: directory.appendingPathComponent("none")), [])
    }

    /// 要約は議事録の隣に `<元の名前>.summary.md` で置く
    func testSummaryURLSitsNextToTranscript() {
        let url = directory.appendingPathComponent("2026-11-11_140500-abc-defg-hij.md")
        let summary = TranscriptLibrary.summaryURL(for: url)
        XCTAssertEqual(summary.lastPathComponent, "2026-11-11_140500-abc-defg-hij.summary.md")
        XCTAssertEqual(summary.deletingLastPathComponent().path, directory.path)
        XCTAssertTrue(TranscriptLibrary.isTranscript(url))
        XCTAssertFalse(TranscriptLibrary.isTranscript(summary))
    }

    /// 「最新の議事録を開く」は要約ファイルを選ばない
    func testLatestFileSkipsSummary() throws {
        let transcriptURL = directory.appendingPathComponent("2026-11-11_140500.md")
        try transcript(lines: 1).write(to: transcriptURL, atomically: true, encoding: .utf8)
        let summaryURL = TranscriptLibrary.summaryURL(for: transcriptURL)
        try "## 要点".write(to: summaryURL, atomically: true, encoding: .utf8)
        // 要約のほうを新しくしておく
        try FileManager.default.setAttributes(
            [.modificationDate: Date().addingTimeInterval(60)], ofItemAtPath: summaryURL.path)

        XCTAssertEqual(TranscriptRecorder.latestFile(in: directory)?.lastPathComponent, "2026-11-11_140500.md")
    }

    // MARK: - 要約の分割

    /// 行の切れ目で分け、どのかたまりも上限以内で、1 文字も落とさない
    func testChunksKeepEveryLine() {
        let lines = (0..<40).map { "行\($0) " + String(repeating: "あ", count: $0 % 7 + 3) }
        let chunks = TranscriptSummarizer.chunks(lines.joined(separator: "\n"), limit: 50)
        XCTAssertGreaterThan(chunks.count, 1)
        XCTAssertTrue(chunks.allSatisfy { $0.count <= 50 })
        XCTAssertEqual(chunks.joined(separator: "\n").split(separator: "\n").map(String.init), lines)
    }

    /// 1 行だけで上限を超えるときは、その行を文字数で切る（落とさない）
    func testChunksSplitOverlongLine() {
        let long = String(repeating: "長", count: 25)
        let chunks = TranscriptSummarizer.chunks("短い\n\(long)\n終わり", limit: 10)
        XCTAssertTrue(chunks.allSatisfy { $0.count <= 10 })
        XCTAssertEqual(chunks.joined().replacingOccurrences(of: "\n", with: ""), "短い\(long)終わり")
    }

    /// 上限以内なら 1 つのまま・空なら何も返さない
    func testChunksSmallAndEmpty() {
        XCTAssertEqual(TranscriptSummarizer.chunks("一行目\n二行目", limit: 100), ["一行目\n二行目"])
        XCTAssertEqual(TranscriptSummarizer.chunks("\n\n"), [])
    }

    // MARK: - 429 の待ち方

    /// `retry-after` があればその秒数だけ待つ。無ければ 5・10・20 秒と延ばす
    func testRetryDecisionWaits() {
        XCTAssertEqual(TranscriptSummarizer.retryDecision(retryAfter: 12, attempt: 0), .wait(12))
        XCTAssertEqual(TranscriptSummarizer.retryDecision(retryAfter: nil, attempt: 0), .wait(5))
        XCTAssertEqual(TranscriptSummarizer.retryDecision(retryAfter: nil, attempt: 1), .wait(10))
        XCTAssertEqual(TranscriptSummarizer.retryDecision(retryAfter: nil, attempt: 2), .wait(20))
        // 0 秒でも即座に叩き直さない
        XCTAssertEqual(TranscriptSummarizer.retryDecision(retryAfter: 0, attempt: 0), .wait(1))
    }

    /// 回数の上限に達したとき・待ち時間が長すぎるときは諦める
    func testRetryDecisionGivesUp() {
        XCTAssertEqual(
            TranscriptSummarizer.retryDecision(retryAfter: 1, attempt: TranscriptSummarizer.maxRetries), .giveUp)
        XCTAssertEqual(
            TranscriptSummarizer.retryDecision(retryAfter: TranscriptSummarizer.maxRetryWait + 1, attempt: 0), .giveUp)
        XCTAssertEqual(
            TranscriptSummarizer.retryDecision(retryAfter: TranscriptSummarizer.maxRetryWait, attempt: 0),
            .wait(TranscriptSummarizer.maxRetryWait))
    }

    /// `retry-after` ヘッダの読み方（秒数だけ受け付ける）
    func testRetryAfterHeader() {
        XCTAssertEqual(TextFormatter.retryAfterSeconds("7"), 7)
        XCTAssertEqual(TextFormatter.retryAfterSeconds(" 2.5 "), 2.5)
        XCTAssertNil(TextFormatter.retryAfterSeconds(nil))
        XCTAssertNil(TextFormatter.retryAfterSeconds("soon"))
        XCTAssertNil(TextFormatter.retryAfterSeconds("-1"))
    }

    // MARK: - 要約の段取り

    /// 短い会議は 1 回の問い合わせで仕上げる
    func testSummarizesShortTranscriptInOneCall() async throws {
        let fake = FakeChat([.success("## 要点\n- まとめ")])
        let steps = Box<TranscriptSummarizer.Progress>()
        let text = try await TranscriptSummarizer.summarize(
            transcript(lines: 3), chat: fake.chat, sleep: { _ in }, progress: { steps.add($0) })

        XCTAssertEqual(text, "## 要点\n- まとめ")
        XCTAssertEqual(fake.calls.count, 1)
        XCTAssertEqual(fake.calls[0].system, TranscriptSummarizer.finalPrompt)
        XCTAssertTrue(fake.calls[0].user.hasPrefix("<<<\n"), "本文は区切りで囲んで渡すこと")
        XCTAssertTrue(fake.calls[0].user.contains("来週までに数字をまとめます"))
        XCTAssertEqual(steps.values, [.part(1, of: 1)])
    }

    /// 長い会議は部分ごとに要約してから 1 つにまとめ直す
    func testSummarizesLongTranscriptInParts() async throws {
        let content = transcript(lines: 300)
        let parts = TranscriptSummarizer.chunks(content).count
        XCTAssertGreaterThan(parts, 1, "テストの前提: 1 回では送れない長さ")

        let fake = FakeChat([.success("- 部分の要点")])
        let steps = Box<TranscriptSummarizer.Progress>()
        let text = try await TranscriptSummarizer.summarize(
            content, chat: fake.chat, sleep: { _ in }, progress: { steps.add($0) })

        XCTAssertEqual(text, "- 部分の要点")
        XCTAssertEqual(fake.calls.count, parts + 1, "部分の数だけ要約し、最後に 1 回まとめる")
        XCTAssertTrue(fake.calls.dropLast().allSatisfy { $0.system == TranscriptSummarizer.partPrompt })
        XCTAssertEqual(fake.calls.last?.system, TranscriptSummarizer.finalPrompt)
        XCTAssertEqual(steps.values, (1...parts).map { .part($0, of: parts) } + [.merging])
        XCTAssertEqual(steps.values.first?.message, "1/\(parts) 個目を要約中")
    }

    /// 部分の要約を合わせてもまだ長いときは、もう一段縮めてからまとめる（回数の上限つき）
    func testMergesAgainWhenNotesAreStillLong() async throws {
        let content = transcript(lines: 300)
        let parts = TranscriptSummarizer.chunks(content).count
        // 部分の要約が毎回とても長い（縮まらない）場合でも、上限で打ち切って最後のまとめへ進む
        let long = String(repeating: "長い要約の行\n", count: TranscriptSummarizer.chunkCharacterLimit / 4)
        let fake = FakeChat([.success(long)])
        _ = try await TranscriptSummarizer.summarize(content, chat: fake.chat, sleep: { _ in })

        XCTAssertGreaterThan(fake.calls.count, parts + 1, "まとめ直しが入ること")
        XCTAssertEqual(fake.calls.last?.system, TranscriptSummarizer.finalPrompt)
    }

    /// 429 は `retry-after` だけ待ってから、もう一度問い合わせる
    func testRetriesAfterRateLimit() async throws {
        let fake = FakeChat([.failure(.rateLimited(retryAfter: 3)), .failure(.rateLimited(retryAfter: nil)), .success("できた")])
        let waits = Box<TimeInterval>()
        let steps = Box<TranscriptSummarizer.Progress>()
        let text = try await TranscriptSummarizer.summarize(
            transcript(lines: 3), chat: fake.chat, sleep: { waits.add($0) }, progress: { steps.add($0) })

        XCTAssertEqual(text, "できた")
        XCTAssertEqual(fake.calls.count, 3)
        XCTAssertEqual(waits.values, [3, 10], "1 回目は retry-after、2 回目は自前の待ち時間（2 回目なので 10 秒）")
        XCTAssertTrue(steps.values.contains(.waiting(seconds: 3)))
    }

    /// 429 が続いたら回数の上限で諦める（永久に叩き続けない）
    func testGivesUpAfterRepeatedRateLimit() async {
        let fake = FakeChat([.failure(.rateLimited(retryAfter: 1))])
        do {
            _ = try await TranscriptSummarizer.summarize(transcript(lines: 3), chat: fake.chat, sleep: { _ in })
            XCTFail("429 が続いているのに成功してはいけない")
        } catch {
            XCTAssertEqual(error as? LLMChatError, .rateLimited(retryAfter: 1))
        }
        XCTAssertEqual(fake.calls.count, TranscriptSummarizer.maxRetries + 1)
    }

    /// 長く待てと言われたら待たずに諦める（1 日の上限に当たったとき）
    func testDoesNotWaitWhenRetryAfterIsTooLong() async {
        let fake = FakeChat([.failure(.rateLimited(retryAfter: 3600))])
        let waits = Box<TimeInterval>()
        do {
            _ = try await TranscriptSummarizer.summarize(
                transcript(lines: 3), chat: fake.chat, sleep: { waits.add($0) })
            XCTFail("成功してはいけない")
        } catch {
            XCTAssertEqual(error as? LLMChatError, .rateLimited(retryAfter: 3600))
        }
        XCTAssertEqual(fake.calls.count, 1)
        XCTAssertEqual(waits.values, [])
    }

    /// 429 以外の失敗は再試行しない
    func testDoesNotRetryOtherFailures() async {
        let fake = FakeChat([.failure(.http(500))])
        do {
            _ = try await TranscriptSummarizer.summarize(transcript(lines: 3), chat: fake.chat, sleep: { _ in })
            XCTFail("成功してはいけない")
        } catch {
            XCTAssertEqual(error as? LLMChatError, .http(500))
        }
        XCTAssertEqual(fake.calls.count, 1)
    }

    /// 発言が無い議事録は、問い合わせずに「発言がありません」で止める
    func testEmptyTranscriptIsNotSent() async {
        let fake = FakeChat([.success("呼ばれないはず")])
        do {
            _ = try await TranscriptSummarizer.summarize(transcript(lines: 0), chat: fake.chat, sleep: { _ in })
            XCTFail("成功してはいけない")
        } catch {
            XCTAssertEqual(error as? TranscriptSummarizer.Failure, .empty)
        }
        XCTAssertEqual(fake.calls.count, 0)
    }

    /// 失敗の文言は短い日本語で、API のエラー本文を含まない
    func testFailureMessages() {
        XCTAssertEqual(TranscriptSummarizer.message(for: TranscriptSummarizer.Failure.empty), "要約する発言がありません")
        XCTAssertTrue(TranscriptSummarizer.message(for: LLMChatError.missingKey).contains("API キー"))
        XCTAssertTrue(TranscriptSummarizer.message(for: LLMChatError.rateLimited(retryAfter: 5)).contains("利用上限"))
        XCTAssertEqual(TranscriptSummarizer.message(for: LLMChatError.http(401)), "Groq の API キーが使えません")
        XCTAssertEqual(TranscriptSummarizer.message(for: LLMChatError.http(503)), "Groq がエラーを返しました（HTTP 503）")
        XCTAssertEqual(TranscriptSummarizer.message(for: URLError(.badURL)), "要約できませんでした")
        XCTAssertEqual(TranscriptSummarizer.message(for: CancellationError()), "要約を止めました")
    }

    // MARK: - ゲスト参加

    /// ログイン済みのときだけアカウントで入る。未ログイン・確かめられなかったときはゲスト
    func testJoinModeFollowsLogin() {
        XCTAssertEqual(MeetJoinMode(loggedIn: true), .account)
        XCTAssertEqual(MeetJoinMode(loggedIn: false), .guest)
        XCTAssertEqual(MeetJoinMode(loggedIn: nil), .guest)
        XCTAssertEqual(MeetJoinMode.guest.approvalWaitLimit, 300, "ゲストの承認待ちは 5 分")
        XCTAssertEqual(MeetJoinMode.account.approvalWaitLimit, 600)
    }

    /// 画面の状態から次にすることを決める（入れた・待つ・押す・読めない）
    func testJoinStepForOrdinaryStates() {
        for mode in [MeetJoinMode.guest, .account] {
            XCTAssertEqual(MeetJoinFlow.step(status: .inMeeting, mode: mode), .entered)
            XCTAssertEqual(MeetJoinFlow.step(status: .waitingApproval, mode: mode), .waitForApproval)
            XCTAssertEqual(MeetJoinFlow.step(status: .other, mode: mode), .pressJoin)
            XCTAssertEqual(MeetJoinFlow.step(status: nil, mode: mode), .wait)
            XCTAssertEqual(MeetJoinFlow.step(status: .removed, mode: mode), .fail(.removed))
            XCTAssertEqual(MeetJoinFlow.step(status: .ended, mode: mode), .fail(.callEnded))
            XCTAssertEqual(MeetJoinFlow.step(status: .notFound, mode: mode), .fail(.meetingNotFound))
        }
    }

    /// ゲストは「承認されなかった」と「ゲストが入れない会議」を理由で分ける
    func testJoinStepSeparatesGuestFailures() {
        XCTAssertEqual(MeetJoinFlow.step(status: .denied, mode: .guest), .fail(.guestNotApproved))
        XCTAssertEqual(MeetJoinFlow.step(status: .cannotJoin, mode: .guest), .fail(.guestNotAllowed))
        XCTAssertEqual(MeetJoinFlow.step(status: .guestBlocked, mode: .guest), .fail(.guestNotAllowed))
        // ログイン済みなら従来どおり「断られた」
        XCTAssertEqual(MeetJoinFlow.step(status: .denied, mode: .account), .fail(.denied))
        XCTAssertEqual(MeetJoinFlow.step(status: .cannotJoin, mode: .account), .fail(.denied))
    }

    /// 待ちきれなかったとき: ゲストで承認待ちまで進んでいたら「承認されませんでした」
    func testJoinTimeout() {
        XCTAssertEqual(MeetJoinFlow.timeout(mode: .guest, waitingSeen: true, joinButtonFound: true), .guestNotApproved)
        XCTAssertEqual(MeetJoinFlow.timeout(mode: .guest, waitingSeen: false, joinButtonFound: true), .couldNotEnter)
        XCTAssertEqual(MeetJoinFlow.timeout(mode: .guest, waitingSeen: false, joinButtonFound: false), .joinButtonNotFound)
        XCTAssertEqual(MeetJoinFlow.timeout(mode: .account, waitingSeen: true, joinButtonFound: true), .approvalTimeout)
    }

    /// 会議のページから飛ばされたとき: ゲストは「ゲストが入れない会議」、ログイン済みは「ログイン切れ」
    func testLeftMeetingPage() {
        XCTAssertEqual(MeetJoinFlow.leftMeetingPage(mode: .guest), .guestNotAllowed)
        XCTAssertEqual(MeetJoinFlow.leftMeetingPage(mode: .account), .notLoggedIn)
    }

    /// ログイン画面へ飛ばされた・ログインを求められた画面は「ゲストでは入れない」
    func testPageStatusForGuestBlocked() {
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "", signInPage: true), .guestBlocked)
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "Sign in to join this meeting"), .guestBlocked)
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "参加するにはログインしてください"), .guestBlocked)
        // 参加ボタンや名前欄が見えている間は、案内文に同じ語が出ても決めつけない
        XCTAssertEqual(
            MeetBotRules.pageStatus(inMeeting: false, text: "Sign in to join with your account", joinControls: true),
            .other)
        // 会議に入れていれば、ほかの文字に関係なく入室
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: true, text: "Sign in to join", signInPage: true), .inMeeting)
    }

    /// 「拒否された」と「この会議には入れない」を別の状態として読む
    func testPageStatusSeparatesDeniedAndCannotJoin() {
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "Someone denied your request to join"), .denied)
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "この通話に参加できません"), .cannotJoin)
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "You can't join this call"), .cannotJoin)
    }

    /// ログインへ案内する失敗と、その通知の文言
    func testLoginSuggestionAndNotices() {
        XCTAssertTrue(MeetBotError.guestNotApproved.suggestsLogin)
        XCTAssertTrue(MeetBotError.guestNotAllowed.suggestsLogin)
        XCTAssertTrue(MeetBotError.notLoggedIn.suggestsLogin)
        XCTAssertFalse(MeetBotError.denied.suggestsLogin)
        XCTAssertFalse(MeetBotError.meetingNotFound.suggestsLogin)

        XCTAssertTrue(MeetBotText.joinFailureNotice(.guestNotApproved, message: "").hasPrefix("主催者に承認されませんでした"))
        XCTAssertTrue(MeetBotText.joinFailureNotice(.guestNotAllowed, message: "").hasPrefix("この会議はゲストが入れません"))
        XCTAssertNotEqual(
            MeetBotError.guestNotApproved.description, MeetBotError.guestNotAllowed.description,
            "画面に出す理由も区別すること")
        XCTAssertTrue(MeetBotText.waitingNotice(mode: .guest, displayName: "voicekey 議事録ボット").contains("5 分"))
    }
}
