//
//  SettingsView.swift
//  設定ウィンドウ（一般 / ホットキー / API キー）
//

import Charts
import ServiceManagement
import SwiftUI

struct SettingsView: View {
    @ObservedObject var config: ConfigStore
    @ObservedObject var history: HistoryStore
    @ObservedObject var stats: StatsStore
    @State private var selectedTab: Int
    /// サイドバーを畳んでいるか（true ならアイコンのみのレール）
    @State private var sidebarCollapsed = false
    /// ホバー中のナビ項目 id（非選択項目に薄いハイライトを出すため）
    @State private var hoveredNav: Int?

    init(config: ConfigStore, history: HistoryStore, stats: StatsStore, initialTab: Int = 0) {
        self.config = config
        self.history = history
        self.stats = stats
        _selectedTab = State(initialValue: initialTab)
    }

    /// サイドバーの 1 項目（id は selectedTab のタグと一致）
    private struct NavItem: Identifiable {
        let id: Int
        let title: String
        let icon: String
    }

    /// 表示するナビ項目（配布ビルドでは API キーを出さない）
    private var navItems: [NavItem] {
        var items: [NavItem] = [
            .init(id: 0, title: "一般", icon: "gearshape"),
            .init(id: 1, title: "録音キー 1（メイン）", icon: "1.circle"),
            .init(id: 2, title: "録音キー 2（サブ）", icon: "2.circle"),
            .init(id: 3, title: "実績", icon: "trophy"),
            .init(id: 4, title: "履歴", icon: "clock.arrow.circlepath"),
            .init(id: 8, title: "ユーザー辞書", icon: "character.book.closed"),
            .init(id: 6, title: "アカウント", icon: "person.crop.circle"),
            .init(id: 7, title: "バージョン情報", icon: "info.circle"),
        ]
        // 配布ビルドは埋め込みキーで動くため、API キーは出さない（テスターの混乱防止）
        if !EmbeddedKeys.isDist {
            items.append(.init(id: 5, title: "API キー", icon: "key"))
        }
        return items
    }

    var body: some View {
        // レイアウト v2.1: 浮くガラス島は「サイドバーだけ」。右のコンテンツはウィンドウの
        // 背景面としてフラットに敷き、島（角丸・影・リム）にしない（Claude デスクトップの
        // サイドバー＋本文と同じ関係）。frosted backdrop とウォッシュはウィンドウ全面に残す。
        HStack(spacing: 12) {
            sidebar
                .clipShape(RoundedRectangle(cornerRadius: 18))  // ナビのスクロールが角からはみ出さないように
                .glassIsland(cornerRadius: 18)
                .padding(.leading, 12)   // 島の左に下地を見せる
                .padding(.vertical, 12)  // 島の上下に下地を見せる
            contentPane                  // 右ペインはウィンドウ端まで広がるフラットな背景面（島にしない）
        }
        // ウィンドウ幅は据え置き（サイドバー島のマージン込みで 700x600）
        .frame(width: 700, height: 600)
        .glassButtons()             // 配下の Button を一括ガラス化（.plain 明示ボタンは影響なし）
        .frostedWindowBackground()  // ウィンドウ全面のすりガラス下地
    }

    /// 左サイドバー（ブランド＋開閉トグル＋スクロール可能なナビ）。背景は body 側で島にする。
    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            brandHeader
            // ナビ（項目が増えても届くようスクロール可能にする）
            ScrollView {
                VStack(spacing: 2) {
                    ForEach(navItems) { item in
                        navButton(item)
                    }
                }
                .padding(.horizontal, 8)
                .padding(.bottom, 8)
            }
        }
        .frame(width: sidebarCollapsed ? 60 : 192)
    }

    /// サイドバー先頭のブランド行（アプリアイコン＋名称＋開閉トグル）。畳み時はアイコン＋トグルを縦積み。
    private var brandHeader: some View {
        Group {
            if sidebarCollapsed {
                VStack(spacing: 8) {
                    appIcon
                    collapseToggle
                }
                .frame(maxWidth: .infinity)
            } else {
                HStack(spacing: 8) {
                    appIcon
                    VStack(alignment: .leading, spacing: 0) {
                        Text("voicekey").font(.headline)
                        Text("設定").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                    collapseToggle
                }
            }
        }
        .padding(.horizontal, sidebarCollapsed ? 0 : 14)
        .padding(.top, 14)
        .padding(.bottom, 10)
    }

    /// アプリアイコン（ブランド格上げ用・角丸表示）
    private var appIcon: some View {
        Image(nsImage: NSApp.applicationIconImage ?? NSImage())
            .resizable()
            .frame(width: 30, height: 30)
            .clipShape(RoundedRectangle(cornerRadius: 7))
    }

    /// サイドバー開閉トグル（アイコンのみのレール ⇄ ラベル付き）
    private var collapseToggle: some View {
        Button {
            withAnimation(.easeInOut(duration: 0.22)) { sidebarCollapsed.toggle() }
        } label: {
            Image(systemName: "sidebar.left")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(.secondary)
        }
        .buttonStyle(.plain)
        .help(sidebarCollapsed ? "サイドバーを開く" : "サイドバーを閉じる")
    }

    /// 右のコンテンツ島（現在タブ名のヘッダ＋選択中ページ）。
    /// 右ペイン（現在タブの中身）。島にはせず、ウィンドウの背景面としてフラットに敷く。
    /// 角丸・影・リムは付けない（frosted backdrop がそのまま透ける。Form 側は
    /// scrollContentBackground(.hidden) 済みなので下地が見える）。
    private var contentPane: some View {
        VStack(alignment: .leading, spacing: 0) {
            contentHeader
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(.top, 12)  // タイトルバー帯からヘッダを逃がす（右・下はウィンドウ端まで広げる）
    }

    /// コンテンツ島の上部ヘッダ（現在タブ名）。System Settings 的な階層感を出す。
    private var contentHeader: some View {
        Text(navItems.first { $0.id == selectedTab }?.title ?? "")
            .font(.title3.weight(.semibold))
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 16)
            .padding(.horizontal, 20)
            .padding(.bottom, 4)
    }

    /// ナビ 1 項目のボタン（選択中はアクセントのグラデピル。ホバーで薄いハイライト。畳むとアイコンのみ）
    private func navButton(_ item: NavItem) -> some View {
        let selected = selectedTab == item.id
        let hovered = hoveredNav == item.id
        return Button {
            selectedTab = item.id
        } label: {
            HStack(spacing: 10) {
                Image(systemName: item.icon)
                    .font(.system(size: 14))
                    .frame(width: 20)
                if !sidebarCollapsed {
                    Text(item.title)
                        .font(.system(size: 13))
                        .lineLimit(1)
                    Spacer(minLength: 0)
                }
            }
            .padding(.vertical, 6)
            .padding(.horizontal, sidebarCollapsed ? 0 : 10)
            .frame(maxWidth: .infinity, alignment: sidebarCollapsed ? .center : .leading)
            .background(navBackground(selected: selected, hovered: hovered))
            .foregroundStyle(selected ? Color.white : Color.primary)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering in
            if hovering { hoveredNav = item.id }
            else if hoveredNav == item.id { hoveredNav = nil }
        }
        .help(item.title)
    }

    /// ナビ項目の背景。選択中＝アクセントのグラデピル＋白リム＋グロー影、ホバー＝薄いフィル、他＝透明。
    @ViewBuilder
    private func navBackground(selected: Bool, hovered: Bool) -> some View {
        if selected {
            RoundedRectangle(cornerRadius: 9)
                .fill(LinearGradient(
                    colors: [Color.accentColor, Color.accentColor.opacity(0.75)],
                    startPoint: .top, endPoint: .bottom
                ))
                .overlay(
                    RoundedRectangle(cornerRadius: 9)
                        .strokeBorder(LinearGradient(
                            colors: [.white.opacity(0.4), .white.opacity(0.0)],
                            startPoint: .top, endPoint: .bottom
                        ), lineWidth: 1)
                )
                .shadow(color: Color.accentColor.opacity(0.45), radius: 8, x: 0, y: 3)
        } else if hovered {
            RoundedRectangle(cornerRadius: 9)
                .fill(Color.primary.opacity(0.07))
        } else {
            Color.clear
        }
    }

    /// 選択中ページ（タグは旧 TabView と同じ値を踏襲）
    @ViewBuilder private var content: some View {
        switch selectedTab {
        case 0: GeneralSettingsTab(config: config)
        case 1: SlotSettingsTab(title: "録音キー 1（メイン）", slot: $config.slot1)
        case 2: SlotSettingsTab(title: "録音キー 2（サブ）", slot: $config.slot2)
        case 3: StatsTab(stats: stats)
        case 4: HistoryTab(history: history)
        case 8: DictionaryTab(config: config)
        case 6: AccountTab()
        case 7: AboutTab()
        case 5: ApiKeysTab()
        default: GeneralSettingsTab(config: config)
        }
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

            LabeledContent("マイク") {
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
            LabeledContent("ダブルタップ送信の待ち時間") {
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
            Text("録音キーを素早く2回押したとき、貼り付け後に Enter を自動で押すまでの待ち時間です。")
                .font(.caption)
                .foregroundStyle(.secondary)

            LabeledContent("ハンズフリーキー") {
                HStack(spacing: 8) {
                    HotkeyRecorderView(hotkey: $config.handsfreeKey)
                    if !config.handsfreeKey.isEmpty {
                        Button("クリア") { config.handsfreeKey = [] }
                            .font(.caption)
                    }
                }
            }
            Text("このキーを押しながら録音キーを押すと、押しっぱなしにしなくても録音が続きます（もう一度録音キーを押すと停止）。修飾キー（右⇧ など）がおすすめです。")
                .font(.caption)
                .foregroundStyle(.secondary)

            LabeledContent("最後の文字起こしを貼り付け") {
                HStack(spacing: 8) {
                    HotkeyRecorderView(hotkey: $config.repasteKey)
                    if !config.repasteKey.isEmpty {
                        Button("クリア") { config.repasteKey = [] }
                            .font(.caption)
                    }
                }
            }
            Text("このキーを押すと、直前に入力したテキストをもう一度貼り付けます（クリアで無効になります）。")
                .font(.caption)
                .foregroundStyle(.secondary)

            // 製品版はテキスト整形のモデル・指示文を固定（UI 非公開）。
            // オンオフは録音キー各タブの「文章を自動で整える」トグルで切り替える。

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

            Section("表示") {
                Toggle("ピルを常に表示", isOn: $config.hudAlwaysVisible)
                Toggle("サイドノッチを表示", isOn: $config.sideNotchEnabled)
                Toggle("Dock に表示", isOn: $config.dockIconAlwaysVisible)
            }

            Section("サウンド") {
                Toggle("操作音", isOn: $config.soundEffectsEnabled)
                Toggle("音声入力中はメディアの音量を下げる", isOn: $config.duckMediaEnabled)
            }

            Section("履歴") {
                Toggle("履歴を保存", isOn: $config.historyEnabled)
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
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

            Picker("録音のしかた", selection: $slot.mode) {
                ForEach(HotkeyMode.allCases) { mode in
                    Text(mode.label).tag(mode)
                }
            }
            .pickerStyle(.segmented)

            // 製品版は文字起こし 2 択（リアルタイム / スタンダード）のみ。モデルは推奨固定で非選択。
            Picker("文字起こしモード", selection: $slot.backend) {
                ForEach(Backend.selectableCases) { backend in
                    Text(backend.label).tag(backend)
                }
            }
            .onChange(of: slot.backend) { _, newBackend in
                // バックエンド変更時はそのバックエンドの推奨モデルに固定で切り替える
                slot.model = newBackend.defaultModel
                // 整形トグルはそのモードの既定へ追従させる（リアルタイム=既定 OFF・
                // スタンダード=既定 ON）。ユーザーはこの後トグルで自由に上書きできる。
                slot.formatEnabled = newBackend.defaultFormatEnabled
            }

            // 選択中モードの説明（薄字）。スタンダード(groq)はハンズフリー自動切替の1行も添える。
            Text(Self.backendCaption(slot.backend))
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)

            Toggle("文章を自動で整える", isOn: $slot.formatEnabled)
            Text("「えー」「あの」などの言いよどみを除去し、句読点や改行を整えます。")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }

    /// 選択中の文字起こしモードの説明文（Windows 版 _backend_caption_text と同一文言）。
    /// スタンダード(groq)はハンズフリー録音時に内部で高精度エンジン(ElevenLabs)へ切替する旨も添える。
    private static func backendCaption(_ backend: Backend) -> String {
        switch backend {
        case .deepgram:
            return "話しながら文字が表示されます（最速）"
        case .groq:
            return "録音後にきれいな文章にして入力します（おすすめ）\n"
                + "ハンズフリー録音のときは、長い録音に強いエンジンへ自動で切り替わります。"
        default:
            return ""
        }
    }
}

// MARK: - 実績

/// チャートの期間（棒グラフの集計単位）
private enum StatsPeriod: String, CaseIterable, Identifiable {
    case week = "週"
    case month = "月"
    case year = "年"
    var id: String { rawValue }
}

/// 使用実績タブ。
/// 「今日 / 今週 / 累計」のサマリーと、期間を切り替えられる入力量チャートで
/// どれだけ使ったかを可視化し、継続利用の達成感につなげる。
/// すべて貼り付け後のローカル集計なので、音声入力の速度には影響しない。
private struct StatsTab: View {
    @ObservedObject var stats: StatsStore
    @State private var period: StatsPeriod = .week
    /// 棒が 0 から伸びる初期アニメーション用
    @State private var animateBars = false
    /// サマリー数値のカウントアップ用
    @State private var appeared = false

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

            // サマリー（今日 / 今週 / 累計）。大きな数字をカウントアップして達成感を出す
            Section {
                HStack(spacing: 12) {
                    statTile(title: "今日", value: todayChars,
                             sub: "\(todaySessions) 回")
                    Divider()
                    statTile(title: "今週", value: weekChars,
                             sub: "録音 \(formattedMinutes(stats.recordingSecondsInLast(days: 7)))")
                    Divider()
                    statTile(title: "累計", value: stats.totalCharacters,
                             sub: "Lv.\(stats.level)")
                }
                .frame(height: 58)
            }

            // 入力量チャート（期間切替・棒グラフ）
            Section {
                Picker("期間", selection: $period) {
                    ForEach(StatsPeriod.allCases) { p in Text(p.rawValue).tag(p) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()

                if hasData {
                    chart.frame(height: 170)
                } else {
                    Text("まだデータがありません。音声入力すると、ここに入力量が表示されます。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity, minHeight: 120, alignment: .center)
                }
            } header: {
                Text("入力量（文字数）")
            }

            // 累計の実績
            Section {
                LabeledContent("推定節約時間", value: formattedSaved)
                LabeledContent("累計文字数", value: "\(stats.totalCharacters) 文字")
                LabeledContent("音声入力した回数", value: "\(stats.totalSessions) 回")
                LabeledContent("連続利用日数", value: streakText)
            }
            Text("「推定節約時間」は、同じ文章をキーボードで打つ場合と比べて短縮できた時間の目安です（タイピングより遅くなる短い入力は 0 として数えます）。実績はリセットできません。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
        .onAppear {
            // 数値のカウントアップと棒の伸びを開いた瞬間に走らせる
            withAnimation(.easeOut(duration: 0.9)) { appeared = true }
            withAnimation(.easeOut(duration: 0.7)) { animateBars = true }
        }
    }

    // MARK: サマリー

    /// サマリー 1 枚（タイトル + 大きな数字 + 補足）
    private func statTile(title: String, value: Int, sub: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 2) {
                AnimatedNumber(value: appeared ? Double(value) : 0)
                    .font(.system(size: 22, weight: .bold, design: .rounded))
                    .monospacedDigit()
                Text("文字").font(.caption2).foregroundStyle(.secondary)
            }
            Text(sub).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var todayChars: Int { stats.charactersInLast(days: 1) }
    private var weekChars: Int { stats.charactersInLast(days: 7) }
    private var todaySessions: Int { stats.dailySeries(1).last?.sessions ?? 0 }

    // MARK: チャート

    /// 期間に応じたバー列
    private var buckets: [UsagePoint] {
        switch period {
        case .week: return stats.dailySeries(7)
        case .month: return stats.dailySeries(30)
        case .year: return stats.monthlySeries(12)
        }
    }

    /// 1 本でも入力があるか（無ければ空状態の案内を出す）
    private var hasData: Bool { buckets.contains { $0.characters > 0 } }

    private var chart: some View {
        Chart(buckets) { b in
            BarMark(
                x: .value("日付", b.date, unit: period == .year ? .month : .day),
                y: .value("文字数", animateBars ? b.characters : 0)
            )
            .foregroundStyle(Color.accentColor.gradient)
            .cornerRadius(3)
        }
        .chartYAxis {
            AxisMarks(position: .leading)
        }
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: xAxisDesiredCount)) { value in
                AxisGridLine()
                AxisValueLabel {
                    if let d = value.as(Date.self) {
                        Text(xAxisLabel(d))
                    }
                }
            }
        }
        .animation(.easeInOut(duration: 0.4), value: period)
    }

    private var xAxisDesiredCount: Int {
        switch period {
        case .week: return 7
        case .month: return 6
        case .year: return 12
        }
    }

    /// 期間ごとの X 軸ラベル（曜日 / 日 / 月）
    private func xAxisLabel(_ d: Date) -> String {
        switch period {
        case .week: return d.formatted(.dateTime.weekday(.narrow))
        case .month: return d.formatted(.dateTime.day())
        case .year: return d.formatted(.dateTime.month(.abbreviated))
        }
    }

    // MARK: 整形

    /// 累計節約時間を「X 時間 Y 分」等に整形する
    private var formattedSaved: String {
        let total = Int(stats.savedSeconds.rounded())
        if total >= 3600 {
            return "\(total / 3600) 時間 \((total % 3600) / 60) 分"
        }
        if total >= 60 {
            return "\(total / 60) 分 \(total % 60) 秒"
        }
        return "\(total) 秒"
    }

    /// 録音秒数を短く整形する（サマリー補足用）
    private func formattedMinutes(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        if total >= 3600 { return "\(total / 3600)時間\((total % 3600) / 60)分" }
        if total >= 60 { return "\(total / 60)分" }
        return "\(total)秒"
    }

    private var streakText: String {
        "\(stats.currentStreak) 日（最長 \(stats.longestStreak) 日）"
    }
}

/// カウントアップ表示用の数字（Animatable で 0→値 を滑らかに補間する）
private struct AnimatedNumber: View, Animatable {
    var value: Double
    var animatableData: Double {
        get { value }
        set { value = newValue }
    }
    var body: some View {
        Text("\(Int(value.rounded()))")
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
                Text("まだ履歴がありません。音声入力すると、ここに直近 10 件が残ります。")
                    .foregroundStyle(.secondary)
            } else {
                // 設定タブでは直近 10 件のみ表示（全 200 件はホーム画面で扱う予定＝Phase B）
                ForEach(Array(history.items.prefix(10))) { entry in
                    HistoryRow(entry: entry, copied: copiedId == entry.id) {
                        copyToClipboard(entry)
                    }
                }
                Button("履歴を消去", role: .destructive) {
                    history.clear()
                }
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }

    private func copyToClipboard(_ entry: HistoryItem) {
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
    let entry: HistoryItem
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
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
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

// MARK: - アカウント（ブラウザ経由ログイン）

private struct AccountTab: View {
    // ログイン状態・利用権は司令塔（アプリ全体で 1 つ）を購読する。
    // deep link でログインが完了／キー登録するとここの表示も自動更新される。
    @ObservedObject private var login = LoginCoordinator.shared
    /// アクティベーションキーの入力欄
    @State private var keyInput: String = ""

    /// 期限表示用の日付フォーマッタ
    private static let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .none
        return f
    }()

    /// 利用権が有効か（有効ならキー入力欄を隠す）
    private var isActive: Bool {
        if case .active = login.entitlement { return true }
        return false
    }

    /// ログイン済みか
    private var isLoggedIn: Bool {
        if case .loggedIn = login.status { return true }
        return false
    }

    var body: some View {
        Form {
            Section {
                statusRow
                actionButtons
            } header: {
                Text("アカウント")
            } footer: {
                Text("ログインすると無料体験で文字起こし（高速リアルタイム／正確性）が使えます。無料体験を使い切ったら、アクティベーションキーを登録すると続けて使えます。ログインはブラウザで行います。")
            }

            // ログイン済みのときだけライセンス（アクティベーションキー）欄を出す
            if isLoggedIn {
                Section {
                    entitlementRow
                    if !isActive {
                        activationKeyField
                    }
                } header: {
                    Text("ライセンス（アクティベーションキー）")
                } footer: {
                    Text("配布されたアクティベーションキーを入力して登録してください。一度登録するとアカウントに紐付き、別の端末でもログインすれば使えます。")
                }
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
        // キー登録が成功して有効になったら入力欄をクリアする
        .onChange(of: login.entitlement) { _, new in
            if case .active = new { keyInput = "" }
        }
    }

    /// 現在のログイン状態の表示行
    @ViewBuilder private var statusRow: some View {
        switch login.status {
        case .loggedIn:
            Label(login.accountEmail.map { "ログイン済み（\($0)）" } ?? "ログイン済み",
                  systemImage: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .waiting:
            Label("ブラウザでログインを完了してください…", systemImage: "safari")
                .foregroundStyle(.secondary)
        case .exchanging:
            Label("ログイン処理中…", systemImage: "arrow.triangle.2.circlepath")
                .foregroundStyle(.secondary)
        case .failed(let msg):
            Label(msg, systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
        case .idle:
            Label("未ログイン", systemImage: "person.crop.circle.badge.xmark")
                .foregroundStyle(.secondary)
        }
    }

    /// 状態に応じたログイン／ログアウトボタン
    @ViewBuilder private var actionButtons: some View {
        switch login.status {
        case .loggedIn:
            Button("ログアウト") { login.logout() }
        case .exchanging:
            EmptyView()  // 処理中は操作させない
        default:
            Button("ログイン") { login.beginLogin() }
                .glassProminentButton()  // 主要アクション（accent 色ガラス）
        }
    }

    /// 利用権（アクティベーションキー）の状態表示行
    @ViewBuilder private var entitlementRow: some View {
        switch login.entitlement {
        case .active(let until):
            if let until {
                Label("有効（期限: \(Self.dateFormatter.string(from: until))）",
                      systemImage: "checkmark.seal.fill")
                    .foregroundStyle(.green)
            } else {
                Label("有効", systemImage: "checkmark.seal.fill")
                    .foregroundStyle(.green)
            }
        case .free(let remaining, let quota):
            Label("無料体験中（残り \(remaining) / \(quota) 回）", systemImage: "gift.fill")
                .foregroundStyle(.blue)
        case .checking:
            Label("確認中…", systemImage: "arrow.triangle.2.circlepath")
                .foregroundStyle(.secondary)
        case .none:
            Label("無料体験を使い切りました（キーを入力してください）", systemImage: "key.slash")
                .foregroundStyle(.orange)
        case .error(let msg):
            HStack {
                Label(msg, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                Spacer()
                Button("再確認") { login.refreshEntitlement() }
            }
        case .unknown:
            Button("状態を確認") { login.refreshEntitlement() }
        }
    }

    /// アクティベーションキーの入力欄＋登録ボタン
    @ViewBuilder private var activationKeyField: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                TextField("アクティベーションキー", text: $keyInput)
                    .textFieldStyle(.roundedBorder)
                    .disableAutocorrection(true)
                Button(login.redeeming ? "登録中…" : "登録") {
                    login.redeem(code: keyInput)
                }
                .disabled(login.redeeming
                          || keyInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            if let err = login.redeemError {
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
    }
}

// MARK: - バージョン情報（自動アップデート）

/// バージョン情報タブ。現在版を表示し、Sparkle の更新確認・更新検知時の「今すぐ更新する」を出す。
/// 自動アップデート（起動時＋1 日ごと）は配布ビルドのみ有効。Sparkle 既定のダイアログはそのまま使う。
private struct AboutTab: View {
    @ObservedObject private var updater = UpdaterController.shared

    /// 現在のアプリバージョン（Info.plist の CFBundleShortVersionString）
    private var currentVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
    }

    var body: some View {
        Form {
            Section {
                LabeledContent("現在のバージョン", value: currentVersion)
                statusRow
            } header: {
                Text("バージョン情報")
            } footer: {
                Text("新しいバージョンが見つかると「今すぐ更新する」ボタンが表示されます。更新は起動時と 1 日ごとに自動で確認されます。")
            }

            Section {
                if updater.isAvailable {
                    Button("アップデートを確認") { updater.checkForUpdates() }
                    // 新バージョン検知時のみ「今すぐ更新する」を出す（押すと Sparkle の DL→インストールへ）
                    if updater.availableVersionString != nil {
                        Button("今すぐ更新する") { updater.checkForUpdates() }
                            .glassProminentButton()  // 主要アクション（accent 色ガラス）
                    }
                } else {
                    Text("このビルドでは自動アップデートは利用できません")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }

    /// 更新状態の表示行（新版あり / 最新です）
    @ViewBuilder private var statusRow: some View {
        if let version = updater.availableVersionString {
            Label("新しいバージョン \(version) が利用可能です", systemImage: "arrow.down.circle.fill")
                .foregroundStyle(.green)
        } else if updater.isAvailable {
            Label("最新です", systemImage: "checkmark.circle.fill")
                .foregroundStyle(.secondary)
        }
    }
}

// MARK: - ユーザー辞書（確定置換）

/// ユーザー辞書タブ。文字起こし・整形が終わった最終テキストに対し、貼り付け直前で
/// from→to を機械置換するルールを編集する（API を通さないので遅延ゼロ）。
private struct DictionaryTab: View {
    @ObservedObject var config: ConfigStore

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if config.replacements.isEmpty {
                // 空状態の案内（待ち/未設定をユーザーが判別できるようにする）
                VStack(spacing: 8) {
                    Image(systemName: "character.book.closed")
                        .font(.largeTitle)
                        .foregroundStyle(.secondary)
                    Text("確定置換ルールがありません")
                        .foregroundStyle(.secondary)
                    Text("「追加」で、よく誤変換される語の置き換えを登録できます。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List {
                    // $config.replacements の各行をその場で編集（変更は ConfigStore が自動保存）
                    ForEach($config.replacements) { $rule in
                        HStack(spacing: 8) {
                            Toggle("", isOn: $rule.enabled)
                                .labelsHidden()
                                .help("この行を有効/無効にする")
                            TextField("変換元", text: $rule.from)
                                .textFieldStyle(.roundedBorder)
                            Image(systemName: "arrow.right")
                                .foregroundStyle(.secondary)
                            TextField("変換先", text: $rule.to)
                                .textFieldStyle(.roundedBorder)
                            Button {
                                config.replacements.removeAll { $0.id == rule.id }
                            } label: {
                                Image(systemName: "trash")
                                    .foregroundStyle(.red)
                            }
                            .buttonStyle(.borderless)
                            .help("この行を削除")
                        }
                    }
                }
                .scrollContentBackground(.hidden)  // List の不透明背景を消してすりガラス下地を透かす
                .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
            }

            Divider()
            HStack {
                Button {
                    config.replacements.append(ReplacementRule())
                } label: {
                    Label("追加", systemImage: "plus")
                }
                Spacer()
                Text("貼り付け直前に置き換えます（部分一致・登録順）。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(12)
        }
    }
}
