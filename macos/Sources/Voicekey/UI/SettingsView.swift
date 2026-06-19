//
//  SettingsView.swift
//  設定ウィンドウ（一般 / ホットキー / API キー）
//

import ServiceManagement
import SwiftUI

struct SettingsView: View {
    @ObservedObject var config: ConfigStore
    @ObservedObject var history: HistoryStore
    @ObservedObject var stats: StatsStore
    @State private var selectedTab: Int

    init(config: ConfigStore, history: HistoryStore, stats: StatsStore, initialTab: Int = 0) {
        self.config = config
        self.history = history
        self.stats = stats
        _selectedTab = State(initialValue: initialTab)
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            GeneralSettingsTab(config: config)
                .tabItem { Label("一般", systemImage: "gearshape") }
                .tag(0)
            SlotSettingsTab(title: "ホットキー 1", slot: $config.slot1)
                .tabItem { Label("ホットキー 1", systemImage: "1.circle") }
                .tag(1)
            SlotSettingsTab(title: "ホットキー 2", slot: $config.slot2)
                .tabItem { Label("ホットキー 2", systemImage: "2.circle") }
                .tag(2)
            StatsTab(stats: stats)
                .tabItem { Label("実績", systemImage: "trophy") }
                .tag(3)
            HistoryTab(history: history)
                .tabItem { Label("履歴", systemImage: "clock.arrow.circlepath") }
                .tag(4)
            // 配布ビルドは埋め込みキーで動くため、API キータブは出さない（テスターの混乱防止）
            if !EmbeddedKeys.isDist {
                ApiKeysTab()
                    .tabItem { Label("API キー", systemImage: "key") }
                    .tag(5)
            }
        }
        // fixedSize() だと NSHostingController 上で高さが潰れて
        // 入力欄が描画されないことがあるため、明示サイズを与える
        .frame(width: 480, height: 520)
    }
}

// MARK: - 一般

private struct GeneralSettingsTab: View {
    @ObservedObject var config: ConfigStore
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled
    /// 入力デバイス一覧（開いたタイミングと更新ボタンで読み直す）
    @State private var inputDevices: [AudioInputDevice] = AudioDevices.inputDevices()
    /// マイク自動検出の実行中フラグ（実行中はボタンを無効化する）
    @State private var isDetectingMic = false
    /// マイク自動検出の進捗・結果表示（nil なら非表示）
    @State private var micDetectStatus: String?

    var body: some View {
        Form {
            Picker("言語", selection: $config.language) {
                Text("日本語").tag("ja")
                Text("英語").tag("en")
                Text("自動判定").tag("")
            }

            LabeledContent("入力デバイス") {
                HStack(spacing: 8) {
                    Picker("", selection: $config.inputDeviceUID) {
                        Text("システム既定").tag("")
                        ForEach(inputDevices) { device in
                            Text(device.name).tag(device.uid)
                        }
                        // 保存済みデバイスが現在見つからない場合も選択を保持して表示する
                        if !config.inputDeviceUID.isEmpty,
                           !inputDevices.contains(where: { $0.uid == config.inputDeviceUID }) {
                            Text("（未接続のデバイス）").tag(config.inputDeviceUID)
                        }
                    }
                    .labelsHidden()
                    Button {
                        inputDevices = AudioDevices.inputDevices()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .help("デバイス一覧を更新")
                    Button("自動検出") {
                        startMicAutoDetect()
                    }
                    .disabled(isDetectingMic)
                    .help("全マイクを監視し、喋った声が入ったマイクを自動選択します")
                }
            }
            .onAppear { inputDevices = AudioDevices.inputDevices() }
            // 検出中・検出結果の表示（待ち時間を可視化する。無表示の待ちはバグと区別できない）
            if let micDetectStatus {
                Text(micDetectStatus)
                    .font(.caption)
                    .foregroundStyle(isDetectingMic ? Color.accentColor : .secondary)
            }
            LabeledContent("自動 Enter の遅延") {
                HStack {
                    TextField(
                        "",
                        value: $config.autoEnterDelayMs,
                        format: .number
                    )
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 60)
                    .multilineTextAlignment(.trailing)
                    Text("ms")
                }
            }
            Text("ホットキーを素早く 2 回押すと、貼り付け後に Enter を送信します。")
                .font(.caption)
                .foregroundStyle(.secondary)

            LabeledContent("ハンズフリー切替キー") {
                HStack(spacing: 8) {
                    HotkeyRecorderView(hotkey: $config.handsfreeKey)
                    if !config.handsfreeKey.isEmpty {
                        Button("クリア") { config.handsfreeKey = [] }
                            .font(.caption)
                    }
                }
            }
            Text("切替キー＋ホットキーで、トグル録音（1 回で開始・もう 1 回で停止）になります。修飾キー（右⇧ など）を推奨。")
                .font(.caption)
                .foregroundStyle(.secondary)

            // 製品版はテキスト整形のモデル・指示文を固定（UI 非公開）。
            // オンオフはホットキー各タブの「テキスト整形（LLM）」トグルで切り替える。

            Divider()

            Toggle("ログイン時に起動", isOn: $launchAtLogin)
                .onChange(of: launchAtLogin) { _, enabled in
                    do {
                        if enabled {
                            try SMAppService.mainApp.register()
                        } else {
                            try SMAppService.mainApp.unregister()
                        }
                    } catch {
                        // 開発実行（未バンドル）では失敗するため表示を戻すだけ
                        launchAtLogin = SMAppService.mainApp.status == .enabled
                    }
                }
        }
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }

    /// マイク自動検出を開始する。検出中はユーザーに喋るよう促し、結果を 2 秒表示する
    private func startMicAutoDetect() {
        isDetectingMic = true
        micDetectStatus = "自動検出中… マイクに向かって喋ってください"
        MicAutoDetector.detect { detection in
            isDetectingMic = false
            let displaySeconds: Double
            if let detection {
                config.inputDeviceUID = detection.device.uid
                inputDevices = AudioDevices.inputDevices()
                micDetectStatus = "「\(detection.device.name)」を選択しました"
                displaySeconds = 2
            } else {
                micDetectStatus = "音声を検出できませんでした。喋りながらもう一度お試しください"
                displaySeconds = 4
            }
            // 結果表示は数秒で消す（再実行中に消さないようガード）
            DispatchQueue.main.asyncAfter(deadline: .now() + displaySeconds) {
                if !isDetectingMic {
                    micDetectStatus = nil
                }
            }
        }
    }
}

// MARK: - ホットキースロット

private struct SlotSettingsTab: View {
    let title: String
    @Binding var slot: SlotConfig

    var body: some View {
        Form {
            LabeledContent("ホットキー") {
                HotkeyRecorderView(hotkey: $slot.hotkey)
                    .frame(width: 220)
            }

            Picker("動作", selection: $slot.mode) {
                ForEach(HotkeyMode.allCases) { mode in
                    Text(mode.label).tag(mode)
                }
            }
            .pickerStyle(.segmented)

            // 製品版は文字起こし 2 択（高速リアルタイム / 正確性）のみ。モデルは推奨固定で非選択。
            Picker("バックエンド", selection: $slot.backend) {
                ForEach(Backend.selectableCases) { backend in
                    Text(backend.label).tag(backend)
                }
            }
            .onChange(of: slot.backend) { _, newBackend in
                // バックエンド変更時はそのバックエンドの推奨モデルに固定で切り替える
                slot.model = newBackend.defaultModel
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("プロンプト（任意）")
                TextField("専門用語や固有名詞のヒントを入力", text: $slot.prompt, axis: .vertical)
                    .lineLimit(2...4)
                    .textFieldStyle(.roundedBorder)
            }

            Toggle("テキスト整形（LLM）", isOn: $slot.formatEnabled)
        }
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }
}

// MARK: - 実績

/// 使用実績タブ。
/// 使うほど貯まるレベル・推定節約時間・連続利用日数を表示し、継続利用の動機づけにする。
/// すべて貼り付け後のローカル集計なので、音声入力の速度には影響しない。
private struct StatsTab: View {
    @ObservedObject var stats: StatsStore

    var body: some View {
        Form {
            // レベルと次レベルまでの進捗
            Section {
                HStack(alignment: .firstTextBaseline) {
                    Text("レベル \(stats.level)")
                        .font(.title2).bold()
                    Spacer()
                    Text("経験値 \(stats.xp)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                ProgressView(value: stats.levelProgress)
                Text("あと \(stats.xpToNextLevel) 文字でレベル \(stats.level + 1)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            // 累計の実績
            Section {
                LabeledContent("推定節約時間", value: formattedSaved)
                LabeledContent("累計文字数", value: "\(stats.data.totalCharacters) 文字")
                LabeledContent("音声入力した回数", value: "\(stats.data.totalSessions) 回")
                LabeledContent("連続利用日数", value: streakText)
            }
            Text("「推定節約時間」は、同じ文章をキーボードで打つ場合と比べて短縮できた時間の目安です（タイピングより遅くなる短い入力は 0 として数えます）。実績はリセットできません。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }

    /// 累計節約時間を「X 時間 Y 分」等に整形する
    private var formattedSaved: String {
        let total = Int(stats.data.savedSeconds.rounded())
        if total >= 3600 {
            return "\(total / 3600) 時間 \((total % 3600) / 60) 分"
        }
        if total >= 60 {
            return "\(total / 60) 分 \(total % 60) 秒"
        }
        return "\(total) 秒"
    }

    private var streakText: String {
        "\(stats.data.currentStreak) 日（最長 \(stats.data.longestStreak) 日）"
    }
}

// MARK: - 履歴

/// 音声入力履歴タブ。
/// 直近 10 件をクリックでクリップボードにコピーできる（誤貼り付け・貼り付け失敗時の救出用）。
private struct HistoryTab: View {
    @ObservedObject var history: HistoryStore
    /// 直近にコピーしたエントリ（行に「コピーしました」を一時表示する）
    @State private var copiedId: UUID?

    var body: some View {
        Form {
            if history.items.isEmpty {
                Text("まだ履歴がありません。音声入力すると、ここに直近 \(HistoryStore.maxItems) 件が残ります。")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(history.items) { entry in
                    HistoryRow(entry: entry, copied: copiedId == entry.id) {
                        copyToClipboard(entry)
                    }
                }
                Button("履歴を消去", role: .destructive) {
                    history.clear()
                }
            }
        }
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }

    private func copyToClipboard(_ entry: HistoryEntry) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(entry.text, forType: .string)
        copiedId = entry.id
        // 1.5 秒後にフィードバック表示を消す（その間に別の行が押されたら上書きされる）
        Task {
            try? await Task.sleep(for: .seconds(1.5))
            if copiedId == entry.id { copiedId = nil }
        }
    }
}

private struct HistoryRow: View {
    let entry: HistoryEntry
    let copied: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 4) {
                Text(entry.text)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                HStack {
                    Text(entry.date.formatted(date: .abbreviated, time: .shortened))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    if copied {
                        Label("コピーしました", systemImage: "checkmark.circle.fill")
                            .font(.caption)
                            .foregroundStyle(.green)
                    } else {
                        Image(systemName: "doc.on.doc")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            // 行全体をクリック領域にする（テキスト部分だけだと押しにくい）
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - API キー

private struct ApiKeysTab: View {
    // 製品版で使うキーのみ表示（開発ビルドのみ表示されるタブ）。
    // 文字起こし 2 択（Deepgram/ElevenLabs）＋ 裏のテキスト整形に使う Groq。OpenAI は使わない。
    private let backends: [Backend] = [.deepgram, .elevenlabs, .groq]

    var body: some View {
        Form {
            ForEach(backends) { backend in
                ApiKeyRow(backend: backend)
            }
        }
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }
}

private struct ApiKeyRow: View {
    let backend: Backend
    @State private var input = ""
    @State private var saved = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(backend.providerName)
                    .fontWeight(.medium)
                if saved {
                    Label("設定済み", systemImage: "checkmark.circle.fill")
                        .font(.caption)
                        .foregroundStyle(.green)
                }
            }
            HStack {
                // グループ化フォーム内の既定スタイルは枠が描画されず
                // 入力欄と認識できないため、明示的に枠付きにする
                SecureField("API キーを入力", text: $input)
                    .textFieldStyle(.roundedBorder)
                Button("保存") {
                    let key = input.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !key.isEmpty else { return }
                    Keychain.setApiKey(key, for: backend)
                    input = ""
                    saved = true
                }
                .disabled(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                Button("削除") {
                    Keychain.deleteApiKey(for: backend)
                    saved = false
                }
                .disabled(!saved)
            }
        }
        .onAppear {
            saved = Keychain.hasApiKey(for: backend)
        }
    }
}
