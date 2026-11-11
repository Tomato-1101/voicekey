/// 設定ウィンドウの「議事録・会議」タブ
///
/// 「Google のやつに入る機能とかも、使い方がわかんない」（2026-10-11 ユーザー）への対応。
/// 参加 → 会議中の発言を見る → 過去の議事録を読む・要約する、までをこの 1 画面で済むようにする
/// （メニューバー側は残す）。
///
/// **操作や結果の表示で、ほかの欄の位置と大きさを動かさない**（恒久要件）。状態行・発言の欄・一覧・本文・
/// 要約・知らせの行は、空でも中身が最大でも同じ高さを確保し、はみ出す分は欄の中でスクロールする。
/// 条件で行を出したり消したりしない（押せないときは無効にするだけ）。
///
/// 値の正本は `CaptionSettings`（文字起こしの方法・ボットの言語）。
import AppKit
import SwiftUI

/// 議事録・会議タブ
@available(macOS 26.0, *)
struct MeetBotSettingsTab: View {

    @ObservedObject var config: ConfigStore
    var controller: AppController?

    /// controller が無いのは `--ui-snapshot`（見た目の確認）のときだけ。画面を空にすると
    /// 見た目を確かめられないので、どこにも結線されない表示専用のボットで中身を描く
    @MainActor private static let snapshotBot = MeetBotService()
    /// 撮影する見本の状態（`--ui-snapshot` のときだけ使う）
    @MainActor private static var snapshotScenario: MeetingSnapshotScenario = .empty

    /// 撮影の前に、見本の状態を切り替える（`--ui-snapshot --meeting-sample` 専用）
    @MainActor static func prepareSnapshot(_ scenario: MeetingSnapshotScenario) {
        snapshotScenario = scenario
        snapshotBot.applySnapshotSample(scenario)
    }

    var body: some View {
        if let controller {
            MeetBotSettingsContent(bot: controller.meetBot, notes: MeetingNotesModel(), sampleURL: nil)
        } else {
            // 撮影では本人の議事録フォルダもクリップボードも読まない（見本だけで描く）
            MeetBotSettingsContent(
                bot: Self.snapshotBot, notes: MeetingNotesModel(sample: Self.snapshotScenario),
                sampleURL: Self.snapshotScenario == .empty ? "" : "https://meet.google.com/abc-defg-hij"
            )
        }
    }
}

/// 中身（ボットの状態を観測して描き直す）
@available(macOS 26.0, *)
private struct MeetBotSettingsContent: View {

    @ObservedObject var bot: MeetBotService
    @StateObject private var notes: MeetingNotesModel
    /// 撮影用の URL 欄の中身（nil なら通常どおりクリップボードから初期値を入れる）
    private let sampleURL: String?

    @State private var urlText = ""
    @State private var source: MeetTranscriptSource = .meetCaptions
    @State private var language: CaptionLanguage = .japanese
    /// 参加したときゲストになるか（ログインの確認中に案内文を行き来させないよう、確認が済んだ値を持つ）
    @State private var joinsAsGuest = true

    init(bot: MeetBotService, notes: @autoclosure @escaping () -> MeetingNotesModel, sampleURL: String?) {
        self.bot = bot
        _notes = StateObject(wrappedValue: notes())
        self.sampleURL = sampleURL
        _urlText = State(initialValue: sampleURL ?? "")
        _joinsAsGuest = State(initialValue: bot.loginStatus.joinsAsGuest)
    }

    private var chromeInstalled: Bool { MeetBotService.isAvailable }
    private var urlIsValid: Bool { MeetBotService.normalizedMeetURL(urlText) != nil }

    var body: some View {
        Form {
            // 上から「入る → 会議中に見る → 後で読む」の順。設定とログイン（任意）は下
            meetingSection
            liveSection
            notesSection
            methodSection
            loginSection
            storageSection
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 他タブと同じ「ガラスの棚」に揃える
        .formStyle(.grouped)
        .padding(.vertical, 8)
        .onAppear {
            source = CaptionSettings.meetTranscriptSource
            language = CaptionSettings.meetBotLanguage
            notes.reload()
            // 会議 URL は直前にコピーしていることが多いので、入っていれば初期値にする
            if sampleURL == nil, urlText.isEmpty,
               let clipboard = NSPasteboard.general.string(forType: .string),
               MeetBotService.normalizedMeetURL(clipboard) != nil {
                urlText = clipboard.trimmingCharacters(in: .whitespacesAndNewlines)
            }
        }
        .onChange(of: bot.loginStatus) { _, status in
            if status != .checking { joinsAsGuest = status.joinsAsGuest }
        }
        // 議事録ファイルができた・会議が終わったら一覧を読み直す
        .onChange(of: bot.currentTranscript) { _, _ in notes.reload() }
        .onChange(of: bot.state.isActive) { _, _ in notes.reload() }
        // 記録中の議事録を開いているときは、行が増えるたびに本文も読み直す
        .onChange(of: bot.recordedLines) { _, _ in
            if notes.selected != nil, notes.selected == bot.currentTranscript { notes.reloadSelected() }
        }
    }

    // MARK: - 会議

    private var meetingSection: some View {
        Section("会議に参加") {
            if !chromeInstalled {
                Label("Google Chrome が必要です。インストールすると使えるようになります。",
                      systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
            }
            // 状態と補足は 2 行ぶんずつ、高さを数字で固定する（文の長さで下の欄が動かないように）。
            // `reservesSpace` だけだと、1 行のときと実際に 2 行に折り返したときで 1pt ずれた（実測）
            VStack(alignment: .leading, spacing: 3) {
                Label(bot.statusLine, systemImage: statusIcon)
                    .foregroundStyle(statusColor)
                    .lineLimit(2)
                    .frame(height: 36, alignment: .topLeading)
                    .help(bot.statusLine)
                Text(statusDetail).font(.caption).foregroundStyle(.secondary)
                    .lineLimit(2)
                    .frame(height: 28, alignment: .topLeading)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            TextField("会議の URL", text: $urlText, prompt: Text("https://meet.google.com/abc-defg-hij"))
                .onSubmit(join)
                .disabled(bot.state.isActive)

            LabeledContent {
                HStack {
                    // ゲストで入れなかったときだけ押せる。場所は常に取っておく
                    Button("Google にログイン") { bot.showLoginWindow() }
                        .opacity(suggestsLogin ? 1 : 0)
                        .disabled(!suggestsLogin || !chromeInstalled)
                        .accessibilityHidden(!suggestsLogin)
                    if bot.state.isActive {
                        Button { bot.leave() } label: { Text("会議から退出").frame(width: 84) }
                    } else {
                        Button(action: join) { Text("参加").frame(width: 84) }
                            .keyboardShortcut(.defaultAction)
                            .disabled(!chromeInstalled || !urlIsValid)
                    }
                }
            } label: {
                FixedRowLabel(
                    title: "ボットを会議に入れる",
                    detail: joinsAsGuest
                        ? "ゲスト「\(MeetBotService.botDisplayName)」として参加をリクエストします。主催者の承認が要ります。"
                        : "ログイン中の Google アカウントの名前で入ります。ほかの人の会議では主催者の承認が要ります。",
                    lines: 2
                )
            }
        }
    }

    /// 直近の失敗が「ログインすれば入れる見込みがある」ものか
    private var suggestsLogin: Bool {
        !bot.state.isActive && bot.lastFailure?.suggestsLogin == true
    }

    private func join() {
        guard chromeInstalled, urlIsValid, !bot.state.isActive else { return }
        bot.join(urlString: urlText)
    }

    private var statusIcon: String {
        switch bot.state {
        case .idle: return "pause.circle"
        case .launching, .joining: return "hourglass"
        case .waitingForApproval: return "person.badge.clock"
        case .recording: return bot.saveError == nil ? "record.circle" : "exclamationmark.triangle"
        case .ended: return "checkmark.circle"
        case .failed: return "exclamationmark.triangle"
        }
    }

    private var statusColor: Color {
        switch bot.state {
        case .recording: return bot.saveError == nil ? Brand.signal : .orange
        case .failed: return .orange
        default: return .primary
        }
    }

    /// 状態の下に出す補足（次に何をすればいいか）
    private var statusDetail: String {
        switch bot.state {
        case .idle:
            return "会議の URL を入れて「参加」を押してください。Google にログインしていなくても入れます。"
        case .launching, .joining:
            return "Chrome を裏で起動して会議に入っています。数十秒かかることがあります。"
        case .waitingForApproval:
            return "会議の画面で主催者が「承認」を押すと記録が始まります。"
        case .recording:
            if let file = bot.currentTranscript {
                return "保存先: \(file.lastPathComponent)"
            }
            return "発言があると議事録に書き始めます。"
        case .ended:
            return bot.recordedLines > 0
                ? "議事録に \(bot.recordedLines) 行を保存しました。"
                : "議事録に残った発言はありませんでした。"
        case .failed:
            return suggestsLogin
                ? "ボット用ブラウザで Google にログインしてから、もう一度「参加」を押してください。"
                : "直したら、もう一度「参加」を押してください。"
        }
    }

    // MARK: - 記録中の発言

    private var liveSection: some View {
        Section("記録中の発言") {
            LiveLinesPane(lines: bot.liveLines)
                .frame(height: 160)
                .paneBackground()
        }
    }

    // MARK: - 過去の議事録

    private var notesSection: some View {
        Section("過去の議事録") {
            fileList
                .frame(height: 132)
                .paneBackground()

            HStack {
                Picker("表示", selection: $notes.pane) {
                    Text("本文").tag(MeetingNotesModel.Pane.transcript)
                    Text("要約").tag(MeetingNotesModel.Pane.summary)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 140)
                Spacer()
                Button("Finder で表示") { notes.revealInFinder() }
                Button("コピー") { notes.copy() }
                // 「要約」と「作り直す」で幅が変わらないよう、文字の幅を固定する
                Button { notes.summarize() } label: {
                    Text(notes.summary == nil ? "要約" : "作り直す").frame(width: 56)
                }
                .disabled(notes.isSummarizing)
            }
            .disabled(notes.selected == nil)

            notePane
                .frame(height: 220)
                .paneBackground()

            noticeRow
        }
    }

    /// 議事録の一覧（新しい順）
    private var fileList: some View {
        ScrollView {
            LazyVStack(spacing: 2) {
                ForEach(notes.files) { info in
                    Button { notes.select(info.url) } label: {
                        fileRow(info, selected: info.url == notes.selected)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(6)
        }
        .overlay {
            if notes.files.isEmpty { PaneHint("まだ議事録がありません。会議に参加すると、ここに並びます。") }
        }
    }

    private func fileRow(_ info: TranscriptFileInfo, selected: Bool) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 1) {
                Text(TranscriptLibrary.title(for: info)).font(.subheadline.weight(.semibold))
                Text(TranscriptLibrary.detail(for: info)).font(.caption)
                    .foregroundStyle(selected ? Color.white.opacity(0.85) : Color.secondary)
            }
            .lineLimit(1)
            Spacer()
            Text("要約あり").font(.caption)
                .foregroundStyle(selected ? Color.white.opacity(0.85) : Color.secondary)
                .opacity(info.hasSummary ? 1 : 0)
        }
        .foregroundStyle(selected ? Color.white : Color.primary)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .contentShape(Rectangle())
        .background {
            // 左のナビの選択中と同じ見た目（アクセントのグラデ＋白リム）
            if selected {
                RoundedRectangle(cornerRadius: 9)
                    .fill(LinearGradient(
                        colors: [Brand.signal, Brand.signal.opacity(0.75)], startPoint: .top, endPoint: .bottom
                    ))
                    .overlay(
                        RoundedRectangle(cornerRadius: 9)
                            .strokeBorder(LinearGradient(
                                colors: [.white.opacity(0.4), .white.opacity(0.0)], startPoint: .top, endPoint: .bottom
                            ), lineWidth: 1)
                    )
            }
        }
    }

    /// 本文か要約
    @ViewBuilder
    private var notePane: some View {
        switch notes.pane {
        case .transcript:
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 6) {
                    ForEach(notes.lines) { TranscriptLineRow(line: $0) }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
            }
            .overlay {
                if notes.lines.isEmpty {
                    PaneHint(notes.selected == nil
                        ? "上の一覧から議事録を選ぶと、ここに本文が出ます。"
                        : "この議事録には発言がありません。")
                }
            }
        case .summary:
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array((notes.summary ?? "").split(separator: "\n").enumerated()), id: \.offset) { _, row in
                        // 「## 見出し」は太字にするだけ（Markdown の記号はそのまま読める形で保存してある）
                        if row.hasPrefix("## ") {
                            Text(row.dropFirst(3)).font(.subheadline.weight(.semibold)).padding(.top, 4)
                        } else {
                            Text(row).font(.subheadline)
                        }
                    }
                }
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
            }
            .overlay {
                if notes.summary == nil {
                    PaneHint(notes.selected == nil
                        ? "上の一覧から議事録を選んでください。"
                        : "まだ要約がありません。「要約」を押すと作ります。")
                }
            }
        }
    }

    /// 知らせの 1 行（進み具合・済んだこと・失敗）。何も無いときも同じ高さで使い方を出す
    private var noticeRow: some View {
        HStack(spacing: 6) {
            switch notes.notice {
            case .none:
                Text("要約は Groq の無料モデルで作り、議事録の隣に保存します。").foregroundStyle(.secondary)
            case let .info(text):
                Label(text, systemImage: "checkmark.circle").foregroundStyle(.secondary)
            case let .progress(text):
                ProgressView().controlSize(.small)
                Text(text).foregroundStyle(.secondary)
            case let .error(text):
                Label(text, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            }
        }
        .font(.caption)
        .lineLimit(1)
        .frame(maxWidth: .infinity, minHeight: 18, maxHeight: 18, alignment: .leading)
    }

    // MARK: - 文字起こしの方法

    private var methodSection: some View {
        Section("文字起こしの方法") {
            Picker(selection: $source) {
                ForEach(MeetTranscriptSource.allCases, id: \.self) { item in
                    Text(item.displayName).tag(item)
                }
            } label: {
                FixedRowLabel(
                    title: "取り方",
                    detail: source == .meetCaptions
                        ? "Meet の字幕を読みます。話者名が残ります（認識は Google 側）。"
                        : "会議の音声をこの Mac の中で文字にします。音声は外に出ませんが、話者名は取れたときだけです。",
                    lines: 2
                )
            }
            .disabled(bot.state.isActive)
            .onChange(of: source) { _, newValue in CaptionSettings.meetTranscriptSource = newValue }

            Picker(selection: $language) {
                ForEach(CaptionLanguage.allCases, id: \.self) { item in
                    Text(item.displayName).tag(item)
                }
            } label: {
                SettingRowLabel(title: "会議の言語", detail: "会議で話される言語を選んでください。ライブ字幕の設定とは別です。")
            }
            .disabled(bot.state.isActive)
            .onChange(of: language) { _, newValue in CaptionSettings.meetBotLanguage = newValue }

            Text("参加中は変えられません。変えた設定は、次に参加したときから効きます。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    // MARK: - ログイン（任意）

    private var loginSection: some View {
        Section("ボット用ブラウザのログイン（任意）") {
            LabeledContent {
                Button("ログイン画面を開く") { bot.showLoginWindow() }
                    .disabled(!chromeInstalled || bot.state.isActive)
            } label: {
                VStack(alignment: .leading, spacing: 3) {
                    Label(bot.loginStatus.title,
                          systemImage: bot.loginStatus == .loggedIn ? "checkmark.seal" : "person.crop.circle.badge.questionmark")
                    Text(bot.loginCheckedAt.map { "最終確認: \($0.formatted(date: .abbreviated, time: .shortened))" }
                        ?? "まだ確かめていません")
                        .font(.caption).foregroundStyle(.secondary)
                }
                .lineLimit(1)
            }
            Text("ログインしなくても、ゲストとして参加をリクエストできます。"
                + "ゲストが入れない会議や、承認なしで自分の会議に入りたいときだけログインしてください。"
                + "会議ではログインしたアカウントの名前で表示されるので、ボット用に別の Google アカウントを使うのがおすすめです。")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - 保存先

    private var storageSection: some View {
        Section("保存先") {
            // ライブ字幕の保存スイッチとは独立（ボットの役目は議事録なので、スイッチに関係なく必ず保存する）
            Text("ボットの議事録は会議ごとに 1 ファイルで必ず保存されます（参加から退出まで）。")
                .font(.caption).foregroundStyle(.secondary)
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)
            LabeledContent {
                HStack {
                    Button("最新の議事録を開く") { openLatest() }
                    Button("保存フォルダを開く") { openDirectory() }
                }
            } label: {
                VStack(alignment: .leading, spacing: 3) {
                    Text("保存先")
                    Text(TranscriptRecorder.directory.path)
                        .font(.caption).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle)
                }
            }
        }
    }

    /// 最新の議事録を開く（まだ 1 つも無ければフォルダを開く）
    private func openLatest() {
        if let file = TranscriptRecorder.latestFile() {
            NSWorkspace.shared.open(file)
        } else {
            openDirectory()
        }
    }

    /// 議事録フォルダを Finder で開く（無ければ作ってから開く）
    private func openDirectory() {
        let directory = TranscriptRecorder.directory
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        NSWorkspace.shared.open(directory)
    }
}

// MARK: - 部品

/// 記録中の発言が流れる欄
///
/// 新しい行が来たら末尾へ送る。ただし利用者が上へ戻して読んでいる間は動かさない。
@available(macOS 26.0, *)
private struct LiveLinesPane: View {

    let lines: [TranscriptLine]

    /// 末尾に付いているか（利用者が上へ戻したら false）
    @State private var pinned = true

    private static let endID = "end"

    /// スクロール位置（「末尾にいるか」を、位置が動いたときだけ判定し直すために持つ）
    private struct Position: Equatable {
        let offset: CGFloat
        let maxOffset: CGFloat
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 6) {
                    ForEach(lines) { TranscriptLineRow(line: $0) }
                    Color.clear.frame(height: 1).id(Self.endID)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
            }
            .defaultScrollAnchor(.bottom)
            .onScrollGeometryChange(for: Position.self) { geometry in
                Position(
                    offset: geometry.contentOffset.y,
                    maxOffset: geometry.contentSize.height - geometry.containerSize.height
                )
            } action: { old, new in
                // 行が増えて中身が伸びただけ（位置は同じ）のときは判定を変えない。
                // そこで「末尾にいない」にすると、次の行から追いかけなくなる
                guard old.offset != new.offset else { return }
                pinned = new.offset >= new.maxOffset - 24
            }
            .onChange(of: lines.last?.id) { _, _ in
                if pinned { proxy.scrollTo(Self.endID, anchor: .bottom) }
            }
        }
        .overlay {
            if lines.isEmpty { PaneHint("会議に参加すると、発言がここに流れます。") }
        }
    }
}

/// 発言 1 行（時刻・話者・本文）
private struct TranscriptLineRow: View {

    let line: TranscriptLine

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(line.time).font(.caption).monospacedDigit().foregroundStyle(.secondary)
            (Text(line.speaker.map { "\($0)  " } ?? "").fontWeight(.semibold) + Text(line.text))
                .font(.subheadline)
                .textSelection(.enabled)
        }
    }
}

/// 欄が空のときの案内（欄の真ん中に出す。欄の大きさは変えない）
private struct PaneHint: View {

    let text: String

    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text).font(.caption).foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            .padding(.horizontal, 16)
    }
}

/// 説明の行数を固定した行ラベル（文の長さが変わっても行の高さを変えない）
private struct FixedRowLabel: View {

    let title: String
    let detail: String
    let lines: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
            Text(detail).font(.caption).foregroundStyle(.secondary)
                .lineLimit(lines, reservesSpace: true)
        }
    }
}

private extension View {
    /// 行の中に置く欄の下地（ナビのホバーと同じ薄いフィル）
    func paneBackground() -> some View {
        frame(maxWidth: .infinity)
            .background(RoundedRectangle(cornerRadius: 9).fill(Color.primary.opacity(0.07)))
            .clipShape(RoundedRectangle(cornerRadius: 9))
    }
}
