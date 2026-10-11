/// 文字起こしのローカル保存（議事録）
///
/// ライブ字幕が確定させたテキストを、その場で Markdown ファイルへ追記する。
/// **保存するのは文字起こしだけ**（訳文は保存しない・2026-08-28 ユーザー指示）。
///
/// 追記型にしている理由: 会議は数十分〜数時間続き、途中でアプリが落ちたり Mac が
/// スリープしたりする。メモリに貯めて終了時に一括保存する作りだと、その 1 回で全部消える。
///
/// ファイルの区切り方は 2 通り:
/// - ライブ字幕（手動）: 5 分以上あいたら別の会議とみなして新しいファイルにする。
/// - Meet ボット: `beginMeeting` から `endSession` まで（参加から退出まで）を必ず 1 ファイルにする。
///   会議中に 5 分黙っていても分けない。ファイル名に会議コードを入れて、どの会議か分かるようにする。
import Foundation
import OSLog

/// 確定文をファイルへ追記する記録係
///
/// 認識コールバック（音声スレッド）から呼ばれるので、書き込みは専用の直列キューで行う。
final class TranscriptRecorder {

    /// 書き込み 1 回の結果（呼び出し側が「記録中 n 行」「保存に失敗」を正しく出すため）
    enum WriteOutcome: Equatable {
        /// 書けた（ファイルと、そのファイルに書いた行数）
        case written(URL, lines: Int)
        /// 書けなかった（理由）
        case failed(String)
    }

    /// 記録を区切ったときの集計（`endSession(onClosed:)` で渡す）
    ///
    /// 書き込みキューの上で作るので、それまでに頼んだ書き込みが全部終わった後の値になる。
    struct SessionSummary: Equatable {
        /// 書いていたファイル（作れなかったときは nil）
        let url: URL?
        /// そのファイルに書いた行数
        let lines: Int
        /// 最後の書き込みが失敗していれば、その理由
        let error: String?
    }

    private let logger = makeCaptionLogger("TranscriptRecorder")

    /// 書き込みを直列化するキュー（ファイル追記は I/O なのでメインから外す）
    private let queue = DispatchQueue(label: "com.voicekey.transcript", qos: .utility)

    /// この間隔以上あいたら「別の会議」とみなして新しいファイルにする（ライブ字幕のときだけ）
    ///
    /// 5 分にしているのは、会議の合間の休憩（1〜2 分）では分けたくない一方、
    /// 「昼の打ち合わせ」と「夕方の打ち合わせ」が 1 ファイルに混ざると議事録として使えないため。
    private static let sessionGap: TimeInterval = 5 * 60

    /// 書き込みのたびに呼ばれる（**記録用の直列キューの上で**呼ぶので、UI へはメインへ戻して使う）
    ///
    /// 以前は失敗を `try?` で握り潰していたため、保存先に書けなくても画面は「記録中」のままだった。
    /// 2 つ目の値は `beginMeeting(tag:)` で渡した会議の番号（会議でなければ 0）。退出直後に
    /// 再参加したとき、前の会議の書き込み結果を新しい会議の行数・ファイル名と取り違えないために使う。
    var onWrite: ((WriteOutcome, Int) -> Void)?

    /// 現在書き込み中のファイル（nil なら次の追記で新規作成）
    private var currentURL: URL?
    /// 最後に追記した時刻（セッション区切りの判定に使う）
    private var lastWriteAt: Date?
    /// 直前に書いた本文（同じ確定文が二重に届いたときに弾く）
    private var lastText: String?
    /// 直前に書いた行の話者（別の人が同じ言葉を言ったときは弾かない）
    private var lastSpeaker: String?
    /// 現在のファイルに書いた行数
    private var linesInFile = 0
    /// 直近の書き込み失敗（成功したら消える）
    private var lastError: String?
    /// 会議（Meet ボット）として記録中なら、その情報
    ///
    /// 入っている間は 5 分ルールで分けず、`endSession` まで同じファイルに書く。
    private var meeting: (code: String?, context: String, language: String, tag: Int)?

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

    /// 現在のファイルに書いた行数
    var lineCount: Int { queue.sync { linesInFile } }

    /// 直近の書き込み失敗の理由（成功していれば nil）
    var lastErrorMessage: String? { queue.sync { lastError } }

    /// 会議（Meet ボット）の記録を始める
    ///
    /// その場でヘッダ付きのファイルを作る（「最新の議事録を開く」ですぐ開けるように）。
    /// 以後は `endSession` まで、間隔が何分あいても同じファイルに追記する。
    ///
    /// - Parameters:
    ///   - code: 会議コード（`abc-defg-hij`。ファイル名に入れる）
    ///   - context: 見出しに残す記録元（「Google Meet abc-defg-hij」など）
    ///   - language: 認識言語の表示名
    ///   - now: 開始時刻（ファイル名・見出しに使う）
    ///   - tag: 会議の番号（`onWrite` の 2 つ目の値として返す）
    func beginMeeting(code: String?, context: String, language: String, now: Date = Date(), tag: Int = 0) {
        queue.async { [weak self] in
            guard let self else { return }
            self.closeCurrent()
            self.meeting = (code, context, language, tag)
            // 前の記録の失敗を、この会議の結果として持ち越さない
            self.lastError = nil
            do {
                let url = try self.createFile(at: now, code: code, context: context, language: language)
                self.lastError = nil
                self.onWrite?(.written(url, lines: 0), tag)
            } catch {
                self.reportFailure(error)
            }
        }
    }

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
            // 同じ確定文の重複（認識器が同一テキストを再送する / ボットの DOM 再読込）を弾く。
            // 話者も比べる（別の人が続けて「はい」と言ったのは別の発言なので残す）。
            if self.lastText == trimmed, self.lastSpeaker == speaker,
               let last = self.lastWriteAt, now.timeIntervalSince(last) < 10 {
                return
            }
            do {
                let url = try self.fileURL(at: now, context: context, language: language)
                let line = Self.formatLine(text: trimmed, speaker: speaker, at: now)
                try self.append(line, to: url)
                self.lastWriteAt = now
                self.lastText = trimmed
                self.lastSpeaker = speaker
                self.linesInFile += 1
                self.lastError = nil
                self.onWrite?(.written(url, lines: self.linesInFile), self.meeting?.tag ?? 0)
            } catch {
                self.reportFailure(error)
            }
        }
    }

    /// 記録を区切る（字幕を止めた・会議を出たときなど。次の追記は新しいファイルになる）
    ///
    /// - Parameter onClosed: 区切った時点の集計を受け取る（**記録用の直列キューの上で**呼ぶ）。
    ///   それまでに頼んだ書き込みが全部終わった後に呼ぶので、終了通知の行数が書きかけで少なくならない。
    func endSession(onClosed: ((SessionSummary) -> Void)? = nil) {
        queue.async { [weak self] in
            guard let self else { return }
            let summary = SessionSummary(url: self.currentURL, lines: self.linesInFile, error: self.lastError)
            self.closeCurrent()
            self.meeting = nil
            onClosed?(summary)
        }
    }

    /// ここまでに頼んだ書き込みが全部終わるまで待つ
    ///
    /// アプリ終了の直前に呼ぶ。キューに残った追記はプロセスと一緒に消えるため。
    func waitUntilWritten() {
        queue.sync {}
    }

    /// 保存先にある一番新しい議事録（無ければ nil）
    ///
    /// - Parameter directory: 探す場所（既定は ~/Documents/voicekey/transcripts）
    static func latestFile(in directory: URL = TranscriptRecorder.directory) -> URL? {
        let keys: Set<URLResourceKey> = [.contentModificationDateKey]
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: Array(keys), options: [.skipsHiddenFiles]
        ) else { return nil }
        let modified = { (url: URL) -> Date in
            (try? url.resourceValues(forKeys: keys).contentModificationDate) ?? .distantPast
        }
        return files.filter { $0.pathExtension == "md" }.max { modified($0) < modified($1) }
    }

    /// ファイル名の本体（`2026-10-11_140512` / 会議なら `2026-10-11_140512-abc-defg-hij`）
    static func fileBaseName(at date: Date, code: String?) -> String {
        let stamp = fileStampFormatter.string(from: date)
        // 会議コードは URL から来るので、ファイル名に使えない文字を落として長さも抑える
        var safe = ""
        safe.unicodeScalars.append(
            contentsOf: (code ?? "").unicodeScalars.filter {
                $0.isASCII && (CharacterSet.alphanumerics.contains($0) || $0 == "-")
            }
        )
        safe = String(safe.prefix(40))
        return safe.isEmpty ? stamp : "\(stamp)-\(safe)"
    }

    // MARK: - 内部処理

    /// 今のファイルを閉じる（次の追記は新しいファイルになる）
    private func closeCurrent() {
        if let url = currentURL {
            logger.notice("議事録を閉じました: \(url.lastPathComponent, privacy: .public)")
        }
        currentURL = nil
        lastWriteAt = nil
        lastText = nil
        lastSpeaker = nil
        linesInFile = 0
    }

    /// 失敗を記録して呼び出し側へ知らせる
    private func reportFailure(_ error: Error) {
        let message = error.localizedDescription
        lastError = message
        logger.error("議事録を保存できません: \(message, privacy: .public)")
        onWrite?(.failed(message), meeting?.tag ?? 0)
    }

    /// 追記先のファイルを決める（必要なら新規作成してヘッダを書く）
    ///
    /// 会議の記録中は間隔に関係なく同じファイル。ライブ字幕は 5 分あいたら新しいファイル。
    private func fileURL(at now: Date, context: String?, language: String) throws -> URL {
        if let meeting {
            if let url = currentURL { return url }
            // 開始時のファイル作成に失敗していたときだけここへ来る（書けるようになったら作る）
            return try createFile(at: now, code: meeting.code, context: meeting.context, language: meeting.language)
        }
        if let url = currentURL, let last = lastWriteAt, now.timeIntervalSince(last) < Self.sessionGap {
            return url
        }
        closeCurrent()
        return try createFile(at: now, code: nil, context: context, language: language)
    }

    /// 新しいファイルを作ってヘッダを書く
    ///
    /// 名前は秒まで入れ、それでも同名があれば `-2` `-3` と付ける。以前は分単位の名前で、
    /// 同じ分に始まった別の記録が既存ファイルの続きに混ざっていた。
    private func createFile(at now: Date, code: String?, context: String?, language: String) throws -> URL {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let header = Data("""
            # 議事録 \(Self.headerFormatter.string(from: now))

            - 記録元: \(context ?? "システム音声")
            - 言語: \(language)

            """.utf8)
        let baseName = Self.fileBaseName(at: now, code: code)
        // ライブ字幕とボットは別々の記録係（別キュー）なので、「無ければ作る」を 1 回の操作で行う
        // （存在確認と書き込みを分けると、同じ秒に始まった 2 つが同じファイルを掴む）
        for attempt in 1...100 {
            let name = attempt == 1 ? baseName : "\(baseName)-\(attempt)"
            let url = directory.appendingPathComponent("\(name).md")
            do {
                try header.write(to: url, options: .withoutOverwriting)
            } catch where FileManager.default.fileExists(atPath: url.path) {
                continue
            }
            currentURL = url
            linesInFile = 0
            logger.notice("議事録を開始しました: \(url.lastPathComponent, privacy: .public)")
            return url
        }
        throw CocoaError(.fileWriteFileExists)
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
    ///
    /// 記録中にファイルが消された（Finder で捨てた等）ときは作り直して書く。
    private func append(_ line: String, to url: URL) throws {
        let data = Data(line.utf8)
        guard FileManager.default.fileExists(atPath: url.path) else {
            try data.write(to: url, options: .atomic)
            return
        }
        let handle = try FileHandle(forWritingTo: url)
        defer { try? handle.close() }
        try handle.seekToEnd()
        try handle.write(contentsOf: data)
    }

    /// ファイル名用（2026-08-28_143205）
    private static let fileStampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd_HHmmss"
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
