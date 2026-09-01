//
//  ActionLog.swift
//  行動ログ（ユーザー操作と内部状態遷移をファイルに常時記録する）
//
//  os.log とは別に、平文のファイルへ「何が起きたか」を時系列で残す。
//  目的は障害時に**ログだけで経路を特定できる**ようにすること。
//  2026-09-02 の「変換中で固まる」障害では、録音開始要求を出したのに完了しなかった
//  （AVAudioEngine の inputFormat が HAL キューへの dispatch_sync で無期限ブロックし、
//  audio-control 直列キューごと詰まった）ことをアプリのログからは示せなかった。
//  「要求」と「完了」を対で残しておけば、完了行の欠落だけで原因箇所が分かる。
//
//  設計の要点:
//  - `write` は即 return する。整形もファイル I/O も専用の直列キュー（qos: .utility）で行う。
//    ディクテーションのクリティカルパスに 1ms も足さないための恒久要件。
//  - 1 行ずつ書き切る（バッファに溜めない）。ハングやクラッシュで落ちても直前まで残る。
//  - 日付でファイルを分け、古いものは自動削除する（保持 14 日）。
//

import Foundation

/// 行動ログの記録先。`ActionLog.shared.write(category, message)` で使う
final class ActionLog {

    /// アプリ全体で共有するインスタンス
    static let shared = ActionLog()

    /// ログを保持する日数（これより古い日付のファイルは起動時と日付切替時に削除する）
    static let retentionDays = 14

    /// 1 ファイルの上限バイト数（超えたら同日でも連番ファイルへ切り替える）
    private static let defaultMaxBytes = 20 * 1024 * 1024

    /// ファイル名の日付部分（`voicekey-YYYY-MM-DD.log`）
    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    /// 行頭の時刻（日付はファイル名にあるので時刻だけ）
    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()

    /// 書き込みを直列化するキュー。呼び出し側は常に非同期で投げるだけ
    private let queue = DispatchQueue(label: "com.voicekey.actionlog", qos: .utility)
    private let directory: URL
    private let maxBytes: Int

    // 以下はすべて queue 上でのみ触る
    private var handle: FileHandle?
    private var currentDay = ""
    private var currentIndex = 1
    private var currentBytes = 0

    /// - Parameters:
    ///   - directory: 保存先ディレクトリ（既定は `~/Library/Logs/voicekey`）
    ///   - maxBytes: 1 ファイルの上限バイト数（テストから小さい値を入れて連番ローテートを検証する）
    init(directory: URL = ActionLog.defaultDirectory, maxBytes: Int = ActionLog.defaultMaxBytes) {
        self.directory = directory
        self.maxBytes = maxBytes
        // 起動時の掃除もキュー上で行う（呼び出し側＝アプリ起動を待たせない）
        queue.async { [self] in purgeOldFiles(today: Date()) }
    }

    /// 既定の保存先（`~/Library/Logs/voicekey`）
    static var defaultDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/voicekey", isDirectory: true)
    }

    /// 行動を 1 行記録する（**即座に返る**。ファイル I/O は専用キューで行う）
    ///
    /// - Parameters:
    ///   - category: 分類（既存の os.log のカテゴリ名に合わせる。例 `audio` / `hotkey` / `app`）
    ///   - message: 本文。**文字起こしの本文など個人情報は入れない**（文字数などに留める）
    func write(_ category: String, _ message: String) {
        let now = Date()
        queue.async { [self] in
            append(line: "\(Self.timeFormatter.string(from: now)) [\(category)] \(message)\n", at: now)
        }
    }

    /// 溜まっている書き込みが終わるまで待つ。
    ///
    /// 呼んでよいのは**アプリ終了時とテスト**だけ（終了行を落とさないため）。
    /// 録音・貼り付けなどの通常経路からは絶対に呼ばない（ここだけは呼び出し側を待たせる）。
    func flush() {
        queue.sync {}
    }

    // MARK: - 削除対象の判定（純関数・テスト対象）

    /// 保持期間を過ぎたログファイル名を選ぶ。
    ///
    /// 実ファイルには触らず、**ファイル名の日付だけ**で判定する純関数にしてある
    /// （日付をまたぐ削除の挙動をテストで固定できるようにするため）。
    ///
    /// - Parameters:
    ///   - names: ディレクトリ内のファイル名一覧
    ///   - today: 基準日（通常は現在時刻）
    ///   - retentionDays: 保持日数。これより古い日付のファイルを削除対象にする
    /// - Returns: 削除してよいファイル名（`voicekey-*.log` 以外は対象にしない）
    static func filesToDelete(
        names: [String], today: Date, retentionDays: Int = ActionLog.retentionDays
    ) -> [String] {
        let calendar = Calendar.current
        let todayStart = calendar.startOfDay(for: today)
        return names.filter { name in
            guard let day = fileDay(from: name) else { return false }
            guard let diff = calendar.dateComponents(
                [.day], from: calendar.startOfDay(for: day), to: todayStart
            ).day else { return false }
            return diff > retentionDays
        }
    }

    /// ログファイル名から日付を取り出す（自分のログでなければ nil）
    ///
    /// 受け付ける形式は `voicekey-YYYY-MM-DD.log` と連番付きの `voicekey-YYYY-MM-DD.2.log`。
    private static func fileDay(from name: String) -> Date? {
        let prefix = "voicekey-"
        guard name.hasPrefix(prefix), name.hasSuffix(".log") else { return nil }
        let rest = name.dropFirst(prefix.count)
        guard rest.count > 10 else { return nil }
        let dayText = String(rest.prefix(10))
        // 日付の直後は必ず区切りのドット（`.log` か `.2.log`）
        guard rest.dropFirst(10).hasPrefix(".") else { return nil }
        return dayFormatter.date(from: dayText)
    }

    // MARK: - ファイル操作（すべて queue 上）

    /// 1 行追記する。日付が変わっていればローテートし、上限を超えていれば連番へ送る
    private func append(line: String, at now: Date) {
        let day = Self.dayFormatter.string(from: now)
        if day != currentDay {
            closeFile()
            currentDay = day
            currentIndex = 1
            openFile()
            // 日付が変わったこの瞬間に古いログを掃除する（起動しっぱなしでも溜まらない）
            purgeOldFiles(today: now)
        }
        guard let data = line.data(using: .utf8) else { return }
        if handle != nil, currentBytes > 0, currentBytes + data.count > maxBytes {
            closeFile()
            currentIndex += 1
            openFile()
        }
        guard let handle else { return }
        // 1 行ずつ書き切る（落ちても直前まで残す）。失敗しても本体は止めない
        do {
            try handle.write(contentsOf: data)
            currentBytes += data.count
        } catch {
            closeFile()
        }
    }

    /// 現在の日付・連番のファイルを開く（無ければ作る）
    private func openFile() {
        let fm = FileManager.default
        if !fm.fileExists(atPath: directory.path) {
            try? fm.createDirectory(at: directory, withIntermediateDirectories: true)
        }
        // 既存ファイルが既に上限を超えていたら次の連番へ送る（再起動直後の巨大ファイル対策）
        while currentIndex < 1000 {
            let url = fileURL(day: currentDay, index: currentIndex)
            let attributes = try? fm.attributesOfItem(atPath: url.path)
            let existing = (attributes?[.size] as? Int) ?? 0
            if existing >= maxBytes {
                currentIndex += 1
                continue
            }
            if !fm.fileExists(atPath: url.path) {
                fm.createFile(atPath: url.path, contents: nil)
            }
            handle = try? FileHandle(forWritingTo: url)
            _ = try? handle?.seekToEnd()
            currentBytes = existing
            return
        }
    }

    private func closeFile() {
        try? handle?.close()
        handle = nil
        currentBytes = 0
    }

    /// 日付と連番からファイル URL を作る（連番 1 は付けない）
    private func fileURL(day: String, index: Int) -> URL {
        let name = index <= 1 ? "voicekey-\(day).log" : "voicekey-\(day).\(index).log"
        return directory.appendingPathComponent(name)
    }

    /// 保持期間を過ぎたログを削除する
    private func purgeOldFiles(today: Date) {
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(atPath: directory.path) else { return }
        for name in Self.filesToDelete(names: names, today: today) {
            try? fm.removeItem(at: directory.appendingPathComponent(name))
        }
    }
}
