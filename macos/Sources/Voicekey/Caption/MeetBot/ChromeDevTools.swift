/// Chrome DevTools Protocol（CDP）の最小クライアント
///
/// Google Meet へ入るボットのために、**専用プロファイルの Chrome を 1 つ起動して外から操作する**。
/// Playwright や Node をアプリに同梱すると Chromium ごと数百 MB 抱えることになるため、
/// **すでに入っている Chrome を DevTools プロトコルで動かす**方式にした（追加依存ゼロ）。
///
/// 使うのは 3 つだけ:
/// - HTTP `/json/version`（起動待ち）・`/json/new`（タブを開く）・`/json/list`（タブを探す）
/// - WebSocket の `Runtime.evaluate`（ページ内で JS を実行）
/// - WebSocket の `Page.navigate`（URL を開き直す）
import Foundation
import OSLog

/// CDP のエラー
enum ChromeDevToolsError: Error, CustomStringConvertible {
    /// Chrome の実行ファイルが見つからない
    case chromeNotFound
    /// DevTools が期限内に応答しなかった
    case devToolsUnavailable
    /// ページのターゲットが見つからない
    case targetNotFound
    /// CDP がエラーを返した
    case protocolError(String)
    /// 応答の形が想定と違う
    case malformedResponse

    var description: String {
        switch self {
        case .chromeNotFound: return "Google Chrome が見つかりません"
        case .devToolsUnavailable: return "Chrome の DevTools に接続できませんでした"
        case .targetNotFound: return "操作対象のタブが見つかりません"
        case let .protocolError(message): return "Chrome の操作に失敗しました: \(message)"
        case .malformedResponse: return "Chrome から想定外の応答が返りました"
        }
    }
}

/// 1 つのページ（タブ）に繋いで JS を流すクライアント
///
/// 送受信は `URLSessionWebSocketTask`。CDP は「id を振ったコマンド」と「同じ id の応答」の
/// 往復なので、id ごとの continuation を辞書で持って async/await に変換している。
final class ChromeDevTools: @unchecked Sendable {

    private let logger = makeCaptionLogger("ChromeDevTools")

    /// Chrome の実行ファイル（標準の場所）
    static let chromeExecutable =
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    /// Chrome がインストールされているか
    static var isChromeInstalled: Bool {
        FileManager.default.isExecutableFile(atPath: chromeExecutable)
    }

    private let session = URLSession(configuration: .ephemeral)
    private var socket: URLSessionWebSocketTask?

    /// コマンド id の採番と応答待ちの管理（受信は別スレッドから来るので lock で守る）
    private let lock = NSLock()
    private var nextID = 1
    private var pending: [Int: CheckedContinuation<[String: Any], Error>] = [:]

    // MARK: - Chrome の起動

    /// 専用プロファイルの Chrome を起動する
    ///
    /// 普段使いの Chrome とは **別の `--user-data-dir`** を使う。理由は 2 つ:
    /// 1. 普段の Chrome を巻き込んで再起動させない（作業中のタブを閉じさせない）
    /// 2. ボットのログイン状態を分けて持てる（会議には別アカウント / ゲストで入れる）
    ///
    /// - Parameters:
    ///   - port: DevTools のポート
    ///   - profileDirectory: プロファイルの置き場所
    ///   - headless: 画面に出さずに動かすか
    /// - Returns: 起動した Chrome のプロセス
    static func launchChrome(port: Int, profileDirectory: URL, headless: Bool) throws -> Process {
        guard isChromeInstalled else { throw ChromeDevToolsError.chromeNotFound }
        try? FileManager.default.createDirectory(at: profileDirectory, withIntermediateDirectories: true)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: chromeExecutable)
        var arguments = [
            "--remote-debugging-port=\(port)",
            "--user-data-dir=\(profileDirectory.path)",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            // 会議のカメラ・マイクの許可ダイアログで止まらないようにする。
            // ボットは黙って聞くだけなので、入力デバイスは偽物で十分（実マイクも使わない）。
            "--use-fake-ui-for-media-capture",
            "--use-fake-device-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
        ]
        if headless {
            // 画面に出さない。Meet は headless=new なら通常どおり動く。
            arguments.append("--headless=new")
        }
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        return process
    }

    /// DevTools が応答するまで待つ
    ///
    /// - Parameters:
    ///   - port: DevTools のポート
    ///   - timeout: 待つ秒数
    static func waitForDevTools(port: Int, timeout: TimeInterval = 20) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let _ = try? await httpJSON(path: "/json/version", port: port) { return }
            try? await Task.sleep(for: .milliseconds(300))
        }
        throw ChromeDevToolsError.devToolsUnavailable
    }

    // MARK: - タブ操作（HTTP）

    /// 新しいタブで URL を開き、そのタブの WebSocket URL を返す
    ///
    /// `/json/new` は今の Chrome では **PUT** でないと 405 を返す。
    static func openTab(url: String, port: Int) async throws -> URL {
        let encoded = url.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? url
        let json = try await httpJSON(path: "/json/new?\(encoded)", port: port, method: "PUT")
        guard let dictionary = json as? [String: Any],
              let socket = dictionary["webSocketDebuggerUrl"] as? String,
              let socketURL = URL(string: socket) else {
            throw ChromeDevToolsError.malformedResponse
        }
        return socketURL
    }

    /// DevTools の HTTP エンドポイントを叩く
    private static func httpJSON(path: String, port: Int, method: String = "GET") async throws -> Any {
        guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else {
            throw ChromeDevToolsError.malformedResponse
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 5
        let (data, _) = try await URLSession.shared.data(for: request)
        return try JSONSerialization.jsonObject(with: data)
    }

    // MARK: - WebSocket

    /// ページに接続する
    func connect(to socketURL: URL) {
        let task = session.webSocketTask(with: socketURL)
        socket = task
        task.resume()
        receiveLoop()
    }

    /// 接続を閉じる（待ち中のコマンドはすべて失敗させる）
    func close() {
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        let waiting = lock.withLock { () -> [CheckedContinuation<[String: Any], Error>] in
            let values = Array(pending.values)
            pending.removeAll()
            return values
        }
        for continuation in waiting {
            continuation.resume(throwing: ChromeDevToolsError.devToolsUnavailable)
        }
    }

    /// CDP のコマンドを 1 つ送って結果を待つ
    ///
    /// - Parameters:
    ///   - method: `Runtime.evaluate` など
    ///   - params: パラメータ
    /// - Returns: `result` の中身
    @discardableResult
    func send(_ method: String, _ params: [String: Any] = [:]) async throws -> [String: Any] {
        guard let socket else { throw ChromeDevToolsError.devToolsUnavailable }
        let id = lock.withLock { () -> Int in
            let value = nextID
            nextID += 1
            return value
        }
        let payload: [String: Any] = ["id": id, "method": method, "params": params]
        let data = try JSONSerialization.data(withJSONObject: payload)
        guard let text = String(data: data, encoding: .utf8) else {
            throw ChromeDevToolsError.malformedResponse
        }

        return try await withCheckedThrowingContinuation { continuation in
            lock.withLock { pending[id] = continuation }
            socket.send(.string(text)) { [weak self] error in
                guard let error, let self else { return }
                // 送信に失敗したら待ち手を残さない（残すと await が永久に返らない）
                guard let waiting = self.lock.withLock({ self.pending.removeValue(forKey: id) }) else { return }
                waiting.resume(throwing: error)
            }
        }
    }

    /// ページ内で JS を実行して結果（JSON 化できる値）を返す
    ///
    /// `returnByValue` を付けるのは、オブジェクト参照ではなく**値そのもの**を持ち帰るため。
    /// `awaitPromise` は Meet の DOM 待ちを JS 側の Promise で書けるようにするため。
    ///
    /// - Parameter javaScript: 実行する式
    /// - Returns: 評価結果（nil のこともある）
    @discardableResult
    func evaluate(_ javaScript: String) async throws -> Any? {
        let result = try await send(
            "Runtime.evaluate",
            [
                "expression": javaScript,
                "returnByValue": true,
                "awaitPromise": true,
                "userGesture": true,
            ]
        )
        if let exception = result["exceptionDetails"] as? [String: Any] {
            let text = (exception["text"] as? String) ?? "JS の実行に失敗しました"
            throw ChromeDevToolsError.protocolError(text)
        }
        guard let value = result["result"] as? [String: Any] else {
            throw ChromeDevToolsError.malformedResponse
        }
        return value["value"]
    }

    /// URL を開く
    func navigate(to url: String) async throws {
        try await send("Page.navigate", ["url": url])
    }

    // MARK: - 内部処理

    /// 受信を回し続ける（1 件処理するたびに自分を呼び直す）
    private func receiveLoop() {
        socket?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case let .success(message):
                if case let .string(text) = message { self.handle(text) }
                self.receiveLoop()
            case let .failure(error):
                self.logger.notice("DevTools の接続が切れました: \(String(describing: error), privacy: .public)")
                let waiting = self.lock.withLock { () -> [CheckedContinuation<[String: Any], Error>] in
                    let values = Array(self.pending.values)
                    self.pending.removeAll()
                    return values
                }
                for continuation in waiting { continuation.resume(throwing: error) }
            }
        }
    }

    /// 応答 1 件を対応する待ち手へ渡す
    private func handle(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        // イベント（id 無し）は今は使っていないので捨てる
        guard let id = json["id"] as? Int else { return }
        guard let continuation = lock.withLock({ pending.removeValue(forKey: id) }) else { return }
        if let error = json["error"] as? [String: Any] {
            let message = (error["message"] as? String) ?? "unknown"
            continuation.resume(throwing: ChromeDevToolsError.protocolError(message))
            return
        }
        continuation.resume(returning: (json["result"] as? [String: Any]) ?? [:])
    }
}
