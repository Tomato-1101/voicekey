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

    /// 読めなかったファイルを同じフォルダの `<名前>.corrupt-<日時>` へ移す。
    /// - Returns: 移動先（移せなかったときは nil。その場合も呼び出し側は空で開始してよい）
    @discardableResult
    static func quarantine(_ url: URL, now: Date = Date()) -> URL? {
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
            try FileManager.default.moveItem(at: url, to: destination)
            ActionLog.shared.write("store", "読めないファイルを退避: \(destination.lastPathComponent)")
            return destination
        } catch {
            ActionLog.shared.write("store", "読めないファイルを退避できませんでした: \(url.lastPathComponent)")
            return nil
        }
    }
}
