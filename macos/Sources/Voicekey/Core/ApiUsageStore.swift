//
//  ApiUsageStore.swift
//  API 使用量の日次集計と永続化（ホームの「API の利用料金」の元データ）
//
//  API 呼び出し 1 回ごとに「日付・プロバイダー・モデル・用途・数量」を積み上げる。
//  保存するのは数量だけ（音声秒数・トークン数・回数）。発話本文や API キーは受け取らない。
//  料金は表示時に ApiPricing から計算する（単価を直せば過去分にも効く・未確認単価を隠さない）。
//
//  記録はどのスレッドからでも同期で呼べる（ロックで守る）。音声入力の経路から呼ばれるので
//  待ちは足さない: 加算はメモリ上で一瞬、ファイル保存は直列キューで後回しにする。
//  常駐中は App Nap で Task{@MainActor}・Timer が止まることがあるため、記録自体は
//  メインスレッドに依存させない（UI への変更通知だけをメインへ投げる）。
//

import Foundation
import os.log

private let apiUsageLog = Logger(subsystem: "com.voicekey.app", category: "api-usage")

/// 1 日 × プロバイダー × モデル × 用途 の集計 1 行
struct ApiUsageEntry: Codable, Equatable {
    /// ApiProvider.rawValue（未知の値でも読み込みごと失敗しないよう文字列で持つ）
    var provider: String
    var model: String
    /// ApiUsagePurpose.rawValue
    var purpose: String
    /// API を呼んだ回数
    var requests: Int = 0
    /// 実際に送った音声の秒数
    var audioSeconds: Double = 0
    /// 課金対象の秒数（最低課金秒数を 1 リクエストごとに適用した後）
    var billedAudioSeconds: Double = 0
    var inputTokens: Int = 0
    var outputTokens: Int = 0
    /// 計測開始前の使用量をログ・統計から推定した行（実測の行とは別行として持つ＝推定分だけ後から作り直せる）
    var estimated: Bool = false

    var providerKind: ApiProvider? { ApiProvider(rawValue: provider) }
    var purposeKind: ApiUsagePurpose? { ApiUsagePurpose(rawValue: purpose) }

    /// 料金（USD）。nil = 単価未確認。「実際に払った分」モードで無料枠のプロバイダーは 0 円
    func costUSD(mode: ApiCostMode) -> Double? {
        if mode.isFreeTier(providerKind) { return 0 }
        guard let p = providerKind else { return nil }
        return ApiPricing.cost(
            provider: p, model: model, billedAudioSeconds: billedAudioSeconds,
            inputTokens: inputTokens, outputTokens: outputTokens)
    }

    /// 同じキーの行を足し合わせる
    mutating func add(_ other: ApiUsageEntry) {
        requests += other.requests
        audioSeconds += other.audioSeconds
        billedAudioSeconds += other.billedAudioSeconds
        inputTokens += other.inputTokens
        outputTokens += other.outputTokens
    }

    func sameKey(_ other: ApiUsageEntry) -> Bool {
        provider == other.provider && model == other.model && purpose == other.purpose
            && estimated == other.estimated
    }
}

extension ApiUsageEntry {
    /// 後から項目を増やしても古い JSON を読めるよう decodeIfPresent で読む
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        provider = try c.decodeIfPresent(String.self, forKey: .provider) ?? ""
        model = try c.decodeIfPresent(String.self, forKey: .model) ?? ""
        purpose = try c.decodeIfPresent(String.self, forKey: .purpose) ?? ""
        requests = try c.decodeIfPresent(Int.self, forKey: .requests) ?? 0
        audioSeconds = try c.decodeIfPresent(Double.self, forKey: .audioSeconds) ?? 0
        billedAudioSeconds = try c.decodeIfPresent(Double.self, forKey: .billedAudioSeconds) ?? 0
        inputTokens = try c.decodeIfPresent(Int.self, forKey: .inputTokens) ?? 0
        outputTokens = try c.decodeIfPresent(Int.self, forKey: .outputTokens) ?? 0
        estimated = try c.decodeIfPresent(Bool.self, forKey: .estimated) ?? false
    }
}

/// 永続化する全体（キー = ローカル yyyy-MM-dd）
struct ApiUsageData: Codable, Equatable {
    var days: [String: [ApiUsageEntry]] = [:]
    /// 過去分の推定取り込み（ApiUsageBackfill）を済ませた版。0 = 未実施
    var backfillVersion: Int = 0

    init(days: [String: [ApiUsageEntry]] = [:], backfillVersion: Int = 0) {
        self.days = days
        self.backfillVersion = backfillVersion
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        days = try c.decodeIfPresent([String: [ApiUsageEntry]].self, forKey: .days) ?? [:]
        backfillVersion = try c.decodeIfPresent(Int.self, forKey: .backfillVersion) ?? 0
    }
}

/// 料金の合計（単価未確認の行を含むかどうかも持つ＝0 円に見せて隠さないため）
struct ApiCostSummary: Equatable {
    var usd: Double = 0
    var hasUnpriced: Bool = false
    var requests: Int = 0
    /// usd のうち推定行（計測開始前をログ・統計から推定した分）の合計
    var estimatedUSD: Double = 0
}

/// 日次の推移 1 本ぶん
struct ApiCostDay: Identifiable, Equatable {
    var id: String { day }
    let day: String
    let summary: ApiCostSummary
}

final class ApiUsageStore: ObservableObject, @unchecked Sendable {

    /// アプリ全体で共有するストア。UI スナップショットの検証時だけ差し替える
    nonisolated(unsafe) static var shared = ApiUsageStore()

    private let fileURL: URL
    private let lock = NSLock()
    private var data = ApiUsageData()
    /// ファイル保存は直列で後回しにする（記録した側を待たせない）
    private let saveQueue = DispatchQueue(label: "com.voicekey.api-usage.save", qos: .utility)

    /// - Parameter directory: 保存先（テストで一時ディレクトリを注入する）。nil なら
    ///   ~/Library/Application Support/voicekey。テスト実行中は本人の実ファイルを汚さないよう一時ディレクトリにする。
    init(directory: URL? = nil) {
        let dir: URL
        if let directory {
            dir = directory
        } else if Self.isRunningTests {
            dir = FileManager.default.temporaryDirectory
                .appendingPathComponent("voicekey-api-usage-tests-\(UUID().uuidString)", isDirectory: true)
        } else {
            dir = FileManager.default
                .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("voicekey", isDirectory: true)
        }
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        fileURL = dir.appendingPathComponent("api-usage.json")
        if let raw = try? Data(contentsOf: fileURL),
           let loaded = try? JSONDecoder().decode(ApiUsageData.self, from: raw) {
            data = loaded
        }
    }

    private static var isRunningTests: Bool {
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
            || NSClassFromString("XCTestCase") != nil
    }

    // MARK: - 記録（どのスレッドからでも呼べる）

    /// 音声の文字起こし 1 リクエストを記録する。最低課金秒数はここで 1 回ごとに適用する
    func recordAudio(
        provider: ApiProvider, model: String, purpose: ApiUsagePurpose = .transcription,
        seconds: Double, date: Date = Date()
    ) {
        guard seconds > 0 else { return }
        var e = ApiUsageEntry(provider: provider.rawValue, model: model, purpose: purpose.rawValue)
        e.requests = 1
        e.audioSeconds = seconds
        e.billedAudioSeconds = ApiPricing.billedSeconds(provider: provider, model: model, audioSeconds: seconds)
        add(e, date: date)
    }

    /// LLM 1 リクエストを記録する（トークン数は応答の usage から）
    func recordTokens(
        provider: ApiProvider, model: String, purpose: ApiUsagePurpose,
        inputTokens: Int, outputTokens: Int, date: Date = Date()
    ) {
        var e = ApiUsageEntry(provider: provider.rawValue, model: model, purpose: purpose.rawValue)
        e.requests = 1
        e.inputTokens = max(0, inputTokens)
        e.outputTokens = max(0, outputTokens)
        add(e, date: date)
    }

    private func add(_ entry: ApiUsageEntry, date: Date) {
        let day = Self.dayString(date)
        lock.lock()
        var rows = data.days[day] ?? []
        if let i = rows.firstIndex(where: { $0.sameKey(entry) }) {
            rows[i].add(entry)
        } else {
            rows.append(entry)
        }
        data.days[day] = rows
        lock.unlock()
        scheduleSave()
        // 画面への反映だけメインへ（記録した側は待たない）
        DispatchQueue.main.async { [weak self] in self?.objectWillChange.send() }
    }

    /// OpenAI 互換のチャット応答（Groq）の usage を読んで記録する。
    /// 通常の応答は `usage`、Groq のストリーミング最終チャンクは `x_groq.usage` に入るので両方を見る。
    /// - Returns: usage が見つかって記録したら true（SSE では usage の載った行だけ true になる）
    @discardableResult
    func recordChatUsage(
        fromJSON json: Data, provider: ApiProvider, model: String, purpose: ApiUsagePurpose
    ) -> Bool {
        guard let env = try? JSONDecoder().decode(ChatUsageEnvelope.self, from: json),
              let usage = env.usage ?? env.x_groq?.usage,
              usage.prompt_tokens != nil || usage.completion_tokens != nil
        else { return false }
        recordTokens(
            provider: provider, model: model, purpose: purpose,
            inputTokens: usage.prompt_tokens ?? 0, outputTokens: usage.completion_tokens ?? 0)
        return true
    }

    /// Gemini の応答の usageMetadata を読んで記録する。思考トークンは出力として課金されるので出力に足す
    @discardableResult
    func recordGeminiUsage(fromJSON json: Data, model: String, purpose: ApiUsagePurpose) -> Bool {
        guard let env = try? JSONDecoder().decode(GeminiUsageEnvelope.self, from: json),
              let meta = env.usageMetadata
        else { return false }
        recordTokens(
            provider: .gemini, model: model, purpose: purpose,
            inputTokens: meta.promptTokenCount ?? 0,
            outputTokens: (meta.candidatesTokenCount ?? 0) + (meta.thoughtsTokenCount ?? 0))
        return true
    }

    private struct GeminiUsageEnvelope: Decodable {
        struct Meta: Decodable {
            let promptTokenCount: Int?
            let candidatesTokenCount: Int?
            let thoughtsTokenCount: Int?
        }
        let usageMetadata: Meta?
    }

    /// 応答 JSON のうち usage だけを読む（本文には触れない）
    private struct ChatUsageEnvelope: Decodable {
        struct Usage: Decodable {
            let prompt_tokens: Int?
            let completion_tokens: Int?
        }
        struct XGroq: Decodable { let usage: Usage? }
        let usage: Usage?
        let x_groq: XGroq?
    }

    // MARK: - 過去分の推定取り込み（1 回だけ）

    /// 計測開始前の使用量をログ・統計から推定して取り込む。取り込み済み（backfillVersion）なら何もしない。
    /// ファイル読み込みはロックの外で行い、呼び出し側（起動時のバックグラウンド）以外では呼ばない。
    /// 推定行は毎回すべて作り直す＝版を上げて再実行しても重複しない。ログ・統計が無ければ空のまま版だけ立てる。
    func backfillIfNeeded(
        logsDirectory: URL = ActionLog.defaultDirectory,
        statsFile: URL? = nil
    ) {
        lock.lock()
        let done = data.backfillVersion >= ApiUsageBackfill.version
        lock.unlock()
        if done { return }

        let stats = statsFile ?? FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("voicekey/stats.json")
        let sources = ApiUsageBackfill.readSources(logsDirectory: logsDirectory, statsFile: stats)

        lock.lock()
        // 実測との突き合わせと書き込みは同じロックの中で（読んでいる間に増えた実測も差し引くため）
        let plan = ApiUsageBackfill.plan(sources: sources, measuredDays: data.days)
        for day in Array(data.days.keys) {
            let kept = (data.days[day] ?? []).filter { !$0.estimated }
            data.days[day] = kept.isEmpty ? nil : kept
        }
        for (day, rows) in plan {
            data.days[day, default: []].append(contentsOf: rows)
        }
        data.backfillVersion = ApiUsageBackfill.version
        lock.unlock()
        scheduleSave()
        DispatchQueue.main.async { [weak self] in self?.objectWillChange.send() }
    }

    /// 保存は書き込む時点の最新内容を使う（記録が前後しても古い内容で上書きしない）
    private func scheduleSave() {
        saveQueue.async { [weak self] in
            guard let self else { return }
            self.lock.lock()
            let snapshot = self.data
            self.lock.unlock()
            do {
                try JSONEncoder().encode(snapshot).write(to: self.fileURL, options: [.atomic])
            } catch {
                apiUsageLog.error("API 使用量の保存に失敗: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    /// 保留中の保存が終わるまで待つ（テスト用）
    func flush() { saveQueue.sync {} }

    // MARK: - 集計（表示用）

    /// 全日分のコピー
    var snapshot: ApiUsageData {
        lock.lock(); defer { lock.unlock() }
        return data
    }

    /// 指定した日付キーの行をまとめた合計
    func summary(mode: ApiCostMode, where include: (String) -> Bool) -> ApiCostSummary {
        var s = ApiCostSummary()
        for (day, rows) in snapshot.days where include(day) {
            for r in rows { Self.accumulate(&s, r, mode: mode) }
        }
        return s
    }

    func todaySummary(mode: ApiCostMode, now: Date = Date()) -> ApiCostSummary {
        let today = Self.dayString(now)
        return summary(mode: mode) { $0 == today }
    }

    func monthSummary(mode: ApiCostMode, now: Date = Date()) -> ApiCostSummary {
        let month = String(Self.dayString(now).prefix(7))
        return summary(mode: mode) { $0.hasPrefix(month) }
    }

    func allTimeSummary(mode: ApiCostMode) -> ApiCostSummary { summary(mode: mode) { _ in true } }

    /// プロバイダー × モデル × 用途（× 実測/推定）の内訳（料金の高い順。未確認は末尾）
    func breakdown(mode: ApiCostMode, where include: (String) -> Bool) -> [ApiUsageEntry] {
        var merged: [ApiUsageEntry] = []
        for (day, rows) in snapshot.days where include(day) {
            for r in rows {
                if let i = merged.firstIndex(where: { $0.sameKey(r) }) {
                    merged[i].add(r)
                } else {
                    merged.append(r)
                }
            }
        }
        return merged.sorted { ($0.costUSD(mode: mode) ?? -1) > ($1.costUSD(mode: mode) ?? -1) }
    }

    /// 直近 numDays 日（今日を末尾・古い順）。記録の無い日は 0
    func dailySeries(_ numDays: Int, mode: ApiCostMode, endingAt end: Date = Date()) -> [ApiCostDay] {
        let cal = Calendar.current
        let all = snapshot.days
        return (0..<numDays).reversed().compactMap { offset in
            guard let d = cal.date(byAdding: .day, value: -offset, to: end) else { return nil }
            let key = Self.dayString(d)
            var s = ApiCostSummary()
            for r in all[key] ?? [] { Self.accumulate(&s, r, mode: mode) }
            return ApiCostDay(day: key, summary: s)
        }
    }

    private static func accumulate(_ s: inout ApiCostSummary, _ r: ApiUsageEntry, mode: ApiCostMode) {
        s.requests += r.requests
        if let c = r.costUSD(mode: mode) {
            s.usd += c
            if r.estimated { s.estimatedUSD += c }
        } else {
            s.hasUnpriced = true
        }
    }

    /// ローカルタイムゾーンでの yyyy-MM-dd（StatsStore と同じ書式）
    static func dayString(_ date: Date) -> String {
        let f = DateFormatter()
        f.calendar = Calendar.current
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }
}
