/// 保存済みの議事録の一覧と中身の解析
///
/// 設定の「議事録・会議」で、過去の議事録を選んで読めるようにするための純粋なロジック。
/// ファイルの読み込みを含むので、`list` と `read` は SwiftUI の body から呼ばない（裏のスレッドで呼ぶ）。
/// 書き方（ファイル名・行の形）の正本は `TranscriptRecorder`。ここはそれを読み戻すだけ。
import Foundation

/// 議事録の 1 行（1 つの発言）
struct TranscriptLine: Equatable, Identifiable, Sendable {
    /// その議事録の中での通し番号（1 始まり）
    let id: Int
    /// 時刻（`14:32:05`）
    let time: String
    /// 話者名（取れなかった行は nil）
    let speaker: String?
    /// 発言
    let text: String
}

/// 一覧に出す議事録 1 件
struct TranscriptFileInfo: Equatable, Identifiable, Sendable {
    let url: URL
    /// 記録を始めた日時（ファイル名から読めなければ更新日時）
    let date: Date
    /// 会議コード（ライブ字幕の議事録など、会議でなければ nil）
    let meetingCode: String?
    /// 発言の行数
    let lineCount: Int
    /// 発言の文字数（長さの目安）
    let characterCount: Int
    /// 要約が保存済みか
    let hasSummary: Bool

    var id: URL { url }
}

/// 議事録フォルダの読み取り
enum TranscriptLibrary {

    /// 要約ファイルの末尾（`<議事録の名前>.summary.md` で議事録の隣に置く）
    static let summarySuffix = ".summary.md"

    /// 議事録の本体か（要約ファイルや他の種類のファイルは除く）
    static func isTranscript(_ url: URL) -> Bool {
        let name = url.lastPathComponent
        return name.hasSuffix(".md") && !name.hasSuffix(summarySuffix)
    }

    /// その議事録の要約の保存先
    static func summaryURL(for transcript: URL) -> URL {
        let stem = transcript.deletingPathExtension().lastPathComponent
        return transcript.deletingLastPathComponent().appendingPathComponent(stem + summarySuffix)
    }

    /// ファイル名（拡張子なし）から日時と会議コードを読む
    ///
    /// 形は `2026-10-11_140512` / `2026-10-11_140512-abc-defg-hij` / 同名回避の `…-2`。
    /// 以前の版は分までの `2026-08-28_1432` だったので、それも読む。
    static func parseFileName(_ stem: String) -> (date: Date?, code: String?) {
        let parts = stem.split(separator: "_", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2, parts[0].count == 10 else { return (nil, nil) }
        let tail = parts[1]
        let digits = tail.prefix { $0.isNumber }
        guard digits.count == 6 || digits.count == 4 else { return (nil, nil) }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = digits.count == 6 ? "yyyy-MM-dd_HHmmss" : "yyyy-MM-dd_HHmm"
        guard let date = formatter.date(from: "\(parts[0])_\(digits)") else { return (nil, nil) }

        var rest = String(tail.dropFirst(digits.count))
        guard rest.hasPrefix("-") else { return (date, nil) }
        rest.removeFirst()
        // 末尾の `-2` は同名回避の連番。会議コードは英字だけ（abc-defg-hij）なので、
        // 「数字だけ」か「会議コード＋数字」のときに限って落とす
        var pieces = rest.split(separator: "-").map(String.init)
        if let last = pieces.last, last.allSatisfy(\.isNumber),
           pieces.count == 1 || pieces.dropLast().allSatisfy({ $0.allSatisfy(\.isLetter) }) {
            pieces.removeLast()
        }
        let code = pieces.joined(separator: "-")
        return (date, code.isEmpty ? nil : code)
    }

    /// 議事録の本文から発言の行を取り出す
    ///
    /// 行の形は ``- `14:32:05` **山田**: 本文`` か、話者なしの ``- `14:32:05` 本文``。
    /// 見出しやヘッダ（`- 記録元: …`）は発言ではないので飛ばす。
    static func parseLines(_ content: String) -> [TranscriptLine] {
        var lines: [TranscriptLine] = []
        for raw in content.split(whereSeparator: \.isNewline) {
            guard raw.hasPrefix("- `") else { continue }
            let body = raw.dropFirst(3)
            guard let tick = body.firstIndex(of: "`") else { continue }
            let time = String(body[..<tick])
            var rest = body[body.index(after: tick)...].drop { $0 == " " }
            var speaker: String?
            if rest.hasPrefix("**"), let close = rest.range(of: "**: ") {
                speaker = String(rest[rest.index(rest.startIndex, offsetBy: 2)..<close.lowerBound])
                rest = rest[close.upperBound...]
            }
            let text = String(rest)
            guard !text.isEmpty else { continue }
            lines.append(TranscriptLine(id: lines.count + 1, time: time, speaker: speaker, text: text))
        }
        return lines
    }

    /// 保存済みの議事録を新しい順に並べる（**ファイルを読むので裏のスレッドで呼ぶ**）
    ///
    /// - Parameter directory: 探す場所（既定は ~/Documents/voicekey/transcripts）
    static func list(in directory: URL = TranscriptRecorder.directory) -> [TranscriptFileInfo] {
        let keys: Set<URLResourceKey> = [.contentModificationDateKey]
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: Array(keys), options: [.skipsHiddenFiles]
        ) else { return [] }
        let names = Set(files.map(\.lastPathComponent))
        let infos = files.filter(isTranscript).map { url -> TranscriptFileInfo in
            let parsed = parseFileName(url.deletingPathExtension().lastPathComponent)
            let modified = (try? url.resourceValues(forKeys: keys).contentModificationDate) ?? .distantPast
            let lines = parseLines((try? String(contentsOf: url, encoding: .utf8)) ?? "")
            return TranscriptFileInfo(
                url: url, date: parsed.date ?? modified, meetingCode: parsed.code,
                lineCount: lines.count, characterCount: lines.reduce(0) { $0 + $1.text.count },
                hasSummary: names.contains(summaryURL(for: url).lastPathComponent)
            )
        }
        // 同じ秒に始まった記録も毎回同じ順に並ぶよう、名前でも比べる
        return infos.sorted {
            $0.date != $1.date ? $0.date > $1.date : $0.url.lastPathComponent > $1.url.lastPathComponent
        }
    }

    /// 一覧の 1 行目（`11/11 14:05`）
    static func title(for info: TranscriptFileInfo) -> String {
        titleFormatter.string(from: info.date)
    }

    /// 一覧の 2 行目（会議コード・行数・長さ）
    static func detail(for info: TranscriptFileInfo) -> String {
        var parts: [String] = []
        if let code = info.meetingCode { parts.append(code) }
        parts.append("\(info.lineCount) 行")
        parts.append("\(info.characterCount.formatted()) 字")
        return parts.joined(separator: "・")
    }

    private static let titleFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ja_JP")
        formatter.dateFormat = "yyyy/MM/dd HH:mm"
        return formatter
    }()
}
