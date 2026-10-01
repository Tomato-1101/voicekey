//
//  ApiUsageBackfillTests.swift
//  計測開始前の使用量をログ・統計から推定する処理を確かめる（一時ディレクトリだけ・通信なし・課金なし）
//

import XCTest
@testable import voicekey

final class ApiUsageBackfillTests: XCTestCase {

    private func makeTempDir() -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("ApiUsageBackfillTests-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func req(_ route: String, _ backend: String, _ model: String, _ sec: Double)
        -> ApiUsageBackfill.LoggedRequest {
        .init(route: route, backend: backend, model: model, seconds: sec)
    }

    // MARK: - ログ行の解析

    func testParseLogLine() {
        let line = "01:28:01.067 [transcriber] 文字起こし要求 経路=streaming backend=soniox model=stt-rt-v5 音声=12.83s"
        XCTAssertEqual(ApiUsageBackfill.parseLogLine(line), req("streaming", "soniox", "stt-rt-v5", 12.83))
        // 日本語のモデル名（空白なし）・REST 経路も読める
        XCTAssertEqual(
            ApiUsageBackfill.parseLogLine("01:36:37.893 [transcriber] 文字起こし要求 経路=rest backend=apple_local model=オンデバイス音声認識 音声=0.55s"),
            req("rest", "apple_local", "オンデバイス音声認識", 0.55))
        // 別カテゴリ・別の文言・秒数が壊れた行は拾わない
        XCTAssertNil(ApiUsageBackfill.parseLogLine("00:05:00.803 [app] ホットキー押下 slot=1 mode=hold backend=groq model=x"))
        XCTAssertNil(ApiUsageBackfill.parseLogLine("01:28:01.067 [transcriber] 文字起こし要求 経路=rest backend=groq model=m 音声=abcs"))
        XCTAssertNil(ApiUsageBackfill.parseLogLine(""))
    }

    /// backend → provider・モデル名の対応。Apple は取り込まず、openai_live の REST は gpt-transcribe で数える
    func testBillingTarget() {
        XCTAssertNil(ApiUsageBackfill.billingTarget(for: req("streaming", "apple_local", "x", 1)))
        XCTAssertNil(ApiUsageBackfill.billingTarget(for: req("rest", "unknown_backend", "x", 1)))
        let live = ApiUsageBackfill.billingTarget(for: req("streaming", "openai_live", "gpt-live-transcribe", 1))
        XCTAssertEqual(live?.provider, .openai)
        XCTAssertEqual(live?.model, "gpt-live-transcribe")
        let liveRest = ApiUsageBackfill.billingTarget(for: req("rest", "openai_live", "gpt-live-transcribe", 1))
        XCTAssertEqual(liveRest?.model, "gpt-transcribe")
        XCTAssertEqual(ApiUsageBackfill.billingTarget(for: req("rest", "azure_mai", "mai-transcribe-2", 1))?.provider, .microsoft)
    }

    /// 集計は provider × モデルごとに 1 行・推定フラグ付き。Groq は 1 要求ごとに最低 10 秒課金
    func testAggregate() throws {
        let rows = ApiUsageBackfill.aggregate([
            req("rest", "groq", "whisper-large-v3-turbo", 3),
            req("rest", "groq", "whisper-large-v3-turbo", 25),
            req("streaming", "soniox", "stt-rt-v5", 60),
            req("streaming", "apple_local", "オンデバイス音声認識", 99),
        ])
        XCTAssertEqual(rows.count, 2)
        let groq = try XCTUnwrap(rows.first { $0.provider == "groq" })
        XCTAssertEqual(groq.requests, 2)
        XCTAssertEqual(groq.audioSeconds, 28, accuracy: 1e-9)
        XCTAssertEqual(groq.billedAudioSeconds, 35, accuracy: 1e-9)   // 10 + 25
        XCTAssertTrue(groq.estimated)
        XCTAssertEqual(groq.purpose, "transcription")
    }

    // MARK: - 差分（二重計上の回避）

    private func logged(_ provider: String, _ model: String, requests: Int, seconds: Double) -> ApiUsageEntry {
        var e = ApiUsageEntry(provider: provider, model: model, purpose: "transcription")
        e.requests = requests; e.audioSeconds = seconds; e.billedAudioSeconds = seconds; e.estimated = true
        return e
    }

    private func measured(_ provider: String, _ model: String, requests: Int, seconds: Double) -> ApiUsageEntry {
        var e = logged(provider, model, requests: requests, seconds: seconds)
        e.estimated = false
        return e
    }

    func testEstimatesTakeOnlyPositiveDifference() throws {
        // 実測が無い日 → ログ全部が推定
        let full = ApiUsageBackfill.estimates(
            logged: [logged("soniox", "stt-rt-v5", requests: 5, seconds: 100)], measured: [])
        XCTAssertEqual(full.first?.audioSeconds ?? 0, 100, accuracy: 1e-9)
        XCTAssertEqual(full.first?.requests, 5)

        // 計測の途中から始まった日 → 差分だけ（100 - 40 = 60 秒・5 - 2 = 3 回）
        let partial = try XCTUnwrap(ApiUsageBackfill.estimates(
            logged: [logged("soniox", "stt-rt-v5", requests: 5, seconds: 100)],
            measured: [measured("soniox", "stt-rt-v5", requests: 2, seconds: 40)]).first)
        XCTAssertEqual(partial.audioSeconds, 60, accuracy: 1e-9)
        XCTAssertEqual(partial.requests, 3)
        XCTAssertEqual(partial.billedAudioSeconds, 60, accuracy: 1e-9)
        XCTAssertTrue(partial.estimated)

        // 実測のほうが多い（末尾無音ぶんなど）・誤差内（1 秒以下 / 2 % 以下）は推定しない
        for secondsMeasured in [100.0, 130.0, 99.5, 98.5] {
            XCTAssertTrue(ApiUsageBackfill.estimates(
                logged: [logged("soniox", "stt-rt-v5", requests: 5, seconds: 100)],
                measured: [measured("soniox", "stt-rt-v5", requests: 5, seconds: secondsMeasured)]).isEmpty,
                "実測 \(secondsMeasured) 秒なら差分なし")
        }
        // 別モデルの実測は差し引かない
        XCTAssertEqual(ApiUsageBackfill.estimates(
            logged: [logged("openai", "gpt-live-transcribe", requests: 1, seconds: 30)],
            measured: [measured("openai", "gpt-transcribe", requests: 1, seconds: 30)]).count, 1)
        // 既存の推定行は「実測」に数えない（作り直しても二重にならない）
        XCTAssertEqual(ApiUsageBackfill.estimates(
            logged: [logged("groq", "whisper-large-v3-turbo", requests: 1, seconds: 30)],
            measured: [logged("groq", "whisper-large-v3-turbo", requests: 1, seconds: 30)]).count, 1)
    }

    // MARK: - 統計（ログが無い古い日）

    func testLogDayFromFileName() {
        XCTAssertEqual(ApiUsageBackfill.logDay(fileName: "voicekey-2026-09-18.log"), "2026-09-18")
        XCTAssertEqual(ApiUsageBackfill.logDay(fileName: "voicekey-2026-09-18.2.log"), "2026-09-18")
        XCTAssertNil(ApiUsageBackfill.logDay(fileName: "app.log"))
        XCTAssertNil(ApiUsageBackfill.logDay(fileName: "voicekey-2026-09-18-x.log"))
        XCTAssertNil(ApiUsageBackfill.logDay(fileName: "voicekey-abcd-ef-gh.log"))
    }

    /// 統計を使うのは「ログの最古日・実測の最古日」のどちらよりも前の日だけ
    func testStatsCutoffAndEstimates() throws {
        XCTAssertEqual(ApiUsageBackfill.statsCutoffDay(oldestLogDay: "2026-09-18", earliestMeasuredDay: "2026-10-02"), "2026-09-18")
        XCTAssertEqual(ApiUsageBackfill.statsCutoffDay(oldestLogDay: nil, earliestMeasuredDay: "2026-10-02"), "2026-10-02")
        XCTAssertNil(ApiUsageBackfill.statsCutoffDay(oldestLogDay: nil, earliestMeasuredDay: nil))

        let daily: [String: ApiUsageBackfill.StatsDay] = [
            "2026-09-02": .init(sessions: 10, recordingSeconds: 200),   // 平均 20 秒 → 課金 200
            "2026-09-03": .init(sessions: 10, recordingSeconds: 50),    // 平均 5 秒 → 最低 10 秒で 100
            "2026-09-17": .init(sessions: 1, recordingSeconds: 0),      // 録音 0 秒は無視
            "2026-09-18": .init(sessions: 3, recordingSeconds: 60),     // cutoff の日はログ側
            "2026-10-02": .init(sessions: 3, recordingSeconds: 60),
            "bad-key": .init(sessions: 3, recordingSeconds: 60),
        ]
        let rows = ApiUsageBackfill.statsEstimates(daily: daily, before: "2026-09-18")
        XCTAssertEqual(Set(rows.keys), ["2026-09-02", "2026-09-03"])
        let d2 = try XCTUnwrap(rows["2026-09-02"])
        XCTAssertEqual(d2.provider, "groq")
        XCTAssertEqual(d2.model, "whisper-large-v3-turbo")
        XCTAssertEqual(d2.requests, 10)
        XCTAssertEqual(d2.billedAudioSeconds, 200, accuracy: 1e-9)
        XCTAssertTrue(d2.estimated)
        XCTAssertEqual(try XCTUnwrap(rows["2026-09-03"]).billedAudioSeconds, 100, accuracy: 1e-9)
        // cutoff が無ければ録音秒数のある全日（不正なキー・録音 0 秒は除く）
        XCTAssertEqual(ApiUsageBackfill.statsEstimates(daily: daily, before: nil).count, 4)
    }

    // MARK: - 取り込み計画と、ストアへの 1 回だけの反映

    func testPlanSkipsLoggedRangeForStats() {
        var sources = ApiUsageBackfill.Sources()
        sources.loggedByDay["2026-09-18"] = [req("rest", "groq", "whisper-large-v3-turbo", 20)]
        sources.statsDaily = [
            "2026-09-10": .init(sessions: 2, recordingSeconds: 40),
            "2026-09-19": .init(sessions: 9, recordingSeconds: 90),   // ログ範囲内の日は統計から足さない
        ]
        let plan = ApiUsageBackfill.plan(sources: sources, measuredDays: [:])
        XCTAssertEqual(Set(plan.keys), ["2026-09-18", "2026-09-10"])
    }

    func testStoreBackfillRunsOnceAndNeverTouchesMeasured() throws {
        let storeDir = makeTempDir()
        let logsDir = makeTempDir()
        let statsFile = makeTempDir().appendingPathComponent("stats.json")

        // 9/18 と 9/19（連番ローテート分もある）のログ。10/02 は計測が途中から始まった日
        let log18 = """
        00:05:00.803 [app] ホットキー押下 slot=1 mode=hold backend=groq model=x
        00:05:02.503 [transcriber] 文字起こし要求 経路=rest backend=groq model=whisper-large-v3-turbo 音声=30.00s
        00:06:02.503 [transcriber] 文字起こし要求 経路=streaming backend=apple_local model=オンデバイス音声認識 音声=5.00s
        """
        try log18.write(to: logsDir.appendingPathComponent("voicekey-2026-09-18.log"), atomically: true, encoding: .utf8)
        try "00:00:01.000 [transcriber] 文字起こし要求 経路=streaming backend=soniox model=stt-rt-v5 音声=60.00s\n"
            .write(to: logsDir.appendingPathComponent("voicekey-2026-09-19.log"), atomically: true, encoding: .utf8)
        try "00:00:02.000 [transcriber] 文字起こし要求 経路=streaming backend=soniox model=stt-rt-v5 音声=40.00s\n"
            .write(to: logsDir.appendingPathComponent("voicekey-2026-09-19.2.log"), atomically: true, encoding: .utf8)
        try "01:00:00.000 [transcriber] 文字起こし要求 経路=streaming backend=soniox model=stt-rt-v5 音声=100.00s\n"
            .write(to: logsDir.appendingPathComponent("voicekey-2026-10-02.log"), atomically: true, encoding: .utf8)
        let stats = #"{"daily":{"2026-09-02":{"sessions":10,"characters":100,"recordingSeconds":200},"2026-10-02":{"sessions":1,"characters":5,"recordingSeconds":100}}}"#
        try stats.write(to: statsFile, atomically: true, encoding: .utf8)

        // 10/02 は実測が 40 秒ぶん済んでいる
        let pre = ApiUsageStore(directory: storeDir)
        let day = try XCTUnwrap(DateComponents(calendar: .current, year: 2026, month: 10, day: 2, hour: 12).date)
        pre.recordAudio(provider: .soniox, model: "stt-rt-v5", seconds: 40, date: day)
        pre.flush()

        let store = ApiUsageStore(directory: storeDir)
        store.backfillIfNeeded(logsDirectory: logsDir, statsFile: statsFile)
        store.flush()
        let snap = store.snapshot
        XCTAssertEqual(snap.backfillVersion, ApiUsageBackfill.version)

        // 9/18: Groq 30 秒（Apple は取り込まない）／9/19: Soniox 100 秒（2 ファイル合算）
        XCTAssertEqual(snap.days["2026-09-18"]?.count, 1)
        XCTAssertEqual(snap.days["2026-09-18"]?.first?.audioSeconds ?? 0, 30, accuracy: 1e-9)
        XCTAssertEqual(snap.days["2026-09-19"]?.first?.audioSeconds ?? 0, 100, accuracy: 1e-9)
        // 9/02 は統計から Groq と仮定（ログ最古日より前）
        XCTAssertEqual(snap.days["2026-09-02"]?.first?.provider, "groq")
        XCTAssertEqual(snap.days["2026-09-02"]?.first?.estimated, true)
        // 10/02: 実測 40 秒は残り、差分 60 秒だけが推定。統計の 10/02 は使われない
        let oct2 = try XCTUnwrap(snap.days["2026-10-02"])
        XCTAssertEqual(oct2.count, 2)
        XCTAssertEqual(oct2.first { !$0.estimated }?.audioSeconds ?? 0, 40, accuracy: 1e-9)
        XCTAssertEqual(oct2.first { $0.estimated }?.audioSeconds ?? 0, 60, accuracy: 1e-9)
        XCTAssertEqual(oct2.filter { $0.provider == "groq" }.count, 0)

        // 2 回目は何もしない（フラグ済み）。再起動して読み直しても重複しない
        let before = store.snapshot
        store.backfillIfNeeded(logsDirectory: logsDir, statsFile: statsFile)
        XCTAssertEqual(store.snapshot, before)
        let reloaded = ApiUsageStore(directory: storeDir)
        XCTAssertEqual(reloaded.snapshot, before)

        // 版が上がって再実行されても推定行は作り直すだけで増えない
        var reset = reloaded.snapshot
        reset.backfillVersion = 0
        try JSONEncoder().encode(reset).write(to: storeDir.appendingPathComponent("api-usage.json"))
        let again = ApiUsageStore(directory: storeDir)
        again.backfillIfNeeded(logsDirectory: logsDir, statsFile: statsFile)
        XCTAssertEqual(again.snapshot, before)
    }

    /// ログも統計も無ければ何も取り込まず、フラグだけ立てる
    func testBackfillWithNoSourcesOnlySetsFlag() {
        let store = ApiUsageStore(directory: makeTempDir())
        store.backfillIfNeeded(
            logsDirectory: makeTempDir().appendingPathComponent("none"),
            statsFile: makeTempDir().appendingPathComponent("none.json"))
        XCTAssertTrue(store.snapshot.days.isEmpty)
        XCTAssertEqual(store.snapshot.backfillVersion, ApiUsageBackfill.version)
    }
}
