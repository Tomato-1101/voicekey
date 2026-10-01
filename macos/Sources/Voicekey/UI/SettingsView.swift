//
//  SettingsView.swift
//  メインウィンドウ（ダッシュボード + 設定を 1 枚に統合）と各設定タブ
//
//  v3.1: 別ウィンドウの設定を廃止し、ホーム（ダッシュボード）と設定を 1 つの
//  MainWindowView に統合した。浮くガラス島は「サイドバーだけ」（v2.1）で、サイドバーの
//  「ダッシュボード」/「設定」でコンテンツ面を切り替える。右のコンテンツはフラット背景。
//

import ServiceManagement
import SwiftUI

/// メインウィンドウの表示モデル（StatusItemController が保持し、メニュー等から状態を差し込む）。
/// showingSettings=false でダッシュボード、true で設定モード（settingsTab が選択タブ）。
@MainActor
final class MainWindowModel: ObservableObject {
    /// 設定モードか（false=ダッシュボード）
    @Published var showingSettings: Bool { didSet { persistScreen() } }
    /// 設定モードのときの選択タブ（旧 SettingsView のタグ値を踏襲）
    @Published var settingsTab: Int { didSet { persistScreen() } }
    /// 非 nil のとき、メインウィンドウを覆ってオンボーディング（フルウィンドウ・テイクオーバー）を表示する。
    /// 完了で nil に戻し、ダッシュボードへ着地する（独立ウィンドウは廃止）。
    @Published var onboarding: OnboardingModel?

    /// 画面（ホーム／設定のどのタブ）の保存先。nil なら保存しない。
    /// UI 撮影ハーネスがタブを次々に切り替えても、ユーザーの「前回の画面」を書き換えないため。
    private let screenDefaults: UserDefaults?

    init(showingSettings: Bool = false, settingsTab: Int = 0, screenDefaults: UserDefaults? = nil) {
        self.showingSettings = showingSettings
        self.settingsTab = settingsTab
        self.screenDefaults = screenDefaults
    }

    /// 前回の画面で復元したモデルを作る（以後の画面切替も同じ保存先へ書き戻す）。
    /// アプリを再起動しても、最初に開いたウィンドウが前回見ていた画面になるようにするため。
    static func restored(from defaults: UserDefaults = .standard) -> MainWindowModel {
        let screen = MainWindowScreen.load(from: defaults, validTabs: MainWindowView.settingsTabIDs)
        return MainWindowModel(
            showingSettings: screen.showingSettings,
            settingsTab: screen.settingsTab,
            screenDefaults: defaults
        )
    }

    private func persistScreen() {
        guard let screenDefaults else { return }
        MainWindowScreen(showingSettings: showingSettings, settingsTab: settingsTab).save(to: screenDefaults)
    }
}

/// メインウィンドウの「前回の画面」（ホーム or 設定のどのタブ）の保存・復元（純ロジック）。
struct MainWindowScreen: Equatable {
    var showingSettings: Bool
    var settingsTab: Int

    static let showingSettingsKey = "mainWindow.showingSettings"
    static let settingsTabKey = "mainWindow.settingsTab"

    /// 保存値を読む。未保存ならホーム（設定タブは一般）。
    static func load(from defaults: UserDefaults, validTabs: [Int]) -> MainWindowScreen {
        let tab = defaults.object(forKey: settingsTabKey) as? Int ?? 0
        return MainWindowScreen(
            showingSettings: defaults.bool(forKey: showingSettingsKey),
            settingsTab: sanitizedTab(tab, validTabs: validTabs)
        )
    }

    func save(to defaults: UserDefaults) {
        defaults.set(showingSettings, forKey: Self.showingSettingsKey)
        defaults.set(settingsTab, forKey: Self.settingsTabKey)
    }

    /// 保存したタブがいまのサイドバーに無い（ビルド種別や OS の表示条件で消えた）なら一般タブ（0）に落とす。
    /// 無いタブのまま開くと、見出しが空でサイドバーのどれも選ばれていない画面になるため。
    static func sanitizedTab(_ tab: Int, validTabs: [Int]) -> Int {
        validTabs.contains(tab) ? tab : 0
    }
}

/// ダッシュボードと設定を統合したメインウィンドウ。
/// サイドバー（浮遊ガラス島）で「ダッシュボード」/「設定」を切り替え、設定は同じウィンドウ内で
/// タブ表示する。コンテンツ側はフラット背景（v2.1）。別ウィンドウの設定は廃止した。
struct MainWindowView: View {
    @ObservedObject var config: ConfigStore
    @ObservedObject var history: HistoryStore
    @ObservedObject var historySync: HistorySync
    @ObservedObject var stats: StatsStore
    @ObservedObject var updater: UpdaterController
    @ObservedObject var model: MainWindowModel
    /// ホームのマイクテスト用（観測しない plain 参照）。
    var controller: AppController?
    /// ホームの「セットアップガイド」カードからガイドを再表示する。
    var onShowOnboarding: () -> Void = {}
    /// アカウント行の表示（メール・ログイン状態）に使う。deep link ログインで自動更新される。
    @ObservedObject private var login = LoginCoordinator.shared

    /// ホバー中のナビ項目（非選択項目に薄いハイライトを出すため）
    @State private var hoveredNav: String?

    /// サイドバーの 1 項目（id は settingsTab のタグと一致）
    struct SettingsNavItem: Identifiable {
        let id: Int
        let title: String
        let icon: String
        /// サイドバーの見出し（同じ見出しが続く項目を 1 つのまとまりとして並べる）
        let section: String
    }

    /// いまのサイドバーに出る設定タブの id（前回タブの妥当性判定と UI 撮影ハーネスが使う）
    static var settingsTabIDs: [Int] { settingsNavItems.map(\.id) }

    /// 設定タブのナビ項目（配布ビルドでは API キーを出さない）。並び順がそのままサイドバーの表示順。
    /// ビルド種別と OS だけで決まるので static にして、ウィンドウの外（保存タブの検証など）からも引けるようにする。
    static var settingsNavItems: [SettingsNavItem] {
        var items: [SettingsNavItem] = [
            .init(id: 1, title: "録音キー 1（メイン）", icon: "1.circle", section: "音声入力"),
            .init(id: 2, title: "録音キー 2（サブ）", icon: "2.circle", section: "音声入力"),
            .init(id: 8, title: "ユーザー辞書", icon: "character.book.closed", section: "音声入力"),
        ]
        // ライブ字幕は personal 限定・macOS 26 以降でしか動かないので、使える環境でだけ出す
        if EmbeddedKeys.isPersonal, #available(macOS 26.0, *) {
            // 「翻訳して入力」も personal 限定・macOS 26 以降（Apple のオンデバイス翻訳が要る）
            items.append(.init(id: 10, title: "翻訳して入力", icon: "character.bubble", section: "音声入力"))
            items.append(.init(id: 9, title: "ライブ字幕", icon: "captions.bubble", section: "字幕と議事録"))
        }
        items.append(.init(id: 0, title: "一般", icon: "gearshape", section: "アプリ"))
        // personal（個人用最速版）は埋め込みキーで常に利用可＝ログイン/アカウントの概念が無いので
        // アカウントタブを出さない。配布/開発ビルドでは従来どおり出す。
        if !EmbeddedKeys.isPersonal {
            items.append(.init(id: 6, title: "アカウント", icon: "person.crop.circle", section: "アプリ"))
        }
        items.append(.init(id: 7, title: "バージョン情報", icon: "info.circle", section: "アプリ"))
        // 配布ビルド・personal は埋め込みキーで動くため、API キーは出さない（混乱防止）
        if !EmbeddedKeys.isDist, !EmbeddedKeys.isPersonal {
            items.append(.init(id: 5, title: "API キー", icon: "key", section: "アプリ"))
        }
        return items
    }

    var body: some View {
        // 初回セットアップ中はメインウィンドウを覆ってオンボーディングを全面表示する
        // （フルウィンドウ・テイクオーバー。自前のサイズ・背景を持つ）。
        if let ob = model.onboarding {
            OnboardingView(model: ob)
        } else {
            normalLayout
        }
    }

    /// 通常のメインウィンドウ（サイドバー島＋コンテンツ面）。
    private var normalLayout: some View {
        // レイアウト v2.1: 浮くガラス島は「サイドバーだけ」。右のコンテンツはウィンドウの
        // 背景面としてフラットに敷き、島にしない。frosted backdrop はウィンドウ全面に残す。
        HStack(spacing: 12) {
            sidebar
                .clipShape(RoundedRectangle(cornerRadius: 18))  // スクロールが角からはみ出さないように
                .glassIsland(cornerRadius: 18)
                .padding(.leading, 12)   // 島の左に下地を見せる
                .padding(.vertical, 12)  // 島の上下に下地を見せる
            contentPane                  // 右ペインはウィンドウ端まで広がるフラットな背景面
        }
        // ウィンドウを広げた分は右のコンテンツ面が伸びる（サイドバーは幅固定）。
        // 最小サイズ（760×600）は窓側の contentMinSize だけで守る。ここで minHeight: 600 を付けると、
        // タイトルバー帯を除いた 572pt に 600pt を詰めることになり、はみ出した分だけ下端が切れていた
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .glassButtons()             // 配下の Button を一括ガラス化（.plain 明示ボタンは影響なし）
        .frostedWindowBackground()  // ウィンドウ全面のすりガラス下地
    }

    // MARK: - サイドバー

    /// 左サイドバー。ダッシュボードと全設定タブを見出し付きで常に並べる（1 クリックでどこへでも行ける）。
    /// 以前は「設定」を押したときだけタブ群が出る 2 段構えで、毎回「設定」を押し直す必要があり、
    /// 「設定」と選択中タブの 2 つが同時にハイライトされて現在地も分かりにくかったため平らにした。
    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            brandHeader
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    navRow(title: "ダッシュボード", icon: "square.grid.2x2", selected: !model.showingSettings) {
                        model.showingSettings = false
                    }
                    ForEach(navSections, id: \.title) { section in
                        Text(section.title)
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 12)
                            .padding(.top, 14)
                            .padding(.bottom, 4)
                        ForEach(section.items) { item in
                            navRow(title: item.title, icon: item.icon,
                                   selected: model.showingSettings && model.settingsTab == item.id) {
                                model.settingsTab = item.id
                                model.showingSettings = true
                            }
                        }
                    }
                }
                .padding(.horizontal, 8)
                .padding(.bottom, 10)
            }
            .scrollIndicators(.never)
            // personal（個人用最速版）はログイン/アカウントが無いのでアカウント行を出さない。
            if !EmbeddedKeys.isPersonal {
                Divider().padding(.horizontal, 14).padding(.vertical, 4)
                accountRow
                    .padding(.horizontal, 8)
                    .padding(.bottom, 6)
            }
        }
        .frame(width: 216)
    }

    /// サイドバーの見出しごとのまとまり（settingsNavItems の並び順のまま、連続する同じ見出しを束ねる）
    private var navSections: [(title: String, items: [SettingsNavItem])] {
        var sections: [(title: String, items: [SettingsNavItem])] = []
        for item in Self.settingsNavItems {
            if sections.last?.title == item.section {
                sections[sections.count - 1].items.append(item)
            } else {
                sections.append((item.section, [item]))
            }
        }
        return sections
    }

    /// サイドバー先頭のブランド行（アプリアイコン＋名称）
    private var brandHeader: some View {
        HStack(spacing: 8) {
            // 外観でボーン／カーボンを切り替える（applicationIconImage は外観変化で再描画されないため）
            BrandIcon()
                .frame(width: 30, height: 30)
                .clipShape(RoundedRectangle(cornerRadius: 7))
            Text("voicekey").font(.headline)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        // 島はタイトルバー帯（信号ボタン）の下から始まるので、上は少しだけ空ける
        .padding(.top, 14)
        .padding(.bottom, 6)
    }

    /// ナビ 1 行（選択中＝アクセントのグラデピル。ホバーで薄いハイライト）
    private func navRow(title: String, icon: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.system(size: 15))
                    .frame(width: 22)
                Text(title)
                    .font(.system(size: 14))
                    .lineLimit(1)
                Spacer(minLength: 0)
            }
            // 1 行 36pt。狙わなくても当たる高さにする（旧 28pt は「押しにくい」と指摘された）
            .frame(minHeight: 36)
            .padding(.horizontal, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(navBackground(selected: selected, hovered: hoveredNav == title))
            .foregroundStyle(selected ? Color.white : Color.primary)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering in
            if hovering { hoveredNav = title }
            else if hoveredNav == title { hoveredNav = nil }
        }
        .help(title)
    }

    /// ナビ項目の背景。選択中＝アクセントのグラデピル＋白リム＋ソフトな無彩色影、ホバー＝薄いフィル、他＝透明。
    @ViewBuilder
    private func navBackground(selected: Bool, hovered: Bool) -> some View {
        if selected {
            RoundedRectangle(cornerRadius: 9)
                .fill(LinearGradient(
                    colors: [Brand.signal, Brand.signal.opacity(0.75)],
                    startPoint: .top, endPoint: .bottom
                ))
                .overlay(
                    RoundedRectangle(cornerRadius: 9)
                        .strokeBorder(LinearGradient(
                            colors: [.white.opacity(0.4), .white.opacity(0.0)],
                            startPoint: .top, endPoint: .bottom
                        ), lineWidth: 1)
                )
                .shadow(color: .black.opacity(0.18), radius: 6, x: 0, y: 2)
        } else if hovered {
            RoundedRectangle(cornerRadius: 9)
                .fill(Color.primary.opacity(0.07))
        } else {
            Color.clear
        }
    }

    /// アカウント行（アバター円＋メール。未ログインは「ログイン」。クリックで設定のアカウントタブへ）
    private var accountRow: some View {
        Button {
            model.settingsTab = 6
            model.showingSettings = true
        } label: {
            HStack(spacing: 8) {
                avatar
                VStack(alignment: .leading, spacing: 1) {
                    Text(accountPrimary)
                        .font(.system(size: 12, weight: .medium))
                        .lineLimit(1)
                    Text(accountSecondary)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 7)
            .padding(.horizontal, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("アカウント設定を開く")
    }

    /// アバター円（ログイン中はメール頭文字、未ログインは人物アイコン）
    private var avatar: some View {
        ZStack {
            Circle().fill(LinearGradient(
                colors: [Brand.signal, Brand.signal.opacity(0.7)],
                startPoint: .top, endPoint: .bottom
            ))
            if isLoggedIn, let first = login.accountEmail?.first {
                Text(String(first).uppercased())
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(.white)
            } else {
                Image(systemName: "person.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(.white)
            }
        }
        .frame(width: 26, height: 26)
    }

    /// ログイン済みか
    private var isLoggedIn: Bool {
        if case .loggedIn = login.status { return true }
        return false
    }

    /// アカウント行の主表示（メール or 「ログイン」）
    private var accountPrimary: String {
        if isLoggedIn { return login.accountEmail ?? "ログイン済み" }
        return "ログイン"
    }

    /// アカウント行の副表示
    private var accountSecondary: String {
        isLoggedIn ? "アカウント" : "クリックしてログイン"
    }

    // MARK: - コンテンツ面

    /// 右のコンテンツ面（ダッシュボード or 設定タブ）。島にはせずフラットに敷く（v2.1）。
    private var contentPane: some View {
        Group {
            if model.showingSettings {
                VStack(alignment: .leading, spacing: 0) {
                    settingsHeader
                    settingsContent(tab: model.settingsTab)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        // 行のどこを押してもトグルが切り替わり、ポップアップ・ボタンも一回り大きくする
                        // （全タブ共通。各タブ側に書かせない）
                        .toggleStyle(RowSwitchToggleStyle())
                        .controlSize(.large)
                }
                // ウィンドウを広げても項目名と操作部品が離れすぎないよう、設定の列幅は 720 で止めて中央に置く
                .frame(maxWidth: 720)
                .frame(maxWidth: .infinity)
            } else {
                HomeView(config: config, history: history, stats: stats, updater: updater,
                         controller: controller, onShowOnboarding: onShowOnboarding)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(.top, 12)  // タイトルバー帯からヘッダを逃がす
    }

    /// 設定モードの上部ヘッダ（現在タブ名）
    private var settingsHeader: some View {
        Text(Self.settingsNavItems.first { $0.id == model.settingsTab }?.title ?? "")
            .font(.title2.weight(.bold))
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 18)
            .padding(.horizontal, 20)
            .padding(.bottom, 2)
    }

    /// 選択中の設定ページ（タグは旧 SettingsView と同じ値を踏襲・既存タブ View を使い回す）
    @ViewBuilder private func settingsContent(tab: Int) -> some View {
        switch tab {
        case 0: GeneralSettingsTab(config: config, historySync: historySync)
        case 1: SlotSettingsTab(title: "録音キー 1（メイン）", slot: $config.slot1)
        case 2: SlotSettingsTab(title: "録音キー 2（サブ）", slot: $config.slot2)
        case 8: DictionaryTab(config: config)
        case 9:
            if #available(macOS 26.0, *) {
                CaptionSettingsTab(config: config, controller: controller)
            } else {
                GeneralSettingsTab(config: config, historySync: historySync)
            }
        case 10:
            if #available(macOS 26.0, *) {
                TranslateInputTab(config: config)
            } else {
                GeneralSettingsTab(config: config, historySync: historySync)
            }
        case 6: AccountTab()
        case 7: AboutTab()
        case 5: ApiKeysTab()
        default: GeneralSettingsTab(config: config, historySync: historySync)
        }
    }
}

// MARK: - 一般

private struct GeneralSettingsTab: View {
    @ObservedObject var config: ConfigStore
    @ObservedObject var historySync: HistorySync
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled
    /// 入力デバイス一覧（開いたタイミングと更新ボタンで読み直す）
    @State private var inputDevices: [AudioInputDevice] = AudioDevices.inputDevices()
    /// マイク自動検出の実行中フラグ（実行中はボタンを無効化する）
    @State private var isDetectingMic = false
    /// マイク自動検出の進捗・結果表示（nil なら非表示）
    @State private var micDetectStatus: String?
    /// 「変換しない語」の追加入力欄（保護リスト編集用）
    @State private var newProtectWord = ""
    /// 共有トークンは既存値を画面へ戻さず、保存後も即座に消す。
    @State private var syncTokenInput = ""
    @State private var syncTokenSaveMessage: String?

    // 説明文は別の行に置かず、項目名の下に小さく添える（行が倍に増えて目的の項目を探しにくかったため）。
    // 見出しで「入力 → キー → 整形 → 表示 → 履歴」の順に区切り、よく触るものほど上に置く。
    var body: some View {
        Form {
            Section("入力") {
                Picker("言語", selection: $config.language) {
                    Text("日本語").tag("ja")
                    Text("英語").tag("en")
                    Text("自動判定").tag("")
                }

                LabeledContent {
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
                } label: {
                    // 検出中・検出結果の表示（待ち時間を可視化する。無表示の待ちはバグと区別できない）
                    VStack(alignment: .leading, spacing: 3) {
                        Text("マイク")
                        if let micDetectStatus {
                            Text(micDetectStatus)
                                .font(.caption)
                                .foregroundStyle(isDetectingMic ? Brand.signal : .secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .onAppear { inputDevices = AudioDevices.inputDevices() }
            }

            Section("キー操作") {
                LabeledContent {
                    HStack(spacing: 8) {
                        HotkeyRecorderView(hotkey: $config.handsfreeKey)
                            .frame(width: 180)  // 録音キーの欄と同じ幅（行いっぱいに伸びると押せる場所が分かりにくい）
                        if !config.handsfreeKey.isEmpty {
                            Button("クリア") { config.handsfreeKey = [] }
                        }
                    }
                } label: {
                    SettingRowLabel(
                        title: "ハンズフリーキー",
                        detail: "押しながら録音キーを押すと、押しっぱなしにしなくても録音が続きます（もう一度録音キーで停止）。右⇧ などの修飾キーがおすすめです。"
                    )
                }

                LabeledContent {
                    HStack(spacing: 8) {
                        HotkeyRecorderView(hotkey: $config.repasteKey)
                            .frame(width: 180)  // 録音キーの欄と同じ幅（行いっぱいに伸びると押せる場所が分かりにくい）
                        if !config.repasteKey.isEmpty {
                            Button("クリア") { config.repasteKey = [] }
                        }
                    }
                } label: {
                    SettingRowLabel(
                        title: "最後の文字起こしを貼り付け",
                        detail: "直前に入力したテキストをもう一度貼り付けます。"
                    )
                }

                LabeledContent {
                    HStack(spacing: 6) {
                        TextField(
                            "",
                            value: $config.autoEnterDelayMs,
                            format: .number
                        )
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 70)
                        .multilineTextAlignment(.trailing)
                        Text("ms").foregroundStyle(.secondary)
                    }
                } label: {
                    SettingRowLabel(
                        title: "ダブルタップ送信の待ち時間",
                        detail: "録音キーを素早く 2 回押したとき、貼り付けてから Enter を押すまでの待ち時間です。"
                    )
                }
            }

            // 自分用ビルドは整形のモデル・指示文まで自分で選べる（何が動いているか隠さない方針）。
            // オンオフは録音キー各タブの「文章を自動で整える」トグルで切り替える。
            Section {
                Picker("整形モデル", selection: $config.formatModel) {
                    // 表示は推奨モデル（リスト先頭）に「（推奨）」を付け、tag はモデル識別子のまま
                    ForEach(TextFormatter.knownModels, id: \.self) { model in
                        Text(model == TextFormatter.knownModels[0] ? "\(model)（推奨）" : model)
                            .tag(model)
                    }
                    // 保存済みモデルがリスト外でも選択を保持して表示する
                    if !TextFormatter.knownModels.contains(config.formatModel) {
                        Text(config.formatModel).tag(config.formatModel)
                    }
                }
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("整形の指示")
                        Spacer()
                        Button("既定に戻す") {
                            config.autoFormatPrompt = TextFormatter.defaultPrompt
                        }
                    }
                    TextField("", text: $config.autoFormatPrompt, axis: .vertical)
                        .lineLimit(4...8)
                        .textFieldStyle(.roundedBorder)
                        .font(.callout)
                        // grouped Form の入力欄は既定で右寄せになり、長い指示文が読めなくなるため左寄せに戻す
                        .multilineTextAlignment(.leading)
                }
            } header: {
                Text("文章の整形")
            } footer: {
                Text("整形するかどうかは「録音キー」の各ページで切り替えます。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("数字入力") {
                // マスター。OFF なら数字の正規化を一切しない（完全パススルー）
                Toggle("数字を半角に変換", isOn: $config.numeralNormalizeEnabled)
                // 単独漢数字＋助数詞（三時→3時）。マスター OFF のときは無効表示
                Toggle("助数詞つきの漢数字も変換（三時→3時）", isOn: $config.numeralConvertCounter)
                    .disabled(!config.numeralNormalizeEnabled)

                // 変換しない語（保護リスト）: 入力欄＋追加、各行に削除ボタン
                LabeledContent {
                    HStack(spacing: 8) {
                        TextField("語を追加", text: $newProtectWord)
                            .textFieldStyle(.roundedBorder)
                            .frame(minWidth: 140)
                            .onSubmit { addProtectWord() }
                        Button("追加") { addProtectWord() }
                            .disabled(newProtectWord.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                } label: {
                    SettingRowLabel(
                        title: "変換しない語",
                        detail: "「一人」「十分」など、数字に読める普通の言葉を守ります。"
                    )
                }
                ForEach(config.numeralProtectWords, id: \.self) { word in
                    HStack {
                        Text(word)
                        Spacer()
                        Button {
                            config.numeralProtectWords.removeAll { $0 == word }
                        } label: {
                            Image(systemName: "trash").foregroundStyle(.red)
                        }
                        .buttonStyle(.borderless)
                        .help("この語を削除")
                    }
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

            Section("起動") {
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

            Section("履歴") {
                Toggle("履歴を保存", isOn: $config.historyEnabled)
                Toggle("Windows と履歴を共有", isOn: $config.historySyncEnabled)

                LabeledContent("同期サーバー URL") {
                    TextField(
                        "https://voicekey-history-sync.<subdomain>.workers.dev",
                        text: $config.historySyncURL
                    )
                    .textFieldStyle(.roundedBorder)
                }
                if !config.historySyncURL.isEmpty,
                   !HistorySync.isAllowedServerURL(config.historySyncURL) {
                    Text("https URL を入力してください（開発用は localhost / 127.0.0.1 の http も可）。")
                        .font(.caption)
                        .foregroundStyle(.red)
                }

                LabeledContent {
                    HStack(spacing: 8) {
                        SecureField("Windows から貼り付け", text: $syncTokenInput)
                            .textFieldStyle(.roundedBorder)
                        Button("保存") { saveSyncToken() }
                            .disabled(syncTokenInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        if historySync.tokenConfigured {
                            Button("削除") {
                                syncTokenSaveMessage = historySync.deleteToken()
                                    ? "共有トークンを削除しました" : "共有トークンを削除できませんでした"
                            }
                        }
                    }
                } label: {
                    SettingRowLabel(
                        title: "共有トークン",
                        detail: syncTokenSaveMessage ?? (historySync.tokenConfigured ? "登録済み" : "未登録")
                    )
                }

                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 12) {
                        Text("送信待ち \(historySync.pendingCount) 件")
                        Text("最終同期 \(lastSyncText)")
                    }
                    .foregroundStyle(.secondary)
                    if let error = historySync.errorMessage {
                        Text(error).foregroundStyle(.red)
                    }
                }
                .font(.caption)
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
        .onAppear { historySync.requestFetch() }
    }

    private var lastSyncText: String {
        guard let date = historySync.lastSyncAt else { return "--:--" }
        return date.formatted(date: .omitted, time: .shortened)
    }

    private func saveSyncToken() {
        let ok = historySync.saveToken(syncTokenInput)
        syncTokenSaveMessage = ok ? "共有トークンを保存しました" : "共有トークンを保存できませんでした"
        if ok { syncTokenInput = "" }
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

    /// 入力欄の語を保護リストへ追加する（前後空白除去・重複はスキップ）。
    private func addProtectWord() {
        let word = newProtectWord.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !word.isEmpty else { return }
        if !config.numeralProtectWords.contains(word) {
            config.numeralProtectWords.append(word)
        }
        newProtectWord = ""
    }
}

// MARK: - ホットキースロット

private struct SlotSettingsTab: View {
    let title: String
    @Binding var slot: SlotConfig

    // 説明文は別の行に置かず、項目名の下に小さく添える（一般タブと同じ作り）。
    var body: some View {
        Form {
            Section("キー") {
                LabeledContent {
                    HStack(spacing: 8) {
                        // 未割り当てのスロットは録音しない。空表示は「未割り当て」と明示する
                        HotkeyRecorderView(hotkey: $slot.hotkey, emptyLabel: "未割り当て")
                            .frame(width: 180)
                        // 捕捉を始めずに未割り当てへ戻す明示ボタン（ESC と併せて発見性を上げる）
                        if !slot.hotkey.isEmpty {
                            Button("割り当てを外す") { slot.hotkey = [] }
                        }
                    }
                } label: {
                    SettingRowLabel(
                        title: "ホットキー",
                        detail: "クリックしてキーを押すと割り当てます。ESC で割り当てなし（このキーを無効化）。"
                    )
                }

                Picker("録音のしかた", selection: $slot.mode) {
                    ForEach(HotkeyMode.allCases) { mode in
                        Text(mode.label).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
            }

            Section("文字起こし") {
                // 製品版は文字起こし 2 択（即時入力 / スタンダード）のみ。モデルは推奨固定で非選択。
                // personal（自分用）は特徴名で包まず、実プロバイダー名 + モデル名をそのまま出す。
                Picker(selection: $slot.backend) {
                    ForEach(Backend.selectableCases) { backend in
                        Text(EmbeddedKeys.isPersonal ? backend.developerLabel : backend.label).tag(backend)
                    }
                } label: {
                    // 選択中エンジンの説明。スタンダード(groq)はハンズフリー自動切替の1行も添える。
                    SettingRowLabel(
                        title: EmbeddedKeys.isPersonal ? "エンジン" : "文字起こしモード",
                        detail: Self.backendCaption(slot.backend).isEmpty ? nil : Self.backendCaption(slot.backend)
                    )
                }
                .onChange(of: slot.backend) { _, newBackend in
                    // バックエンド変更時はそのバックエンドの推奨モデルに固定で切り替える
                    slot.model = newBackend.defaultModel
                    // 整形トグルはそのモードの既定へ追従させる（即時入力=既定 OFF・
                    // スタンダード=既定 ON）。ユーザーはこの後トグルで自由に上書きできる。
                    slot.formatEnabled = newBackend.defaultFormatEnabled
                }

                // 自分用ビルドはモデルまで自分で選べる（製品版はモデル非選択で固定）。
                // 選択肢が 1 つだけのエンジン（ローカル等）は選ばせても意味が無いので出さない。
                // ただし保存済みモデルが一覧外（自由入力）のときは、何が使われているか見えるよう出す。
                let isCustomModel = !slot.backend.knownModels.contains(slot.model)
                if slot.backend.knownModels.count > 1 || isCustomModel {
                    Picker("モデル", selection: $slot.model) {
                        // 表示は推奨モデルに「（推奨）」を付け、tag（保存値）はモデル識別子のまま
                        ForEach(slot.backend.knownModels, id: \.self) { model in
                            Text(model == slot.backend.defaultModel ? "\(model)（推奨）" : model)
                                .tag(model)
                        }
                        // 一覧外のモデルも選択状態を保って表示する（一般タブの整形モデルと同じ扱い）
                        if isCustomModel {
                            Text(slot.model).tag(slot.model)
                        }
                    }
                }
            }

            Section("整形") {
                Toggle("文章を自動で整える", isOn: $slot.formatEnabled)
                // 整形 ON のときだけ「整え方」プリセットを選ばせる（削り方の強さを切り替える）。
                // 既定 standard は言いよどみだけ除去して話した内容は残す（「内容を削るのは NG」への対応）。
                // モデル/プロンプトはサーバー固定（release 方針）なので、ここではプリセットのみ選ばせる。
                if slot.formatEnabled {
                    Picker(selection: $slot.formatPresetId) {
                        ForEach(Self.formatPresets, id: \.id) { preset in
                            Text(preset.label).tag(preset.id)
                        }
                    } label: {
                        SettingRowLabel(title: "整え方", detail: Self.formatPresetCaption(slot.formatPresetId))
                    }
                    .pickerStyle(.menu)
                }
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }

    /// 整形プリセット（サーバー lib/format.ts の preset_id と一致）。id → UI ラベル。
    /// 既定は standard。配列順がそのまま Picker の表示順。
    private static let formatPresets: [(id: String, label: String)] = [
        ("standard", "標準（言いよどみだけ除去）"),
        ("punctuation", "そのまま（句読点だけ整える）"),
        ("clean", "すっきり（言い直しを整理）"),
        ("bullets", "箇条書き（リストに整理）"),
    ]

    /// 選択中の整形プリセットの一行説明（Picker 下に薄字で出す）。
    /// 「発言内容は削らない」ことが伝わる文言にする（クレーム対応の主眼）。
    private static func formatPresetCaption(_ id: String) -> String {
        switch id {
        case "punctuation":
            return "言葉は一切消さず、句読点・改行だけを整えます。"
        case "clean":
            return "言いよどみに加え、言い直し（例: 火曜いや水曜）も最終形に整理します。内容は残します。"
        case "bullets":
            return "箇条書きにできる部分をリストに整理します。各項目の内容は残します。"
        default:  // standard
            return "「えー」「あの」などの言いよどみだけを消し、話した内容はそのまま残します。"
        }
    }

    /// 選択中の文字起こしモードの説明文（Windows 版 _backend_caption_text と同一文言）。
    /// スタンダード(groq)はハンズフリー録音時に内部で高精度エンジン(ElevenLabs)へ切替する旨も添える。
    private static func backendCaption(_ backend: Backend) -> String {
        switch backend {
        // soniox / openai / azureMAI / elevenlabs も personal 限定の選択肢（2026-10-01 追加・Mac のみ）
        case .soniox:
            return "しゃべりながら文字起こしし、離した瞬間に入力します（速さ重視のおすすめ）。\n"
                + "日本語・英語・中国語の混ざった話にも強いエンジンです（鍵: SONIOX_API_KEY）。"
        case .openai:
            return "録音後に OpenAI の gpt-transcribe で文字起こしします（日本語の精度重視）。"
        case .azureMAI:
            return "録音後に Microsoft の MAI-Transcribe-2 で文字起こしします（精度が高く単価も安め・プレビュー）。\n"
                + "鍵: AZURE_SPEECH_KEY と AZURE_SPEECH_ENDPOINT。リソースは対応リージョンに作ってください（japaneast は非対応）。"
        case .elevenlabs:
            return "録音後に ElevenLabs の Scribe v2 で文字起こしします（日本語の精度重視）。\n"
                + "数字が漢数字で出やすいので、気になるときは「数字入力」の設定と併用してください。"
        // openaiLive は personal 限定の選択肢なので、この説明は personal でしか表示されない
        // （＝ピッカーにモデル名が出ている前提で書く）
        case .openaiLive:
            return "OpenAI の新しいライブ文字起こしで入力します。\n"
                + "確定はやや遅めですが（実測 0.7 秒）、固有名詞や数字に強いエンジンです。"
        // appleLocal も personal 限定の選択肢
        case .appleLocal:
            return "Mac の中だけで文字起こしします。通信もAPIキーも使わないので最速で、オフラインでも動きます。\n"
                + "初回だけ言語モデルのダウンロードが走ります（進捗は録音 HUD に出ます）。"
        case .groq:
            return "録音後にきれいな文章にして入力します（録音後に送る方式で最速）\n"
                + "ハンズフリー録音のときは、長い録音に強いエンジンへ自動で切り替わります。"
        default:
            return ""
        }
    }
}

// MARK: - 翻訳して入力

/// 文字起こし結果を訳してから貼り付ける設定（全体で 1 つ・スロット単位ではない）
@available(macOS 26.0, *)
private struct TranslateInputTab: View {
    @ObservedObject var config: ConfigStore

    var body: some View {
        Form {
            // 説明文は別の行に置かず、項目名の下に小さく添える（他の設定タブと同じ作り）
            Section {
                Toggle(isOn: $config.translateInputEnabled) {
                    SettingRowLabel(
                        title: "翻訳して入力する",
                        detail: "話した内容を翻訳してから貼り付けます（例: 日本語で話す → 英語が入力される）。"
                            + "録音キー 1・2 のどちらでも、どの文字起こしエンジンでも同じように効きます。"
                    )
                }
            }

            if config.translateInputEnabled {
                Section {
                    Picker("入力する言語", selection: $config.translateInputTarget) {
                        ForEach(DictationTranslation.targetLanguages, id: \.code) { language in
                            Text(language.label).tag(language.code)
                        }
                    }
                    Picker(selection: $config.translateInputEngine) {
                        ForEach(DictationTranslationEngine.allCases) { engine in
                            Text(engine.label).tag(engine)
                        }
                    } label: {
                        SettingRowLabel(title: "翻訳エンジン", detail: Self.engineCaption(config.translateInputEngine))
                    }
                } footer: {
                    // 二重に LLM を叩くと遅くなるので、気付けるようにここで一言添える
                    // （どちらを切るかはユーザーが決める。勝手に整形を無効化はしない）
                    Text("「文章を自動で整える」と同時に使うと、整形と翻訳で 2 回 LLM を呼びます。"
                        + "速さを優先するなら、録音キーの設定で整形をオフにしてください。"
                        + "翻訳に失敗したときは原文がそのまま入力されます（文章を失いません）。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }

    /// 選択中の翻訳エンジンの一行説明
    private static func engineCaption(_ engine: DictationTranslationEngine) -> String {
        switch engine {
        case .apple:
            return "Mac の中だけで翻訳します。無料・オフラインで動きます。\n"
                + "使う言語が未導入のときは システム設定 > 一般 > 言語と地域 > 翻訳言語 で追加してください。"
        case .groq:
            return "Groq の LLM で翻訳します。話し言葉のニュアンスに強い代わりに、通信と API キーが必要です。"
        }
    }
}

// MARK: - API キー

private struct ApiKeysTab: View {
    // 製品版で使うキーのみ表示（開発ビルドのみ表示されるタブ）。
    // ElevenLabs ＋ 裏のテキスト整形に使う Groq。Deepgram は 2026-10-01 に選択肢から外したので出さない。
    private let backends: [Backend] = [.elevenlabs, .groq]

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
                Text("ログインすると無料体験で文字起こし（即時入力／正確性）が使えます。無料体験を使い切ったら、アクティベーションキーを登録すると続けて使えます。ログインはブラウザで行います。")
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
                            // 行全体スイッチ（設定画面の共通スタイル）だと空ラベルが行幅を取るので、素のスイッチに戻す
                            Toggle("", isOn: $rule.enabled)
                                .toggleStyle(.switch)
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
