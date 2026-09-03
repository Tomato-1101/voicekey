//
//  HistorySync.swift
//  Cloudflare Worker 経由の Mac ⇄ Windows 履歴同期
//
//  HistoryStore の追加通知からは utility キューへ積むだけにし、ファイル I/O・HTTP・
//  バックオフを 1 本の直列キューで処理する。録音から貼り付けまでの経路を待たせない。
//


import Combine
import CryptoKit
import Foundation
import OSLog

private let historySyncLog = Logger(subsystem: "com.voicekey.app", category: "history-sync")

final class HistorySync: ObservableObject, @unchecked Sendable {

    struct Configuration: Equatable {
        var enabled: Bool
        var url: String
    }

    static let batchSize = 200
    static let maxCacheItems = 200
    static let syncInterval: TimeInterval = 300
    static let initialBackoff: TimeInterval = 10
    static let maximumBackoff: TimeInterval = 300

    @Published private(set) var pendingCount = 0
    @Published private(set) var lastSyncAt: Date?
    @Published private(set) var errorMessage: String?
    @Published private(set) var tokenConfigured = false

    private weak var history: HistoryStore?
    private let configurationProvider: @MainActor () -> Configuration
    private let tokenProvider: () -> String?
    private let tokenWriter: (String) -> Bool
    private let tokenDeleter: () -> Bool
    private let now: () -> Date
    private let uptime: () -> TimeInterval
    private let automaticScheduling: Bool
    private let queue = DispatchQueue(label: "com.voicekey.history-sync", qos: .utility)
    private let session: URLSession
    private let outboxURL: URL
    private let cacheURL: URL

    // 以下の可変状態は queue 上でのみ触る。
    private var configuration = Configuration(enabled: false, url: "")
    private var outbox: [OutboundItem]
    private var cache: CacheFile
    private var tokenInvalid = false
    private var inErrorStreak = false
    private var backoff: TimeInterval = 0
    private var nextAllowedAt: TimeInterval = 0
    private var lastSync: Date?
    private var lastError: String?
    private var periodicTimer: DispatchSourceTimer?
    private var retryTimer: DispatchSourceTimer?
    private var stopped = false

    @MainActor
    convenience init(history: HistoryStore, config: ConfigStore) {
        self.init(
            history: history,
            configuration: {
                Configuration(enabled: config.historySyncEnabled, url: config.historySyncURL)
            }
        )
    }

    @MainActor
    init(
        history: HistoryStore,
        configuration: @escaping @MainActor () -> Configuration,
        directory: URL? = nil,
        session: URLSession? = nil,
        tokenProvider: @escaping () -> String? = Keychain.syncToken,
        tokenWriter: @escaping (String) -> Bool = Keychain.setSyncToken,
        tokenDeleter: @escaping () -> Bool = Keychain.deleteSyncToken,
        now: @escaping () -> Date = Date.init,
        uptime: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        automaticScheduling: Bool = true
    ) {
        self.history = history
        self.configurationProvider = configuration
        self.tokenProvider = tokenProvider
        self.tokenWriter = tokenWriter
        self.tokenDeleter = tokenDeleter
        self.now = now
        self.uptime = uptime
        self.automaticScheduling = automaticScheduling

        let base = directory ?? FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("voicekey", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        outboxURL = base.appendingPathComponent("sync_outbox.json")
        cacheURL = base.appendingPathComponent("sync_cloud_cache.json")
        outbox = Self.loadOutbox(from: outboxURL)
        cache = Self.loadCache(from: cacheURL)

        if let session {
            self.session = session
        } else {
            let sessionConfiguration = URLSessionConfiguration.ephemeral
            sessionConfiguration.timeoutIntervalForRequest = 10
            sessionConfiguration.timeoutIntervalForResource = 10
            self.session = URLSession(configuration: sessionConfiguration)
        }

        pendingCount = outbox.count
        history.setCloudItems(cache.items.compactMap(Self.makeHistoryItem))
        if automaticScheduling { startPeriodicTimer() }
    }

    deinit {
        periodicTimer?.cancel()
        retryTimer?.cancel()
    }

    // MARK: - 公開 API

    /// 設定を読み直し、401 停止とバックオフを解除して即時同期する。
    @MainActor
    func applyConfig() {
        let next = configurationProvider()
        queue.async { [weak self] in
            guard let self, !self.stopped else { return }
            self.configuration = Configuration(
                enabled: next.enabled,
                url: next.url.trimmingCharacters(in: .whitespacesAndNewlines)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            )
            self.tokenInvalid = false
            self.inErrorStreak = false
            self.backoff = 0
            self.nextAllowedAt = 0
            self.retryTimer?.cancel()
            self.retryTimer = nil
            self.lastError = nil

            let token = self.tokenProvider()
            self.publishState(tokenConfigured: token?.isEmpty == false)
            if self.configuration.enabled {
                ActionLog.shared.write("history-sync", "履歴同期: 有効 (\(self.configuration.url))")
            } else {
                ActionLog.shared.write("history-sync", "履歴同期: 無効")
            }
            if self.automaticScheduling {
                self.runCycle(forceFetch: true, token: token)
            }
        }
    }

    /// ローカル追加を送信待ちへ積み、通信サイクルを起こす。呼び出し元は待たせない。
    @MainActor
    func enqueue(_ item: HistoryItem) {
        queue.async { [weak self] in
            guard let self, !self.stopped else { return }
            self.outbox.append(OutboundItem(item: item))
            self.saveOutbox()
            self.publishState()
            if self.automaticScheduling {
                self.runCycle(forceFetch: true)
            }
        }
    }

    /// 履歴 UI を開いたときに、5 分周期を待たず受信を要求する。
    @MainActor
    func requestFetch() {
        queue.async { [weak self] in
            guard let self, self.automaticScheduling else { return }
            self.runCycle(forceFetch: true)
        }
    }

    /// UI から共有トークンを保存する。値はログにも表示状態にも保持しない。
    @MainActor
    @discardableResult
    func saveToken(_ token: String) -> Bool {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        let ok = trimmed.isEmpty ? tokenDeleter() : tokenWriter(trimmed)
        applyConfig()
        return ok
    }

    /// UI から共有トークンを削除する。
    @MainActor
    @discardableResult
    func deleteToken() -> Bool {
        let ok = tokenDeleter()
        applyConfig()
        return ok
    }

    /// https を必須とし、ローカル検証だけ http を許可する。
    static func isAllowedServerURL(_ value: String) -> Bool {
        guard let url = URL(string: value.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = url.scheme?.lowercased(),
              let host = url.host?.lowercased(), !host.isEmpty else { return false }
        if scheme == "https" { return true }
        return scheme == "http" && (host == "127.0.0.1" || host == "localhost")
    }

    /// アプリ終了時にタイマーを止める。
    func shutdown() {
        queue.sync {
            stopped = true
            periodicTimer?.cancel()
            periodicTimer = nil
            retryTimer?.cancel()
            retryTimer = nil
        }
    }

    // MARK: - テスト用フック（実通信・実 Keychain を使わず状態を固定する）

    func runCycleForTesting(forceFetch: Bool = true) {
        queue.sync { runCycle(forceFetch: forceFetch) }
    }

    func waitForIdleForTesting() {
        queue.sync {}
    }

    var backoffForTesting: TimeInterval { queue.sync { backoff } }
    var tokenInvalidForTesting: Bool { queue.sync { tokenInvalid } }

    // MARK: - 周期実行

    private func startPeriodicTimer() {
        queue.async { [weak self] in
            guard let self, !self.stopped else { return }
            let timer = DispatchSource.makeTimerSource(queue: self.queue)
            timer.schedule(deadline: .now() + Self.syncInterval, repeating: Self.syncInterval)
            timer.setEventHandler { [weak self] in self?.runCycle(forceFetch: true) }
            self.periodicTimer = timer
            timer.resume()
        }
    }

    private func runCycle(forceFetch: Bool, token suppliedToken: String? = nil) {
        guard !stopped, configuration.enabled else { return }
        guard Self.isAllowedServerURL(configuration.url) else {
            lastError = "同期サーバー URL は https が必要です（localhost のみ http 可）"
            publishState()
            return
        }
        guard !tokenInvalid, uptime() >= nextAllowedAt else { return }

        let token = suppliedToken ?? tokenProvider()
        publishState(tokenConfigured: token?.isEmpty == false)
        guard let token, !token.isEmpty else { return }

        let requested = outbox.count
        ActionLog.shared.write("history-sync", "履歴同期要求 (送信 \(requested) 件)")

        guard let sent = flushOutbox(token: token) else { return }
        var received = 0
        if forceFetch || sent > 0 {
            guard let fetched = fetchAndMerge(token: token) else { return }
            received = fetched
        }

        recordSuccess()
        lastSync = now()
        publishState()
        ActionLog.shared.write("history-sync", "履歴同期完了 (送信 \(sent) 件, 受信 \(received) 件)")
    }

    // MARK: - 送信

    private func flushOutbox(token: String) -> Int? {
        let pending = outbox
        guard !pending.isEmpty else { return 0 }

        var sent = 0
        for start in stride(from: 0, to: pending.count, by: Self.batchSize) {
            let end = min(start + Self.batchSize, pending.count)
            let chunk = Array(pending[start..<end])
            guard let body = try? JSONEncoder().encode(PostEnvelope(items: chunk)),
                  let url = URL(string: configuration.url + "/history") else {
                recordFailure("送信データを作成できませんでした")
                return nil
            }
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.httpBody = body
            request.timeoutInterval = 10
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")

            guard let response = perform(request) else {
                recordFailure("ネットワークエラー")
                return nil
            }
            if response.status == 401 {
                handleInvalidToken()
                return nil
            }
            guard (200..<300).contains(response.status) else {
                if response.status == 503,
                   (try? JSONDecoder().decode(ErrorResponse.self, from: response.data).error)
                    == "token_not_configured" {
                    recordFailure("サーバー側でトークンが未設定です")
                } else {
                    recordFailure("HTTP \(response.status)")
                }
                return nil
            }

            let sentIDs = Set(chunk.map(\.id))
            outbox.removeAll { sentIDs.contains($0.id) }
            saveOutbox()
            sent += chunk.count
            publishState()
        }
        return sent
    }

    // MARK: - 受信

    private func fetchAndMerge(token: String) -> Int? {
        guard var components = URLComponents(string: configuration.url + "/history") else {
            recordFailure("同期サーバー URL が不正です")
            return nil
        }
        var query = [URLQueryItem(name: "limit", value: String(Self.maxCacheItems))]
        if !cache.since.isEmpty {
            query.append(URLQueryItem(name: "since", value: cache.since))
        }
        components.queryItems = query
        guard let url = components.url else {
            recordFailure("同期サーバー URL が不正です")
            return nil
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 10
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        guard let response = perform(request) else {
            recordFailure("ネットワークエラー")
            return nil
        }
        if response.status == 401 {
            handleInvalidToken()
            return nil
        }
        guard response.status == 200 else {
            if response.status == 503,
               (try? JSONDecoder().decode(ErrorResponse.self, from: response.data).error)
                == "token_not_configured" {
                recordFailure("サーバー側でトークンが未設定です")
            } else {
                recordFailure("HTTP \(response.status)")
            }
            return nil
        }
        guard let envelope = try? JSONDecoder().decode(GetEnvelope.self, from: response.data) else {
            recordFailure("受信データを解析できませんでした")
            return nil
        }

        mergeCache(envelope.items)
        return envelope.items.count
    }

    private func mergeCache(_ incoming: [CloudItem]) {
        guard !incoming.isEmpty else { return }
        var byID = Dictionary(uniqueKeysWithValues: cache.items.map { ($0.id, $0) })
        var since = cache.since
        for item in incoming where !item.id.isEmpty {
            byID[item.id] = item
            if let receivedAt = item.receivedAt, receivedAt > since {
                since = receivedAt
            }
        }
        cache = CacheFile(
            since: since,
            items: Array(byID.values.sorted {
                Self.parseDate($0.date) > Self.parseDate($1.date)
            }.prefix(Self.maxCacheItems))
        )
        saveCache()
        publishCloudItems()
    }

    // MARK: - HTTP・失敗処理

    private func perform(_ request: URLRequest) -> (status: Int, data: Data)? {
        let semaphore = DispatchSemaphore(value: 0)
        var result: (Int, Data)?
        let task = session.dataTask(with: request) { data, response, _ in
            if let http = response as? HTTPURLResponse {
                result = (http.statusCode, data ?? Data())
            }
            semaphore.signal()
        }
        task.resume()
        if semaphore.wait(timeout: .now() + 11) == .timedOut {
            task.cancel()
            return nil
        }
        return result
    }

    private func handleInvalidToken() {
        guard !tokenInvalid else { return }
        tokenInvalid = true
        lastError = "トークンが無効です。設定を保存し直すと再開します"
        retryTimer?.cancel()
        retryTimer = nil
        historySyncLog.warning("履歴同期: トークンが無効です（設定を保存し直すまで停止）")
        ActionLog.shared.write("history-sync", "履歴同期失敗 (トークンが無効です)")
        publishState()
    }

    private func recordFailure(_ reason: String) {
        lastError = reason
        if !inErrorStreak {
            historySyncLog.warning("履歴同期失敗 (\(reason, privacy: .public))")
            inErrorStreak = true
        } else {
            historySyncLog.debug("履歴同期失敗 (\(reason, privacy: .public))")
        }
        ActionLog.shared.write("history-sync", "履歴同期失敗 (\(reason))")
        backoff = min(Self.maximumBackoff, backoff == 0 ? Self.initialBackoff : backoff * 2)
        nextAllowedAt = uptime() + backoff
        if automaticScheduling { scheduleRetry(after: backoff) }
        publishState()
    }

    private func scheduleRetry(after delay: TimeInterval) {
        retryTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + delay)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            self.retryTimer?.cancel()
            self.retryTimer = nil
            self.runCycle(forceFetch: true)
        }
        retryTimer = timer
        timer.resume()
    }

    private func recordSuccess() {
        inErrorStreak = false
        backoff = 0
        nextAllowedAt = 0
        lastError = nil
        retryTimer?.cancel()
        retryTimer = nil
    }

    // MARK: - 永続化・UI 反映

    private static func loadOutbox(from url: URL) -> [OutboundItem] {
        guard let data = try? Data(contentsOf: url),
              let items = try? JSONDecoder().decode([OutboundItem].self, from: data) else { return [] }
        return items.filter { !$0.id.isEmpty }
    }

    private func saveOutbox() {
        guard let data = try? JSONEncoder().encode(outbox) else { return }
        do {
            try data.write(to: outboxURL, options: .atomic)
        } catch {
            historySyncLog.error("履歴同期: 送信待ちファイルを保存できませんでした")
        }
    }

    private static func loadCache(from url: URL) -> CacheFile {
        guard let data = try? Data(contentsOf: url),
              let cache = try? JSONDecoder().decode(CacheFile.self, from: data) else { return CacheFile() }
        return cache
    }

    private func saveCache() {
        guard let data = try? JSONEncoder().encode(cache) else { return }
        do {
            try data.write(to: cacheURL, options: .atomic)
        } catch {
            historySyncLog.error("履歴同期: 受信キャッシュを保存できませんでした")
        }
    }

    private func publishCloudItems() {
        let items = cache.items.compactMap(Self.makeHistoryItem)
        DispatchQueue.main.async { [weak history] in
            history?.setCloudItems(items)
        }
    }

    private func publishState(tokenConfigured configured: Bool? = nil) {
        let pending = outbox.count
        let sync = lastSync
        let error = lastError
        let configuredValue = configured
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.pendingCount = pending
            self.lastSyncAt = sync
            self.errorMessage = error
            if let configuredValue { self.tokenConfigured = configuredValue }
        }
    }

    // MARK: - 形式変換

    private static func makeHistoryItem(_ item: CloudItem) -> HistoryItem? {
        guard !item.text.isEmpty else { return nil }
        return HistoryItem(
            id: normalizedUUID(item.id),
            text: item.text,
            date: parseDate(item.date),
            device: item.device,
            appBundleID: nil,
            appName: item.appName,
            characters: item.characters
        )
    }

    static func parseDate(_ value: String) -> Date {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: value) { return date }
        let seconds = ISO8601DateFormatter()
        seconds.formatOptions = [.withInternetDateTime]
        return seconds.date(from: value) ?? .distantPast
    }

    static func normalizedUUID(_ value: String) -> UUID {
        if let uuid = UUID(uuidString: value) { return uuid }
        if value.count == 32, value.allSatisfy({ $0.isHexDigit }) {
            let i = value.index(value.startIndex, offsetBy: 8)
            let j = value.index(i, offsetBy: 4)
            let k = value.index(j, offsetBy: 4)
            let l = value.index(k, offsetBy: 4)
            let formatted = "\(value[..<i])-\(value[i..<j])-\(value[j..<k])-\(value[k..<l])-\(value[l...])"
            if let uuid = UUID(uuidString: formatted) { return uuid }
        }
        let bytes = Array(SHA256.hash(data: Data(value.utf8)).prefix(16))
        let tuple: uuid_t = (
            bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
            bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]
        )
        return UUID(uuid: tuple)
    }

    private static func postDate(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return formatter.string(from: date)
    }

    // MARK: - JSON 型

    private struct OutboundItem: Codable {
        let id: String
        let text: String
        let date: String
        let device: String
        let appName: String?
        let characters: Int

        enum CodingKeys: String, CodingKey {
            case id, text, date, device, characters
            case appName = "app_name"
        }

        init(item: HistoryItem) {
            id = item.id.uuidString
            text = item.text
            date = HistorySync.postDate(item.date)
            device = "mac"
            appName = item.appName
            characters = item.characters
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(String.self, forKey: .id)
            text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
            date = try c.decodeIfPresent(String.self, forKey: .date) ?? ""
            device = try c.decodeIfPresent(String.self, forKey: .device) ?? "mac"
            appName = try c.decodeIfPresent(String.self, forKey: .appName)
            characters = try c.decodeIfPresent(Int.self, forKey: .characters) ?? text.count
        }

        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(id, forKey: .id)
            try c.encode(text, forKey: .text)
            try c.encode(date, forKey: .date)
            try c.encode("mac", forKey: .device)
            if let appName { try c.encode(appName, forKey: .appName) }
            else { try c.encodeNil(forKey: .appName) }
            try c.encode(characters, forKey: .characters)
        }
    }

    private struct PostEnvelope: Encodable { let items: [OutboundItem] }

    private struct CacheFile: Codable {
        var since: String = ""
        var items: [CloudItem] = []
    }

    private struct CloudItem: Codable {
        let id: String
        let text: String
        let date: String
        let device: String
        let appName: String?
        let characters: Int
        let receivedAt: String?

        enum CodingKeys: String, CodingKey {
            case id, text, date, device, characters
            case appName = "app_name"
            case receivedAt = "received_at"
        }
    }

    private struct GetEnvelope: Decodable { let items: [CloudItem] }
    private struct ErrorResponse: Decodable { let error: String }
}
