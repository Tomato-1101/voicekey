//
//  CorruptFileQuarantine.swift
//  読めなくなった JSON ストアを別名へ退避する
//
//  各ストアは「読めなければ空で開始」する。そのまま次の保存が走ると、壊れた（＝まだ救える
//  かもしれない）元ファイルを空の内容で上書きして消してしまう。読めなかったファイルは
//  `<元の名前>.corrupt-<日時>` へ移してから新規作成させ、手で救える余地を残す。
//

import Foundation

enum CorruptFileQuarantine {

    /// ファイルの移動（テストで「移せなかった」を差し込むために差し替えられるようにしてある）
    nonisolated(unsafe) static var moveItem: (URL, URL) throws -> Void = {
        try FileManager.default.moveItem(at: $0, to: $1)
    }

    /// 読めなかったファイルを同じフォルダの `<名前>.corrupt-<日時>` へ移す。
    ///
    /// 退避したことは本人が気づけるよう 1 回だけ通知する（黙って空で始めると「履歴が消えた」としか見えない）。
    /// - Parameter label: 通知に出すファイルの呼び名（例「履歴ファイル」）
    /// - Returns: 移動先（移せなかったときは nil。その場合、呼び出し側は**保存しない**こと。
    ///   空で始めたまま保存すると、残した元ファイルを空の内容で上書きして消してしまう）
    @discardableResult
    static func quarantine(_ url: URL, label: String = "ファイル", now: Date = Date()) -> URL? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        let base = url.lastPathComponent + ".corrupt-" + formatter.string(from: now)
        let dir = url.deletingLastPathComponent()
        // 同じ秒に 2 回壊れても前の退避を潰さない
        var destination = dir.appendingPathComponent(base)
        var suffix = 2
        while FileManager.default.fileExists(atPath: destination.path) {
            destination = dir.appendingPathComponent("\(base)-\(suffix)")
            suffix += 1
        }
        do {
            try moveItem(url, destination)
            ActionLog.shared.write("store", "読めないファイルを退避: \(destination.lastPathComponent)")
            notify("\(label)が壊れていたので退避しました: \(abbreviated(destination))")
            return destination
        } catch {
            ActionLog.shared.write("store", "読めないファイルを退避できませんでした: \(url.lastPathComponent)")
            notify("\(label)が壊れていて退避できませんでした。上書きせずに残しています: \(abbreviated(url))")
            return nil
        }
    }

    // MARK: - 通知

    private static let lock = NSLock()
    nonisolated(unsafe) private static var handler: ((String) -> Void)?
    nonisolated(unsafe) private static var pending: [String] = []

    /// 通知の受け口を設定する。ストアはアプリの画面より先に作られるので、それまでの通知は溜めておいてここで渡す
    static func setNoticeHandler(_ newHandler: @escaping (String) -> Void) {
        lock.lock()
        handler = newHandler
        let queued = pending
        pending = []
        lock.unlock()
        queued.forEach(newHandler)
    }

    private static func notify(_ message: String) {
        lock.lock()
        guard let handler else {
            pending.append(message)
            lock.unlock()
            return
        }
        lock.unlock()
        handler(message)
    }

    private static func abbreviated(_ url: URL) -> String {
        (url.path as NSString).abbreviatingWithTildeInPath
    }
}
