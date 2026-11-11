/// 設定の「議事録・会議」で、過去の議事録の一覧・本文・要約を持つ画面モデル
///
/// ファイルの読み書きと要約は裏で行い、結果だけをメインで受ける（SwiftUI の body からファイルを読まない）。
/// 一覧と本文の解析は `TranscriptLibrary`、要約の段取りは `TranscriptSummarizer` に任せる。
import AppKit
import Foundation

/// `--ui-snapshot --meeting-sample` で撮る見本の状態（撮影のときだけ使う）
enum MeetingSnapshotScenario: String, CaseIterable {
    /// 何も無い（未参加・議事録なし）
    case empty
    /// 記録中（発言が流れている）
    case recording
    /// 要約あり
    case summary
    /// 参加の失敗と要約の失敗
    case error
}

/// 過去の議事録の一覧・本文・要約
@MainActor
final class MeetingNotesModel: ObservableObject {

    /// 下の欄に出すもの
    enum Pane: Hashable {
        case transcript
        case summary
    }

    /// 下の 1 行に出す知らせ
    enum Notice: Equatable {
        /// 何も無い（使い方の一言を出す）
        case none
        /// 済んだことの知らせ（コピーした・保存した）
        case info(String)
        /// 要約の進み具合
        case progress(String)
        /// 失敗
        case error(String)
    }

    /// 新しい順の議事録
    @Published private(set) var files: [TranscriptFileInfo] = []
    /// 選んでいる議事録
    @Published private(set) var selected: URL?
    /// 選んでいる議事録の発言
    @Published private(set) var lines: [TranscriptLine] = []
    /// 選んでいる議事録の要約（まだ無ければ nil）
    @Published private(set) var summary: String?
    @Published private(set) var notice: Notice = .none
    /// 要約を作っている最中か
    @Published private(set) var isSummarizing = false
    @Published var pane: Pane = .transcript

    private let directory: URL
    /// 実ファイルを読むか（撮影用の見本では読まない）
    private let loadsFromDisk: Bool
    private let chat: TranscriptSummarizer.Chat
    /// 読み込みの世代（選び直した後に届いた古い結果を捨てる）
    private var loadToken = 0

    /// 要約に使う Groq への問い合わせ（整形と同じクライアントを使う）
    /// `chat` は接続（URLSession）を読むだけなので、どのスレッドから呼んでもよい
    nonisolated(unsafe) private static let formatter = TextFormatter()
    nonisolated static let groqChat: TranscriptSummarizer.Chat = { system, user in
        try await formatter.chat(
            system: system, user: user, model: LLMModelCatalog.recommendedGroq,
            purpose: .meetingSummary, maxCompletionTokens: TranscriptSummarizer.maxCompletionTokens
        )
    }

    /// - Parameters:
    ///   - directory: 議事録のフォルダ（既定は ~/Documents/voicekey/transcripts）
    ///   - chat: 要約の問い合わせ（テストでは通信しないものに差し替える）
    init(
        directory: URL = TranscriptRecorder.directory,
        chat: @escaping TranscriptSummarizer.Chat = MeetingNotesModel.groqChat
    ) {
        self.directory = directory
        self.chat = chat
        self.loadsFromDisk = true
    }

    // MARK: - 読み込み

    /// 一覧を読み直す（選んでいたものが残っていれば選択はそのまま）
    func reload() {
        guard loadsFromDisk else { return }
        let directory = directory
        Task {
            let list = await Task.detached(priority: .userInitiated) {
                TranscriptLibrary.list(in: directory)
            }.value
            self.files = list
            if let selected = self.selected, list.contains(where: { $0.url == selected }) { return }
            self.select(list.first?.url)
        }
    }

    /// 議事録を選ぶ（本文と保存済みの要約を読む）
    func select(_ url: URL?) {
        guard url != selected else { return }
        selected = url
        // 前の議事録についての知らせを持ち越さない（進行中の要約の表示は残す）
        if !isSummarizing { notice = .none }
        reloadSelected()
    }

    /// 選んでいる議事録の本文と要約を読み直す
    func reloadSelected() {
        loadToken += 1
        let token = loadToken
        guard let url = selected else {
            lines = []
            summary = nil
            return
        }
        guard loadsFromDisk else { return }
        Task {
            let loaded = await Task.detached(priority: .userInitiated) { () -> ([TranscriptLine], String?) in
                let content = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
                let summary = try? String(contentsOf: TranscriptLibrary.summaryURL(for: url), encoding: .utf8)
                return (TranscriptLibrary.parseLines(content), summary)
            }.value
            guard token == self.loadToken else { return }
            self.lines = loaded.0
            self.summary = loaded.1
        }
    }

    // MARK: - 操作

    /// 選んでいる議事録を要約する（保存済みの要約があっても作り直す）
    func summarize() {
        guard let url = selected, !isSummarizing else { return }
        isSummarizing = true
        notice = .progress("要約の準備中")
        pane = .summary
        let chat = chat
        Task {
            do {
                let content = try await Task.detached { try String(contentsOf: url, encoding: .utf8) }.value
                let text = try await TranscriptSummarizer.summarize(content, chat: chat, progress: { [weak self] step in
                    Task { @MainActor [weak self] in self?.show(step) }
                })
                let saved = await Task.detached {
                    (try? Data(text.utf8).write(to: TranscriptLibrary.summaryURL(for: url), options: .atomic)) != nil
                }.value
                self.isSummarizing = false
                self.notice = saved ? .info("要約を保存しました") : .error("要約はできましたが、保存できませんでした")
                // 要約している間に別の議事録を選んでいたら、表示は差し替えない（一覧の「要約あり」だけ更新する）
                if self.selected == url { self.summary = text }
                self.reload()
            } catch {
                self.isSummarizing = false
                self.notice = .error(TranscriptSummarizer.message(for: error))
            }
        }
    }

    /// 進み具合を出す（要約が終わった後に遅れて届いた分は出さない）
    private func show(_ step: TranscriptSummarizer.Progress) {
        guard isSummarizing else { return }
        notice = .progress(step.message)
    }

    /// いま表示している内容（本文か要約）をコピーする
    func copy() {
        let text: String
        switch pane {
        case .summary where summary != nil:
            text = summary ?? ""
        default:
            text = lines.map { line in
                [line.time, line.speaker.map { "\($0):" }, line.text].compactMap { $0 }.joined(separator: " ")
            }.joined(separator: "\n")
        }
        guard !text.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        if !isSummarizing { notice = .info("コピーしました") }
    }

    /// 選んでいる議事録を Finder で見せる
    func revealInFinder() {
        guard let url = selected else { return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    // MARK: - 撮影用の見本

    /// 見た目の確認（`--ui-snapshot --meeting-sample`）用。ファイルも通信も触らない
    init(sample scenario: MeetingSnapshotScenario) {
        self.directory = TranscriptRecorder.directory
        self.chat = { _, _ in throw LLMChatError.unavailable }
        self.loadsFromDisk = false
        guard scenario != .empty else { return }

        let base = URL(fileURLWithPath: "/sample")
        let codes: [String?] = ["abc-defg-hij", "qrs-tuvw-xyz", nil, "mno-pqrs-tuv", "abc-defg-hij", nil, "jkl-mnop-qrs"]
        files = codes.enumerated().map { index, code in
            TranscriptFileInfo(
                url: base.appendingPathComponent("sample-\(index).md"),
                date: Date(timeIntervalSince1970: 1_794_000_000 - Double(index) * 93_600),
                meetingCode: code, lineCount: 240 - index * 31, characterCount: 18_400 - index * 2_300,
                hasSummary: index % 2 == 0
            )
        }
        selected = files.first?.url
        lines = Self.sampleLines
        switch scenario {
        case .empty, .recording:
            break
        case .summary:
            summary = Self.sampleSummary
            pane = .summary
        case .error:
            pane = .summary
            notice = .error(TranscriptSummarizer.message(for: LLMChatError.rateLimited(retryAfter: nil)))
        }
    }

    /// 見本の発言（長い行・話者なしの行も混ぜて、折り返しとはみ出しを確かめる）
    static let sampleLines: [TranscriptLine] = {
        let rows: [(String?, String)] = [
            ("山田", "それでは週次の定例を始めます。今日の議題は三つあります。"),
            ("佐藤", "はい、お願いします。"),
            ("山田", "一つ目は来月のリリースの日程です。二つ目は問い合わせ対応の当番、三つ目は来期の予算の見直しです。"),
            ("鈴木", "リリースは検証があと二日かかる見込みなので、十八日の火曜日でどうでしょうか。"),
            (nil, "十八日なら告知も間に合います。"),
            ("佐藤", "告知文は私が金曜までに下書きを出します。確認は山田さんにお願いしてもいいですか。"),
            ("山田", "分かりました。月曜の午前中に見ます。"),
            ("鈴木", "問い合わせの当番は、今週は私、来週は佐藤さんの順でお願いします。長くなりそうな件は、その日のうちに共有のメモに書いておいてもらえると、次の人が最初から読み直さずに済むので助かります。"),
            ("佐藤", "了解です。"),
            ("山田", "予算は資料がまだそろっていないので、来週に回します。"),
            ("鈴木", "では来週までに数字をまとめておきます。"),
            ("山田", "ありがとうございます。ほかに無ければ今日はここまでにします。"),
            ("佐藤", "お疲れさまでした。"),
            ("鈴木", "お疲れさまでした。"),
        ]
        return rows.enumerated().map { index, row in
            TranscriptLine(
                id: index + 1, time: String(format: "14:%02d:%02d", 2 + index / 3, (index * 17) % 60),
                speaker: row.0, text: row.1
            )
        }
    }()

    /// 見本の要約（欄より長くして、中でスクロールすることを確かめる）
    static let sampleSummary = """
        ## 要点
        - 来月のリリースは 18 日（火）に行う。検証があと 2 日かかる見込み
        - 告知は 18 日に間に合う
        - 問い合わせ対応の当番は、今週が鈴木さん、来週が佐藤さん
        - 長くなりそうな問い合わせは、その日のうちに共有のメモへ書く
        - 来期の予算の見直しは、資料がそろっていないので来週に回す

        ## 決まったこと
        - リリース日は 18 日（火）
        - 当番の順番は 鈴木さん → 佐藤さん
        - 予算の議題は来週の定例で扱う

        ## 次にやること
        - 告知文の下書きを金曜までに出す — 佐藤
        - 告知文を月曜の午前中に確認する — 山田
        - 予算の数字を来週までにまとめる — 鈴木
        """
}
