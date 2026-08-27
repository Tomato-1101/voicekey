/// 文字起こしのローカル保存（議事録）
///
/// ライブ字幕が確定させたテキストを、その場で Markdown ファイルへ追記する。
/// **保存するのは文字起こしだけ**（訳文は保存しない・2026-08-28 ユーザー指示）。
///
/// 追記型にしている理由: 会議は数十分〜数時間続き、途中でアプリが落ちたり Mac が
/// スリープしたりする。メモリに貯めて終了時に一括保存する作りだと、その 1 回で全部消える。
import Foundation
import OSLog

/// 確定文をファイルへ追記する記録係
///
/// 認識コールバック（音声スレッド）から呼ばれるので、書き込みは専用の直列キューで行う。
final class TranscriptRecorder {

    private let logger = makeCaptionLogger("TranscriptRecorder")

    /// 書き込みを直列化するキュー（ファイル追記は I/O なのでメインから外す）
    private let queue = DispatchQueue(label: "com.voicekey.transcript", qos: .utility)

    /// この間隔以上あいたら「別の会議」とみなして新しいファイルにする
    ///
    /// 5 分にしているのは、会議の合間の休憩（1〜2 分）では分けたくない一方、
    /// 「昼の打ち合わせ」と「夕方の打ち合わせ」が 1 ファイルに混ざると議事録として使えないため。
    private static let sessionGap: TimeInterval = 5 * 60

    /// 現在書き込み中のファイル（nil なら次の追記で新規作成）
    private var currentURL: URL?
    /// 最後に追記した時刻（セッション区切りの判定に使う）
    private var lastWriteAt: Date?
    /// 直前に書いた本文（同じ確定文が二重に届いたときに弾く）
    private var lastText: String?

    /// 保存先ディレクトリ（~/Documents/voicekey/transcripts）
    static var directory: URL {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return documents.appendingPathComponent("voicekey/transcripts", isDirectory: true)
    }

    /// このインスタンスの保存先（テストから差し替えられるようにしてある）
    private let directory: URL

    /// - Parameter directory: 保存先（既定は ~/Documents/voicekey/transcripts）
    init(directory: URL = TranscriptRecorder.directory) {
        self.directory = directory
    }

    /// 現在のファイル（まだ 1 行も書いていなければ nil）
    var currentFileURL: URL? { queue.sync { currentURL } }

    /// 確定文を 1 件記録する
    ///
    /// - Parameters:
    ///   - text: 認識された確定テキスト
    ///   - speaker: 話者名（Google Meet ボット経由のときだけ入る。マイク/システム音声では nil）
    ///   - context: 記録の見出しに残す文脈（対象アプリ名や会議名）
    ///   - language: 認識言語の表示名（ヘッダ用）
    ///   - now: 記録時刻（既定は現在。テストとボット側の再生時刻の指定に使う）
    func record(text: String, speaker: String? = nil, context: String?, language: String, now: Date = Date()) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        queue.async { [weak self] in
            guard let self else { return }
            // 同じ確定文の重複（認識器が同一テキストを再送する / ボットの DOM 再読込）を弾く
            if self.lastText == trimmed, let last = self.lastWriteAt, now.timeIntervalSince(last) < 10 {
                return
            }
            let url = self.fileURL(at: now, context: context, language: language)
            let line = Self.formatLine(text: trimmed, speaker: speaker, at: now)
            self.append(line, to: url)
            self.lastWriteAt = now
            self.lastText = trimmed
        }
    }

    /// 記録を区切る（字幕を止めたときなど。次の追記は新しいファイルになる）
    func endSession() {
        queue.async { [weak self] in
            guard let self, let url = self.currentURL else { return }
            self.logger.notice("議事録を閉じました: \(url.lastPathComponent, privacy: .public)")
            self.currentURL = nil
            self.lastWriteAt = nil
            self.lastText = nil
        }
    }

    // MARK: - 内部処理

    /// 追記先のファイルを決める（間隔が空いていれば新規作成してヘッダを書く）
    private func fileURL(at now: Date, context: String?, language: String) -> URL {
        if let url = currentURL, let last = lastWriteAt, now.timeIntervalSince(last) < Self.sessionGap {
            return url
        }
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)

        let stamp = Self.fileStampFormatter.string(from: now)
        let url = directory.appendingPathComponent("\(stamp).md")
        currentURL = url

        if !FileManager.default.fileExists(atPath: url.path) {
            let header = """
                # 議事録 \(Self.headerFormatter.string(from: now))

                - 記録元: \(context ?? "システム音声")
                - 言語: \(language)

                """
            try? header.write(to: url, atomically: true, encoding: .utf8)
            logger.notice("議事録を開始しました: \(url.lastPathComponent, privacy: .public)")
        }
        return url
    }

    /// 1 行分の Markdown を作る
    private static func formatLine(text: String, speaker: String?, at date: Date) -> String {
        let time = timeFormatter.string(from: date)
        if let speaker, !speaker.isEmpty {
            return "- `\(time)` **\(speaker)**: \(text)\n"
        }
        return "- `\(time)` \(text)\n"
    }

    /// ファイル末尾へ 1 行追記する
    private func append(_ line: String, to url: URL) {
        guard let data = line.data(using: .utf8) else { return }
        if let handle = try? FileHandle(forWritingTo: url) {
            defer { try? handle.close() }
            try? handle.seekToEnd()
            try? handle.write(contentsOf: data)
        } else {
            // ヘッダ作成に失敗していた場合の保険（ここで作れば以降の追記は通る）
            try? data.write(to: url, options: .atomic)
        }
    }

    /// ファイル名用（2026-08-28_1432）
    private static let fileStampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd_HHmm"
        return formatter
    }()

    /// 見出し用（2026-08-28 14:32）
    private static let headerFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        return formatter
    }()

    /// 行頭の時刻用（14:32:05）
    private static let timeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()
}
