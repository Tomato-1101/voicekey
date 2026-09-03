//
//  HistorySyncTests.swift
//  履歴同期の通信契約・停止条件・永続化・Windows 互換形式を外部通信なしで検証する
//


import Foundation
import XCTest
@testable import voicekey

private final class LockedBox<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value

    init(_ value: Value) { self.value = value }

    func withValue<T>(_ body: (inout Value) -> T) -> T {
        lock.lock(); defer { lock.unlock() }
        return body(&value)
    }
}

private final class HistorySyncURLProtocol: URLProtocol {
    typealias Handler = (URLRequest) throws -> (Int, Data)
    private static let handler = LockedBox<Handler?> (nil)

    static func setHandler(_ value: @escaping Handler) {
        handler.withValue { $0 = value }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.handler.withValue({ $0 }) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        do {
            let (status, data) = try handler(request)
            let response = HTTPURLResponse(
                url: request.url!, statusCode: status, httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

private func requestBody(_ request: URLRequest) -> Data {
    if let body = request.httpBody { return body }
    guard let stream = request.httpBodyStream else { return Data() }
    stream.open(); defer { stream.close() }
    var result = Data()
    var buffer = [UInt8](repeating: 0, count: 4096)
    while stream.hasBytesAvailable {
        let count = stream.read(&buffer, maxLength: buffer.count)
        if count <= 0 { break }
        result.append(buffer, count: count)
    }
    return result
}

@MainActor
final class HistorySyncTests: XCTestCase {

    private func makeTempDir() -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-history-sync-test-\(UUID().uuidString)")
        try! FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [HistorySyncURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    private func makeItem(
        id: UUID = UUID(), text: String = "hello", date: Date = Date(timeIntervalSince1970: 1_800_000_000),
        device: String? = "mac", appName: String? = nil
    ) -> HistoryItem {
        HistoryItem(
            id: id, text: text, date: date, device: device,
            appBundleID: nil, appName: appName, characters: text.count
        )
    }

    private func makeSync(
        directory: URL,
        history: HistoryStore,
        configuration: @escaping @MainActor () -> HistorySync.Configuration = {
            .init(enabled: true, url: "http://127.0.0.1:8765")
        },
        token: @escaping () -> String? = { "test-token" }
    ) -> HistorySync {
        HistorySync(
            history: history,
            configuration: configuration,
            directory: directory,
            session: makeSession(),
            tokenProvider: token,
            tokenWriter: { _ in true },
            tokenDeleter: { true },
            now: { Date(timeIntervalSince1970: 1_800_000_100) },
            uptime: { 100 },
            automaticScheduling: false
        )
    }

    private func apply(_ sync: HistorySync) {
        sync.applyConfig()
        sync.waitForIdleForTesting()
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }

    func testPostBodyUsesMacContractAndNullAppName() throws {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let history = HistoryStore(directory: dir)
        let bodies = LockedBox<[[String: Any]]>([])
        HistorySyncURLProtocol.setHandler { request in
            if request.httpMethod == "POST" {
                let json = try JSONSerialization.jsonObject(with: requestBody(request)) as! [String: Any]
                bodies.withValue { $0.append(json) }
                return (200, Data(#"{"accepted":1,"received_at":"2027-01-15T08:01:40.000Z"}"#.utf8))
            }
            return (200, Data(#"{"items":[]}"#.utf8))
        }
        let sync = makeSync(directory: dir, history: history); defer { sync.shutdown() }
        apply(sync)

        sync.enqueue(makeItem(text: "hello", appName: nil))
        sync.waitForIdleForTesting()
        sync.runCycleForTesting()

        let body = bodies.withValue { $0[0] }
        let item = (body["items"] as! [[String: Any]])[0]
        XCTAssertEqual(item["device"] as? String, "mac")
        XCTAssertEqual(item["characters"] as? Int, 5)
        XCTAssertTrue(item["app_name"] is NSNull)
        let date = item["date"] as! String
        XCTAssertTrue(date.hasSuffix("Z"))
        XCTAssertTrue(date.contains("."))
    }

    func testPostSplitsIntoTwoHundredItemBatches() {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let history = HistoryStore(directory: dir)
        let sizes = LockedBox<[Int]>([])
        HistorySyncURLProtocol.setHandler { request in
            if request.httpMethod == "POST" {
                let json = try JSONSerialization.jsonObject(with: requestBody(request)) as! [String: Any]
                sizes.withValue { $0.append((json["items"] as! [Any]).count) }
                return (200, Data(#"{"accepted":1,"received_at":"x"}"#.utf8))
            }
            return (200, Data(#"{"items":[]}"#.utf8))
        }
        let sync = makeSync(directory: dir, history: history); defer { sync.shutdown() }
        apply(sync)
        for i in 0..<201 { sync.enqueue(makeItem(text: "item-\(i)")) }
        sync.waitForIdleForTesting()

        sync.runCycleForTesting()

        XCTAssertEqual(sizes.withValue { $0 }, [200, 1])
    }

    func testDuplicateIDResendIsSafeAndOutboxClears() throws {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let history = HistoryStore(directory: dir)
        let accepted = LockedBox<Set<String>>([])
        HistorySyncURLProtocol.setHandler { request in
            if request.httpMethod == "POST" {
                let json = try JSONSerialization.jsonObject(with: requestBody(request)) as! [String: Any]
                let items = json["items"] as! [[String: Any]]
                accepted.withValue { set in items.forEach { set.insert($0["id"] as! String) } }
                return (200, Data(#"{"accepted":1,"received_at":"x"}"#.utf8))
            }
            return (200, Data(#"{"items":[]}"#.utf8))
        }
        let sync = makeSync(directory: dir, history: history); defer { sync.shutdown() }
        apply(sync)
        let item = makeItem(id: UUID(uuidString: "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA")!)
        sync.enqueue(item); sync.enqueue(item)
        sync.waitForIdleForTesting()

        sync.runCycleForTesting()

        XCTAssertEqual(accepted.withValue { $0.count }, 1)
        let persisted = try JSONSerialization.jsonObject(
            with: Data(contentsOf: dir.appendingPathComponent("sync_outbox.json"))
        ) as! [Any]
        XCTAssertTrue(persisted.isEmpty)
    }

    func testGetAdvancesAndSendsSinceCursor() throws {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let history = HistoryStore(directory: dir)
        let getURLs = LockedBox<[URL]>([])
        let call = LockedBox(0)
        HistorySyncURLProtocol.setHandler { request in
            getURLs.withValue { $0.append(request.url!) }
            let n = call.withValue { value in value += 1; return value }
            let received = n == 1 ? "2026-09-03T01:00:00.000Z" : "2026-09-03T02:00:00.000Z"
            let json = """
            {"items":[{"id":"1111111111111111111111111111111\(n)","text":"w\(n)","date":"2026-09-03T1\(n):00:00+09:00","device":"windows","app_name":null,"characters":2,"received_at":"\(received)"}]}
            """
            return (200, Data(json.utf8))
        }
        let sync = makeSync(directory: dir, history: history); defer { sync.shutdown() }
        apply(sync)

        sync.runCycleForTesting(); sync.runCycleForTesting()

        let urls = getURLs.withValue { $0 }
        XCTAssertNil(URLComponents(url: urls[0], resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "since" })
        XCTAssertEqual(
            URLComponents(url: urls[1], resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "since" }?.value,
            "2026-09-03T01:00:00.000Z"
        )
        let cache = try JSONSerialization.jsonObject(
            with: Data(contentsOf: dir.appendingPathComponent("sync_cloud_cache.json"))
        ) as! [String: Any]
        XCTAssertEqual(cache["since"] as? String, "2026-09-03T02:00:00.000Z")
    }

    func testUnauthorizedStopsUntilApplyConfig() {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let history = HistoryStore(directory: dir)
        let requests = LockedBox(0)
        HistorySyncURLProtocol.setHandler { _ in
            let n = requests.withValue { value in value += 1; return value }
            return n == 1
                ? (401, Data(#"{"error":"unauthorized"}"#.utf8))
                : (200, Data(#"{"items":[]}"#.utf8))
        }
        let sync = makeSync(directory: dir, history: history); defer { sync.shutdown() }
        apply(sync)

        sync.runCycleForTesting()
        XCTAssertTrue(sync.tokenInvalidForTesting)
        sync.runCycleForTesting()
        XCTAssertEqual(requests.withValue { $0 }, 1)

        apply(sync)
        sync.runCycleForTesting()
        XCTAssertFalse(sync.tokenInvalidForTesting)
        XCTAssertEqual(requests.withValue { $0 }, 2)
    }

    func testNetworkFailureKeepsOutboxAndStartsBackoff() throws {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let history = HistoryStore(directory: dir)
        HistorySyncURLProtocol.setHandler { _ in throw URLError(.notConnectedToInternet) }
        let sync = makeSync(directory: dir, history: history); defer { sync.shutdown() }
        apply(sync)
        sync.enqueue(makeItem()); sync.waitForIdleForTesting()

        sync.runCycleForTesting()

        let persisted = try JSONSerialization.jsonObject(
            with: Data(contentsOf: dir.appendingPathComponent("sync_outbox.json"))
        ) as! [Any]
        XCTAssertEqual(persisted.count, 1)
        XCTAssertEqual(sync.backoffForTesting, 10)
    }

    func testOutboxAndCacheRestoreAfterRestart() throws {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let firstHistory = HistoryStore(directory: dir)
        let disabled: @MainActor () -> HistorySync.Configuration = {
            .init(enabled: false, url: "")
        }
        let first = makeSync(directory: dir, history: firstHistory, configuration: disabled)
        apply(first)
        first.enqueue(makeItem(text: "pending")); first.waitForIdleForTesting(); first.shutdown()
        let cache = #"{"since":"cursor-1","items":[{"id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","text":"from windows","date":"2026-09-03T12:34:56+09:00","device":"windows","app_name":null,"characters":12,"received_at":"cursor-1"}]}"#
        try Data(cache.utf8).write(to: dir.appendingPathComponent("sync_cloud_cache.json"))

        let secondHistory = HistoryStore(directory: dir)
        let postCount = LockedBox(0)
        HistorySyncURLProtocol.setHandler { request in
            if request.httpMethod == "POST" {
                postCount.withValue { $0 += 1 }
                return (200, Data(#"{"accepted":1,"received_at":"cursor-2"}"#.utf8))
            }
            return (200, Data(#"{"items":[]}"#.utf8))
        }
        let second = makeSync(directory: dir, history: secondHistory); defer { second.shutdown() }
        XCTAssertEqual(second.pendingCount, 1)
        XCTAssertEqual(secondHistory.cloudItems.first?.text, "from windows")
        apply(second)

        second.runCycleForTesting()

        XCTAssertEqual(postCount.withValue { $0 }, 1)
    }

    func testWindowsHexIDAndOffsetDateAreParsed() {
        let uuid = HistorySync.normalizedUUID("0123456789abcdef0123456789abcdef")
        XCTAssertEqual(uuid.uuidString.lowercased(), "01234567-89ab-cdef-0123-456789abcdef")
        let date = HistorySync.parseDate("2026-09-03T12:34:56+09:00")
        XCTAssertEqual(date.timeIntervalSince1970, 1_788_406_496, accuracy: 0.1)
    }

    func testAllItemsMergesByDateAndExcludesCloudMacAndDuplicateIDs() {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let history = HistoryStore(directory: dir)
        history.add("local")
        let local = history.items[0]
        let newer = makeItem(text: "windows", date: local.date.addingTimeInterval(10), device: "windows")
        let duplicate = makeItem(id: local.id, text: "duplicate", date: local.date.addingTimeInterval(20), device: "windows")
        let cloudMac = makeItem(text: "cloud mac", date: local.date.addingTimeInterval(30), device: "mac")

        history.setCloudItems([cloudMac, duplicate, newer])

        XCTAssertEqual(history.allItems.map(\.text), ["windows", "local"])
    }

    func testLegacyHistoryJSONWithoutDeviceRemainsCompatible() throws {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let json = #"[{"id":"11111111-1111-1111-1111-111111111111","text":"legacy","date":"2026-07-01T00:00:00Z"}]"#
        try Data(json.utf8).write(to: dir.appendingPathComponent("history.json"))

        let history = HistoryStore(directory: dir)

        XCTAssertNil(history.items[0].device)
    }
}
