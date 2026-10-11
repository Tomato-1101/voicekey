/// 設定ウィンドウの「議事録・会議」タブ
///
/// 「Google のやつに入る機能とかも、使い方がわかんない」（2026-10-11 ユーザー）への対応。
/// これまでボットの入口はメニューバーのサブメニューだけで、ログインできているか・いま何をしているか・
/// 議事録がどこにあるかが見えなかった。参加から議事録を開くまでをこの 1 画面で済むようにする
/// （メニューバー側は残す）。
///
/// 値の正本は `CaptionSettings`（文字起こしの方法・ボットの言語）と `ConfigStore`（保存のオンオフ）。
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

    var body: some View {
        MeetBotSettingsContent(config: config, bot: controller?.meetBot ?? Self.snapshotBot)
    }
}

/// 中身（ボットの状態を観測して描き直す）
@available(macOS 26.0, *)
private struct MeetBotSettingsContent: View {

    @ObservedObject var config: ConfigStore
    @ObservedObject var bot: MeetBotService

    @State private var urlText = ""
    @State private var source: MeetTranscriptSource = .meetCaptions
    @State private var language: CaptionLanguage = .japanese

    private var chromeInstalled: Bool { MeetBotService.isAvailable }
    private var urlIsValid: Bool { MeetBotService.normalizedMeetURL(urlText) != nil }

    var body: some View {
        Form {
            meetingSection
            loginSection
            methodSection
            transcriptSection
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 他タブと同じ「ガラスの棚」に揃える
        .formStyle(.grouped)
        .padding(.vertical, 8)
        .onAppear {
            source = CaptionSettings.meetTranscriptSource
            language = CaptionSettings.meetBotLanguage
            // 会議 URL は直前にコピーしていることが多いので、入っていれば初期値にする
            if urlText.isEmpty, let clipboard = NSPasteboard.general.string(forType: .string),
               MeetBotService.normalizedMeetURL(clipboard) != nil {
                urlText = clipboard.trimmingCharacters(in: .whitespacesAndNewlines)
            }
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
            LabeledContent {
                EmptyView()
            } label: {
                VStack(alignment: .leading, spacing: 3) {
                    Label(bot.statusLine, systemImage: statusIcon)
                        .foregroundStyle(statusColor)
                    Text(statusDetail).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if bot.state.isActive {
                LabeledContent {
                    Button("会議から退出") { bot.leave() }
                } label: {
                    SettingRowLabel(title: "退出すると議事録を閉じます",
                                    detail: "会議が終わったり外されたりしたときは自動で閉じます。")
                }
            } else {
                TextField("会議の URL", text: $urlText, prompt: Text("https://meet.google.com/abc-defg-hij"))
                    .onSubmit(join)
                LabeledContent {
                    Button("参加", action: join)
                        .keyboardShortcut(.defaultAction)
                        .disabled(!chromeInstalled || !urlIsValid)
                } label: {
                    SettingRowLabel(
                        title: "ボットを会議に入れる",
                        detail: "見えない Chrome が、下でログインした Google アカウントの名前で"
                            + "マイクとカメラをオフにして入ります。ほかの人の会議ではホストの承認が要ります。"
                    )
                }
            }
        }
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
            return "会議の URL を入れて「参加」を押してください。"
        case .launching, .joining:
            return "Chrome を裏で起動して会議に入っています。数十秒かかることがあります。"
        case .waitingForApproval:
            return "会議の画面でホストが「承認」を押すと記録が始まります（最大 10 分待ちます）。"
        case .recording:
            if !CaptionSettings.savesTranscript {
                return "議事録の保存がオフなので、何も保存していません。下の「議事録」でオンにできます。"
            }
            if let file = bot.currentTranscript {
                return "保存先: \(file.lastPathComponent)"
            }
            return "発言があると議事録に書き始めます。"
        case .ended:
            return bot.recordedLines > 0
                ? "議事録に \(bot.recordedLines) 行を保存しました。"
                : "議事録に残った発言はありませんでした。"
        case .failed:
            return "直したら、もう一度「参加」を押してください。"
        }
    }

    // MARK: - ログイン

    private var loginSection: some View {
        Section("ボット用ブラウザのログイン") {
            LabeledContent {
                Button("ログイン画面を開く") { bot.showLoginWindow() }
                    .disabled(!chromeInstalled || bot.state.isActive)
            } label: {
                VStack(alignment: .leading, spacing: 3) {
                    Label(bot.loginStatus.title,
                          systemImage: bot.loginStatus == .loggedIn ? "checkmark.seal" : "person.crop.circle.badge.questionmark")
                        .foregroundStyle(bot.loginStatus == .loggedOut ? .orange : .primary)
                    if let checkedAt = bot.loginCheckedAt {
                        Text("最終確認: \(checkedAt.formatted(date: .abbreviated, time: .shortened))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Text("ボットは普段の Chrome とは別のブラウザで動くので、最初に 1 回だけ Google にログインしてください。"
                + "ログイン画面のウィンドウを閉じると、ログインできたかを確かめます。"
                + "会議ではこのアカウント名で表示されるので、ボット用に別の Google アカウントを作って使うのがおすすめです。")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - 文字起こしの方法

    private var methodSection: some View {
        Section("文字起こしの方法") {
            Picker(selection: $source) {
                ForEach(MeetTranscriptSource.allCases, id: \.self) { item in
                    Text(item.displayName).tag(item)
                }
            } label: {
                SettingRowLabel(
                    title: "取り方",
                    detail: source == .meetCaptions
                        ? "Meet の字幕を読みます。話者名が残ります（認識は Google 側）。"
                        : "会議の音声をこの Mac の中で文字にします。音声は外に出ませんが、話者名は取れたときだけです。"
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

            if bot.state.isActive {
                Text("参加中は変えられません。次に参加したときから効きます。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - 議事録

    private var transcriptSection: some View {
        Section("議事録") {
            Toggle(isOn: $config.captionSaveTranscript) {
                SettingRowLabel(
                    title: "議事録を保存する",
                    detail: "ボットの議事録は会議ごとに 1 ファイル（参加から退出まで）。"
                        + "ライブ字幕の文字起こしもここで保存されます。"
                )
            }
            if !config.captionSaveTranscript {
                Label("オフのあいだは、ボットが会議に入っても何も保存しません。", systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange)
            }
            if let saveError = bot.saveError {
                Label("保存に失敗しています: \(saveError)", systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange)
            }
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
