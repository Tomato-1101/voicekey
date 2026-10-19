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
        /// ページ見出しの下に出す 1 行の説明（このページで何を決めるのかを最初に言う）
        var subtitle: String = ""
    }

    /// 設定タブの id（保存値・メニュー・UI 撮影ハーネスが数値で参照するので、既存の値は変えない）。
    /// 新しいタブは既存と重ならない番号を足していく（12 = プロンプト・13 = 入力の基本。2026-10-19 追加）
    enum TabID {
        static let general = 0
        static let slot1 = 1
        static let slot2 = 2
        static let apiKeys = 5
        static let account = 6
        static let about = 7
        static let dictionary = 8
        static let caption = 9
        static let translateInput = 10
        static let meeting = 11
        static let prompt = 12
        static let inputBasics = 13
    }

    /// いまのサイドバーに出る設定タブの id（前回タブの妥当性判定と UI 撮影ハーネスが使う）
    static var settingsTabIDs: [Int] { settingsNavItems.map(\.id) }

    /// 設定タブのナビ項目（配布ビルドでは API キーを出さない）。並び順がそのままサイドバーの表示順。
    /// ビルド種別と OS だけで決まるので static にして、ウィンドウの外（保存タブの検証など）からも引けるようにする。
    ///
    /// 並べ方（2026-10-19 作り直し・ユーザー指示「項目構成が合理的じゃないので人間が理解しやすく」）:
    /// - はじめに: キーが無いと何も始まらないので API キーを最初に置く
    /// - 音声入力: 話す前提（言語・マイク）→ 録音キー 1・2 → 整える指示（プロンプト）→ 直し方（辞書・数字）→ 翻訳
    /// - 字幕と議事録: パソコンの音を相手にする機能
    /// - アプリ: 表示・音・起動・履歴など、入力の中身に関係しないもの
    static var settingsNavItems: [SettingsNavItem] {
        var items: [SettingsNavItem] = []
        // personal は GitHub で一般配布し、利用者が各自のキーを入れて使うので API キーを出す。
        // 製品版（配布ビルド）はサーバー経由でキーを持たないため出さない（混乱防止）
        if !EmbeddedKeys.isDist {
            items.append(.init(
                id: TabID.apiKeys, title: "API キー", icon: "key", section: "はじめに",
                subtitle: "使うサービスのキーを入れます。キーはこの Mac の Keychain にだけ保存します。"))
        }
        items += [
            .init(id: TabID.inputBasics, title: "入力の基本", icon: "mic", section: "音声入力",
                  subtitle: "話す言語・使うマイク・録音キーと一緒に使うキーを決めます。"),
            .init(id: TabID.slot1, title: "録音キー 1", icon: "1.circle", section: "音声入力",
                  subtitle: "メインの録音キー。押して話すと、いま使っているアプリに文字が入ります。"),
            .init(id: TabID.slot2, title: "録音キー 2", icon: "2.circle", section: "音声入力",
                  subtitle: "2 つ目の録音キー。エンジンや押し方を 1 と変えて使い分けられます。"),
            .init(id: TabID.prompt, title: "プロンプト", icon: "wand.and.stars", section: "音声入力",
                  subtitle: "文章を整えるときに AI へ渡す指示です。録音キー 1・2 で共通です。"),
            .init(id: TabID.dictionary, title: "辞書と数字", icon: "character.book.closed", section: "音声入力",
                  subtitle: "聞き間違えやすい言葉の直し方と、数字の書き方を決めます。"),
        ]
        // ライブ字幕は personal 限定・macOS 26 以降でしか動かないので、使える環境でだけ出す
        if EmbeddedKeys.isPersonal, #available(macOS 26.0, *) {
            // 「翻訳して入力」も personal 限定・macOS 26 以降（Apple のオンデバイス翻訳が要る）
            items.append(.init(
                id: TabID.translateInput, title: "翻訳して入力", icon: "character.bubble", section: "音声入力",
                subtitle: "話した内容を別の言語に訳してから入力します。"))
            items.append(.init(
                id: TabID.caption, title: "ライブ字幕", icon: "captions.bubble", section: "字幕と議事録",
                subtitle: "パソコンで流れている音声に、字幕（翻訳・文字起こし）を付けます。"))
            // Meet ボットの参加・ログイン・議事録を 1 画面で（メニューだけでは状態が見えなかった）
            items.append(.init(
                id: TabID.meeting, title: "議事録・会議", icon: "person.2.wave.2", section: "字幕と議事録",
                subtitle: "Google Meet にボットを参加させて、議事録を残します。"))
        }
        items.append(.init(
            id: TabID.general, title: "一般", icon: "gearshape", section: "アプリ",
            subtitle: "表示・音・起動・履歴など、アプリ全体の設定です。"))
        // personal（個人用最速版）は埋め込みキーで常に利用可＝ログイン/アカウントの概念が無いので
        // アカウントタブを出さない。配布/開発ビルドでは従来どおり出す。
        if !EmbeddedKeys.isPersonal {
            items.append(.init(
                id: TabID.account, title: "アカウント", icon: "person.crop.circle", section: "アプリ",
                subtitle: "ログインとライセンスの状態です。"))
        }
        items.append(.init(
            id: TabID.about, title: "バージョン情報", icon: "info.circle", section: "アプリ",
            subtitle: "いまのバージョンと、アップデートの確認です。"))
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
                                   selected: model.showingSettings && model.settingsTab == item.id,
                                   trailing: navTrailing(for: item.id)) {
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

    /// ナビ項目の右端に添える短い文字（録音キーのページには割り当て中のキーを出す）。
    /// どのページがどのキーかを、ページを開かずに見分けられるようにするため。
    private func navTrailing(for tab: Int) -> String? {
        switch tab {
        case TabID.slot1: return config.slot1.isAssigned ? config.slot1.hotkeyLabel : "未割り当て"
        case TabID.slot2: return config.slot2.isAssigned ? config.slot2.hotkeyLabel : "未割り当て"
        default: return nil
        }
    }

    /// ナビ 1 行（選択中＝アクセントのグラデピル。ホバーで薄いハイライト）
    private func navRow(
        title: String, icon: String, selected: Bool, trailing: String? = nil, action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.system(size: 15))
                    .frame(width: 22)
                Text(title)
                    .font(.system(size: 14))
                    .lineLimit(1)
                Spacer(minLength: 0)
                if let trailing {
                    Text(trailing)
                        .font(.system(size: 11, weight: .medium, design: .rounded))
                        .lineLimit(1)
                        .foregroundStyle(selected ? Color.white.opacity(0.85) : Color.secondary)
                }
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
            model.settingsTab = TabID.account
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

    /// 設定モードの上部ヘッダ（灯り色のアイコン＋ページ名＋このページで何を決めるかの 1 行）。
    /// ページ名だけだと「ここで何をするのか」が開くまで分からず、項目を上から読ませていたため説明を添える。
    private var settingsHeader: some View {
        let item = Self.settingsNavItems.first { $0.id == model.settingsTab }
        return HStack(alignment: .center, spacing: 12) {
            Image(systemName: item?.icon ?? "gearshape")
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 36, height: 36)
                .background(
                    RoundedRectangle(cornerRadius: 10)
                        .fill(LinearGradient(
                            colors: [Brand.signal, Brand.signal.opacity(0.75)],
                            startPoint: .top, endPoint: .bottom
                        ))
                )
            VStack(alignment: .leading, spacing: 2) {
                Text(item?.title ?? "")
                    .font(.title2.weight(.bold))
                if let subtitle = item?.subtitle, !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 18)
        .padding(.horizontal, 20)
        .padding(.bottom, 2)
    }

    /// 別のページへ移る（録音キーのページの「プロンプトを編集」など、ページ間の導線に使う）
    private func openTab(_ tab: Int) {
        model.settingsTab = tab
        model.showingSettings = true
    }

    /// 選択中の設定ページ（タグは旧 SettingsView と同じ値を踏襲・既存タブ View を使い回す）
    @ViewBuilder private func settingsContent(tab: Int) -> some View {
        switch tab {
        case TabID.general: GeneralSettingsTab(config: config, historySync: historySync)
        case TabID.inputBasics: InputBasicsTab(config: config)
        case TabID.slot1:
            SlotSettingsTab(slotNumber: 1, slot: $config.slot1, otherHotkey: config.slot2.hotkey,
                            formatPrompt: config.autoFormatPrompt, openTab: openTab)
        case TabID.slot2:
            SlotSettingsTab(slotNumber: 2, slot: $config.slot2, otherHotkey: config.slot1.hotkey,
                            formatPrompt: config.autoFormatPrompt, openTab: openTab)
        case TabID.prompt: PromptSettingsTab(config: config, openTab: openTab)
        case TabID.dictionary: DictionaryTab(config: config)
        case TabID.caption:
            if #available(macOS 26.0, *) {
                CaptionSettingsTab(config: config, controller: controller)
            } else {
                GeneralSettingsTab(config: config, historySync: historySync)
            }
        case TabID.translateInput:
            if #available(macOS 26.0, *) {
                TranslateInputTab(config: config)
            } else {
                GeneralSettingsTab(config: config, historySync: historySync)
            }
        case TabID.meeting:
            if #available(macOS 26.0, *) {
                MeetBotSettingsTab(config: config, controller: controller)
            } else {
                GeneralSettingsTab(config: config, historySync: historySync)
            }
        case TabID.account: AccountTab()
        case TabID.about: AboutTab()
        case TabID.apiKeys: ApiKeysTab()
        default: GeneralSettingsTab(config: config, historySync: historySync)
        }
    }
}

// MARK: - 入力の基本（言語・マイク・録音キーと一緒に使うキー）

/// 「入力の基本」タブ。旧「一般」タブの前半（言語・マイク・キー操作）をここへ移した。
/// 旧「一般」は入力の前提と、表示・起動・履歴のようなアプリ全体の設定が 1 ページに混ざっていて、
/// 目的の項目を探しにくかったため（2026-10-19 ユーザー指示「項目構成を人間が理解しやすく」）。
private struct InputBasicsTab: View {
    @ObservedObject var config: ConfigStore
    /// 入力デバイス一覧（開いたタイミングと更新ボタンで読み直す）
    @State private var inputDevices: [AudioInputDevice] = AudioDevices.inputDevices()
    /// マイク自動検出の実行中フラグ（実行中はボタンを無効化する）
    @State private var isDetectingMic = false
    /// マイク自動検出の進捗・結果表示（nil なら非表示）
    @State private var micDetectStatus: String?

    // 説明文は別の行に置かず、項目名の下に小さく添える（行が倍に増えて目的の項目を探しにくかったため）。
    // 並びは「何を話すか（言語）→ どこで拾うか（マイク）→ どう押すか（キー）」。
    var body: some View {
        Form {
            Section("話す言語とマイク") {
                Picker(selection: $config.language) {
                    Text("日本語").tag("ja")
                    Text("英語").tag("en")
                    Text("自動判定").tag("")
                } label: {
                    SettingRowLabel(
                        title: "話す言語",
                        detail: "文字起こしの精度が上がるので、決まっているなら選んでおくのがおすすめです。"
                    )
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

            Section {
                LabeledContent {
                    HStack(spacing: 8) {
                        HotkeyRecorderView(hotkey: $config.handsfreeKey)
                            .frame(width: 200)  // 録音キーの欄と同じ幅（行いっぱいに伸びると押せる場所が分かりにくい）
                        if !config.handsfreeKey.isEmpty {
                            Button("外す") { config.handsfreeKey = [] }
                        }
                    }
                } label: {
                    SettingRowLabel(
                        title: "ハンズフリーキー",
                        detail: "押しながら録音キーを押すと、押しっぱなしにしなくても録音が続きます（もう一度録音キーで停止）。右⇧ などの修飾キーがおすすめです。"
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
                        Text("ミリ秒").foregroundStyle(.secondary)
                    }
                } label: {
                    SettingRowLabel(
                        title: "2 回押しで送信するときの待ち時間",
                        detail: "録音キーを素早く 2 回押すと、入力したあとに Enter も押します。その Enter までの待ち時間です。"
                    )
                }
            } header: {
                Text("録音キーと一緒に使うキー")
            } footer: {
                Text("録音キーそのものは「録音キー 1」「録音キー 2」のページで決めます。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("そのほかのショートカット") {
                LabeledContent {
                    HStack(spacing: 8) {
                        HotkeyRecorderView(hotkey: $config.repasteKey)
                            .frame(width: 200)
                        if !config.repasteKey.isEmpty {
                            Button("外す") { config.repasteKey = [] }
                        }
                    }
                } label: {
                    SettingRowLabel(
                        title: "直前の入力をもう一度貼り付け",
                        detail: "最後に入力したテキストを、いまのカーソル位置へもう一度貼り付けます。"
                    )
                }
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

// MARK: - 一般（アプリ全体）

/// 「一般」タブ。表示・音・起動・履歴など、入力の中身に関係しないアプリ全体の設定だけを置く。
private struct GeneralSettingsTab: View {
    @ObservedObject var config: ConfigStore
    @ObservedObject var historySync: HistorySync
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled
    /// 共有トークンは既存値を画面へ戻さず、保存後も即座に消す。
    @State private var syncTokenInput = ""
    @State private var syncTokenSaveMessage: String?

    var body: some View {
        Form {
            Section("画面の表示") {
                Toggle(isOn: $config.hudAlwaysVisible) {
                    SettingRowLabel(
                        title: "待機中もピルを表示",
                        detail: "画面下の小さなピルを、録音していない間も出しておきます（フルスクリーンのアプリでは隠れます）。"
                    )
                }
                Toggle(isOn: $config.sideNotchEnabled) {
                    SettingRowLabel(
                        title: "画面左端の履歴スリットを表示",
                        detail: "クリックすると、これまでの入力の履歴を開いてコピーできます。"
                    )
                }
                Toggle(isOn: $config.dockIconAlwaysVisible) {
                    SettingRowLabel(
                        title: "Dock にいつも表示",
                        detail: "オフにすると、このウィンドウを開いている間だけ Dock に出ます。"
                    )
                }
            }

            Section("音") {
                Toggle("録音の開始・終了で音を鳴らす", isOn: $config.soundEffectsEnabled)
                Toggle("録音中はほかのアプリの音量を下げる", isOn: $config.duckMediaEnabled)
            }

            Section("起動") {
                Toggle("Mac にログインしたら自動で起動", isOn: $launchAtLogin)
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

            Section {
                Toggle(isOn: $config.historyEnabled) {
                    SettingRowLabel(
                        title: "入力の履歴を保存",
                        detail: "ダッシュボードの統計と、画面左端の履歴スリットに使います。"
                    )
                }
                Toggle("Windows 版と履歴を共有", isOn: $config.historySyncEnabled)

                // 共有の詳細（サーバー・トークン・状態）は、共有をオンにしたときだけ出す
                // （使わない人には関係の無い入力欄が 3 行並んで、ページが読みにくくなっていたため）
                if config.historySyncEnabled {
                    historySyncRows
                }
            } header: {
                Text("履歴")
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
        .onAppear { historySync.requestFetch() }
    }

    /// 履歴共有の設定行（サーバー URL・共有トークン・同期の状態）
    @ViewBuilder private var historySyncRows: some View {
        LabeledContent("同期サーバー URL") {
            TextField(
                "",
                text: $config.historySyncURL,
                prompt: Text("https://voicekey-history-sync.<subdomain>.workers.dev")
            )
            .labelsHidden()
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
                SecureField("", text: $syncTokenInput, prompt: Text("Windows から貼り付け"))
                    .labelsHidden()
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

    private var lastSyncText: String {
        guard let date = historySync.lastSyncAt else { return "--:--" }
        return date.formatted(date: .omitted, time: .shortened)
    }

    private func saveSyncToken() {
        let ok = historySync.saveToken(syncTokenInput)
        syncTokenSaveMessage = ok ? "共有トークンを保存しました" : "共有トークンを保存できませんでした"
        if ok { syncTokenInput = "" }
    }
}

// MARK: - 録音キー（スロット）

/// 録音キー 1・2 のタブ。「どのキーを・どう押して・どのエンジンで文字にし・整えるか」を上から順に決める。
private struct SlotSettingsTab: View {
    /// 1 or 2（文言と導線に使う）
    let slotNumber: Int
    @Binding var slot: SlotConfig
    /// もう一方のスロットのキー（同じキーを選んだときに警告するため）
    var otherHotkey: [String] = []
    /// 共有の整形プロンプト（「プロンプト」ページの正本 `ConfigStore.autoFormatPrompt`。ここでは状態の表示だけ）
    var formatPrompt: String = ""
    /// 別のページへ移る（「プロンプトを編集」ボタン用）
    var openTab: (Int) -> Void = { _ in }

    // 説明文は別の行に置かず、項目名の下に小さく添える（一般タブと同じ作り）。
    var body: some View {
        Form {
            Section("キー") {
                LabeledContent {
                    HStack(spacing: 8) {
                        // 未割り当てのスロットは録音しない。空表示は「未割り当て」と明示する
                        HotkeyRecorderView(hotkey: $slot.hotkey, emptyLabel: "未割り当て")
                            .frame(width: 200)
                        // 捕捉を始めずに未割り当てへ戻す明示ボタン（ESC は取り消しなので、外す手段はこれだけ）
                        if !slot.hotkey.isEmpty {
                            Button("外す") { slot.hotkey = [] }
                                .help("この録音キーの割り当てを外します（録音しなくなります）")
                        }
                    }
                } label: {
                    SettingRowLabel(
                        title: "録音キー",
                        detail: "枠をクリックしてからキーを押すと割り当てます（ESC で取り消し）。右⌘ のように修飾キー 1 つでも使えます。"
                    )
                }
                // 同じキーだと片方の録音しか始まらず、どちらが動くか分からなくなる
                if KeyToken.conflicts(slot.hotkey, otherHotkey) {
                    Label("もう一方の録音キーと同じキーです。どちらかを変えてください", systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }

                // 押し方はモデルやエンジンとは関係の無い「キーの使い方」なので、キーの行の直下に置く
                // （以前はエンジン・モデルの並びに混ざっていて「モデルの場所なのにホールド/トグル」に見えた）
                LabeledContent {
                    Picker("", selection: $slot.mode) {
                        ForEach(HotkeyMode.allCases) { mode in
                            Text(mode.label).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .fixedSize()
                } label: {
                    SettingRowLabel(title: "キーの押し方", detail: Self.modeCaption(slot.mode))
                }
            }

            Section {
                // エンジンとモデルを 1 つのポップアップで決める（提供元の見出しの下にモデルが並ぶ）。
                // 以前は「エンジン」と「モデル」の 2 段で、エンジンを変えると推奨モデルへ黙って切り替わっていた
                LabeledContent {
                    GroupedChoiceMenu(
                        groups: EngineModelCatalog.groups(current: choice),
                        selection: choiceBinding,
                        currentTitle: EngineModelCatalog.title(choice)
                    )
                } label: {
                    SettingRowLabel(
                        title: "文字起こしエンジン",
                        detail: Self.backendCaption(slot.backend).isEmpty ? nil : Self.backendCaption(slot.backend)
                    )
                }
            } header: {
                Text("文字起こし")
            } footer: {
                Text("一覧は日本語で使うときのおすすめ順です。Apple（ローカル）以外は「API キー」ページで、そのサービスのキーを入れてください。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Section("文章の整形") {
                Toggle(isOn: $slot.formatEnabled) {
                    SettingRowLabel(
                        title: "文章を自動で整える",
                        detail: "文字起こしのあと、AI（Groq）が「プロンプト」ページの指示で句読点や言いよどみを整えます。"
                            + "オフにすると、文字起こしをそのまま入力します（いちばん速い）。"
                    )
                }
                if slot.formatEnabled {
                    // 指示文は録音キー 1・2 で共通（正本は 1 つ）。ここでは何が使われるかと、編集への入口だけを出す
                    LabeledContent {
                        Button("プロンプトを編集") { openTab(MainWindowView.TabID.prompt) }
                    } label: {
                        SettingRowLabel(title: "使う指示", detail: Self.promptSummary(formatPrompt))
                    }
                    // 旧製品版（サーバー整形）だけは、サーバーへ渡す「整え方」が録音キーごとに効く。
                    // 自前のキーで整形する personal 版では効かない（指示文が全て）ので出さない
                    if !EmbeddedKeys.isPersonal {
                        Picker(selection: $slot.formatPresetId) {
                            ForEach(FormatPromptTemplates.all) { preset in
                                Text(preset.title).tag(preset.id)
                            }
                        } label: {
                            SettingRowLabel(
                                title: "整え方（ログイン時のサーバー整形）",
                                detail: FormatPromptTemplates.all.first { $0.id == slot.formatPresetId }?.summary
                            )
                        }
                        .pickerStyle(.menu)
                    }
                }
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }

    /// いまの (エンジン, モデル) の組
    private var choice: EngineModelChoice {
        EngineModelChoice(backend: slot.backend, model: slot.model)
    }

    /// ポップアップの選択をスロットへ書き戻す（エンジンとモデルを 1 回の代入で同時に変える＝保存も 1 回）
    private var choiceBinding: Binding<EngineModelChoice> {
        Binding(
            get: { EngineModelChoice(backend: slot.backend, model: slot.model) },
            set: { newValue in
                var updated = slot
                updated.backend = newValue.backend
                updated.model = newValue.model
                slot = updated
            }
        )
    }

    /// キーの押し方の説明
    private static func modeCaption(_ mode: HotkeyMode) -> String {
        switch mode {
        case .hold:
            return "押している間だけ録音し、離すと入力します。"
        case .toggle:
            return "1 回押すと録音が始まり、もう 1 回押すと止まって入力します。長く話すときに向いています。"
        }
    }

    /// 共有プロンプトの状態の一言（どのひな形のままか、自分で書き換えたか）
    private static func promptSummary(_ prompt: String) -> String {
        if prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "既定の指示（標準）を使います。録音キー 1・2 で共通です。"
        }
        if let template = FormatPromptTemplates.matching(prompt) {
            return "ひな形「\(template.title)」を使っています。録音キー 1・2 で共通です。"
        }
        return "自分で書き換えた指示（\(prompt.count) 文字）を使っています。録音キー 1・2 で共通です。"
    }

    /// 選択中の文字起こしエンジンの説明文（Windows 版 _backend_caption_text と同一文言）。
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
            return "録音後に ElevenLabs の Scribe で文字起こしします（日本語の精度重視）。\n"
                + "数字が漢数字で出やすいので、気になるときは「辞書と数字」ページの数字の設定と併用してください。"
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

// MARK: - プロンプト（整形の指示・録音キー 1・2 で共有）

/// 「プロンプト」タブ。整形（Groq）で AI に渡す指示文を、録音キー 1・2 共通で 1 か所で編集する。
///
/// 正本は `ConfigStore.autoFormatPrompt`（UserDefaults キー `autoFormatPrompt`。既定文と同じ内容は空で保存し、
/// 実行時は `TextFormatter.systemPrompt` が空なら既定文へフォールバックする）。モデルは `formatModel`。
/// 以前は「一般」タブの奥に 4〜8 行の入力欄で置かれていて、見つけにくく長い指示も読みにくかった
/// （2026-10-19 ユーザー指示「プロンプトは共有プロンプトで、プロンプトタブを追加してそこで編集」）。
private struct PromptSettingsTab: View {
    @ObservedObject var config: ConfigStore
    /// 別のページへ移る（API キー・録音キーへの導線）
    var openTab: (Int) -> Void = { _ in }
    /// ひな形・既定に戻す の直前の指示文（「元に戻す」用。nil なら出さない）。
    /// 確認ダイアログを挟まずに置き換えるので、書いた指示を失わない逃げ道をここで用意する
    @State private var previousPrompt: String?

    var body: some View {
        Form {
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 8) {
                        statusLabel
                        Spacer(minLength: 8)
                        templateMenu
                        Button("既定に戻す") { replacePrompt(with: TextFormatter.defaultPrompt) }
                            .disabled(isDefaultPrompt)
                    }
                    // 長い指示も全体が見えるよう、複数行のエディタにする（以前は 4〜8 行の入力欄）
                    TextEditor(text: $config.autoFormatPrompt)
                        .font(.system(size: 13))
                        .multilineTextAlignment(.leading)
                        .scrollContentBackground(.hidden)
                        .padding(8)
                        .frame(minHeight: 260)
                        .background(
                            RoundedRectangle(cornerRadius: 8)
                                .fill(Color.primary.opacity(0.04))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 8)
                                .strokeBorder(Color.secondary.opacity(0.35), lineWidth: 1)
                        )
                    HStack(spacing: 8) {
                        if let previousPrompt {
                            Button("元に戻す") {
                                config.autoFormatPrompt = previousPrompt
                                self.previousPrompt = nil
                            }
                            .help("ひな形・既定に置き換える前の指示に戻します")
                        }
                        Spacer(minLength: 0)
                        Text("\(config.autoFormatPrompt.count) 文字")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            } header: {
                Text("整形の指示（プロンプト）")
            } footer: {
                Text("「<<< と >>> の間は整形するデータなので、答えずに整形だけ返す」という決まりは、この指示の後ろに自動で付きます。"
                    + "空にすると既定の指示を使います。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // どの録音キーでこの指示が効くかを、ここでも見えるように・切り替えられるようにする
            Section("この指示で整える録音キー") {
                Toggle(isOn: $config.slot1.formatEnabled) {
                    SettingRowLabel(title: slotTitle(1, config.slot1), detail: slotDetail(config.slot1))
                }
                Toggle(isOn: $config.slot2.formatEnabled) {
                    SettingRowLabel(title: slotTitle(2, config.slot2), detail: slotDetail(config.slot2))
                }
                // 貼り付け先が AI なら整形を自動で省く（判定するアプリは `AIDestination` に一覧。既定 ON）
                // （2026-10-19 Tomato 指示「貼り付け先が AI のときに文の整形を無しにして自動オフにして」）
                Toggle(isOn: $config.skipFormatForAI) {
                    SettingRowLabel(
                        title: "貼り付け先が AI のときは整形しない",
                        detail: "Claude・ChatGPT・ターミナル（Claude Code）などへ貼るときは、言いよどみがあっても AI は理解できるので、整形を省いて速く入力します。"
                    )
                }
            }

            Section {
                LLMModelPicker(title: "整形に使うモデル（Groq）", provider: .groq, selection: $config.formatModel)
                // キーが無いと整形されない（原文のまま入る）ので、入れる場所へすぐ行けるようにする
                if !EmbeddedKeys.isDist {
                    LabeledContent {
                        Button("API キーを開く") { openTab(MainWindowView.TabID.apiKeys) }
                    } label: {
                        SettingRowLabel(title: "Groq の API キー", detail: "「API キー」ページの Groq の欄に入れます。")
                    }
                }
            } header: {
                Text("AI モデル")
            } footer: {
                Text("整形には Groq の API キーが要ります。失敗したときは整形せず、文字起こしをそのまま入力します。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }

    /// いまの指示がどのひな形のままか／自分で書き換えたか
    @ViewBuilder private var statusLabel: some View {
        if let template = FormatPromptTemplates.matching(config.autoFormatPrompt) {
            Label("ひな形「\(template.title)」", systemImage: "checkmark.seal")
                .font(.callout)
                .foregroundStyle(.secondary)
                .help(template.summary)
        } else {
            Label("自分で書き換えた指示", systemImage: "pencil")
                .font(.callout)
                .foregroundStyle(Brand.signal)
        }
    }

    /// ひな形の選択メニュー（選ぶと指示文がその内容に置き換わる）
    private var templateMenu: some View {
        Menu {
            ForEach(FormatPromptTemplates.all) { template in
                Button(template.title) { replacePrompt(with: template.prompt) }
            }
        } label: {
            Label("ひな形から選ぶ", systemImage: "text.badge.plus")
        }
        .fixedSize()
        .help("標準／そのまま／すっきり／箇条書き から選ぶと、指示がその内容に置き換わります（「元に戻す」で戻せます）")
    }

    /// 既定文のままか（前後の空白は無視・空欄も既定扱い＝実行時に既定文を使うため）
    private var isDefaultPrompt: Bool {
        let current = config.autoFormatPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        return current.isEmpty
            || current == TextFormatter.defaultPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 指示文を置き換える（同じ内容なら何もしない。直前の文を「元に戻す」用に覚える）
    private func replacePrompt(with text: String) {
        guard config.autoFormatPrompt != text else { return }
        previousPrompt = config.autoFormatPrompt
        config.autoFormatPrompt = text
    }

    /// 録音キー 1 行分の見出し（例: 「録音キー 1（右⌘）」）
    private func slotTitle(_ number: Int, _ slot: SlotConfig) -> String {
        "録音キー \(number)（\(slot.hotkeyLabel)）"
    }

    /// 録音キー 1 行分の説明（整形するかと、使うエンジン）
    private func slotDetail(_ slot: SlotConfig) -> String {
        let engine = EngineModelCatalog.title(EngineModelChoice(backend: slot.backend, model: slot.model))
        return slot.formatEnabled
            ? "整える（\(engine) で文字起こししたあと）"
            : "整えない（\(engine) の文字起こしをそのまま入力）"
    }
}

// MARK: - 翻訳して入力

/// 「翻訳して入力」のエンジン＋モデルの選択値（Apple はモデルを持たないので空）
private struct TranslateInputChoice: Hashable {
    let engine: DictationTranslationEngine
    let model: String
}

/// 文字起こし結果を訳してから貼り付ける設定（全体で 1 つ・スロット単位ではない）
@available(macOS 26.0, *)
private struct TranslateInputTab: View {
    @ObservedObject var config: ConfigStore
    /// Groq の実在一覧（未取得なら nil＝静的一覧だけで出す。LLMModelPicker と同じ取り方）
    @State private var groqAvailable: [String]?

    var body: some View {
        Form {
            // 説明文は別の行に置かず、項目名の下に小さく添える（他の設定タブと同じ作り）
            Section("翻訳するとき") {
                // たまに 1 回だけ訳す使い方が主なので、キーを先頭に置く（常時 ON は下のトグル）
                LabeledContent {
                    HStack(spacing: 8) {
                        HotkeyRecorderView(hotkey: $config.translateOnceKey)
                            .frame(width: 200)  // 他のキー設定の欄と同じ幅
                        if !config.translateOnceKey.isEmpty {
                            Button("外す") { config.translateOnceKey = [] }
                        }
                    }
                } label: {
                    SettingRowLabel(
                        title: "この 1 回だけ翻訳するキー",
                        detail: "録音中にこのキーを押すと（録音キーと同時でも、話している途中でも）、その回だけ翻訳して貼り付けます。"
                            + "翻訳される回は録音中の表示に「→EN」のような印が出ます。録音中にもう一度押すと取り消せます。"
                    )
                }
                // 録音キー・ハンズフリーキー・再貼り付けキーと重なると黙って効かなくなるので、ここで知らせる
                ForEach(DictationTranslation.translateOnceKeyWarnings(
                    key: config.translateOnceKey,
                    slotHotkeys: [config.slot(1).hotkey, config.slot(2).hotkey],
                    handsfreeKey: config.handsfreeKey,
                    repasteKey: config.repasteKey
                ), id: \.self) { warning in
                    Label(warning, systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
                Toggle(isOn: $config.translateInputEnabled) {
                    SettingRowLabel(
                        title: "いつも翻訳して入力する",
                        detail: "話した内容を毎回翻訳してから貼り付けます（例: 日本語で話す → 英語が入力される）。"
                            + "メニューバーの「翻訳して入力（常時）」でも切り替えられます。"
                    )
                }
            }

            // 言語とエンジンは 1 回翻訳でも常時翻訳でも使うので、常に出す
            Section {
                Picker("訳す先の言語", selection: $config.translateInputTarget) {
                    ForEach(DictationTranslation.targetLanguages, id: \.code) { language in
                        Text(language.label).tag(language.code)
                    }
                }
                // エンジンとモデルを 1 つのポップアップで決める（録音キーの文字起こしエンジンと同じ形）
                LabeledContent {
                    GroupedChoiceMenu(groups: choiceGroups, selection: choiceBinding, currentTitle: currentTitle)
                } label: {
                    SettingRowLabel(title: "翻訳エンジン", detail: engineDetail)
                }
            } header: {
                Text("翻訳のしかた")
            } footer: {
                // 二重に LLM を叩くと遅くなるので、気付けるようにここで一言添える
                // （どちらを切るかはユーザーが決める。勝手に整形を無効化はしない）
                Text("「文章を自動で整える」と同時に使うと、整形と翻訳で 2 回 LLM を呼びます。"
                    + "速さを優先するなら、録音キーの設定で整形をオフにしてください。"
                    + "翻訳に失敗したときは原文がそのまま入力されます（文章を失いません）。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    // footer は既定で 2 行目が右寄せになることがあるので、左寄せを明示する
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
        .task {
            // キャッシュで先に描き、取り直せたら差し替える（一覧 GET は無料。キー探索は非メインで行う）
            groqAvailable = LLMModelCatalog.cachedAvailable(.groq)
            await LLMModelCatalog.refreshAvailable(.groq, force: true)
            groqAvailable = LLMModelCatalog.cachedAvailable(.groq)
        }
    }

    /// いまの選択（Apple はモデルを持たないので空）
    private var choice: TranslateInputChoice {
        TranslateInputChoice(
            engine: config.translateInputEngine,
            model: config.translateInputEngine == .groq ? config.translateInputGroqModel : ""
        )
    }

    private var choiceBinding: Binding<TranslateInputChoice> {
        Binding(
            get: { choice },
            set: { newValue in
                // モデルを先に書いてからエンジンを切り替える（切り替えた瞬間に古いモデルで訳さないように）
                if newValue.engine == .groq { config.translateInputGroqModel = newValue.model }
                config.translateInputEngine = newValue.engine
            }
        )
    }

    /// ポップアップの中身（Apple は 1 項目・Groq は見出しの下にモデル）
    private var choiceGroups: [ChoiceMenuGroup<TranslateInputChoice>] {
        let groqOptions = LLMModelCatalog.options(
            .groq, current: config.translateInputGroqModel, available: groqAvailable
        ).map { ChoiceMenuOption(id: TranslateInputChoice(engine: .groq, model: $0.id), label: $0.label) }
        return [
            ChoiceMenuGroup(title: nil, options: [
                ChoiceMenuOption(id: TranslateInputChoice(engine: .apple, model: ""), label: "Apple 翻訳（端末内・無料）"),
            ]),
            ChoiceMenuGroup(title: "Groq", options: groqOptions),
        ]
    }

    /// 閉じたポップアップの表示
    private var currentTitle: String {
        switch config.translateInputEngine {
        case .apple: return "Apple 翻訳（端末内）"
        case .groq: return "Groq · \(LLMModelCatalog.displayName(.groq, id: config.translateInputGroqModel))"
        }
    }

    /// 選択中のエンジン（とモデル）の説明
    private var engineDetail: String {
        switch config.translateInputEngine {
        case .apple:
            return "Mac の中だけで翻訳します。無料・オフラインで動きます。\n"
                + "使う言語が未導入のときは システム設定 > 一般 > 言語と地域 > 翻訳言語 で追加してください。"
        case .groq:
            return "Groq の LLM で翻訳します。話し言葉のニュアンスに強い代わりに、通信と API キーが必要です。\n"
                + LLMModelCatalog.detail(.groq, id: config.translateInputGroqModel, available: groqAvailable)
        }
    }
}

// MARK: - API キー

private struct ApiKeysTab: View {
    var body: some View {
        Form {
            Section {
                Text("使う文字起こしエンジンのキーだけ入れれば動きます。キーはこの Mac の Keychain（アプリ専用の項目）に保存され、"
                    + "画面やログには表示しません。\nApple（ローカル）はキー不要で、端末内だけで文字起こしします。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Section("文字起こし") {
                ApiKeyRow(item: .soniox, title: "Soniox")
                ApiKeyRow(item: .openai, title: "OpenAI", note: "文字起こし（REST / ライブ）で共用")
                ApiKeyRow(item: .azureKey, title: "Microsoft MAI（キー）")
                ApiKeyRow(item: .azureEndpoint, title: "Microsoft MAI（エンドポイント）",
                          placeholder: "https://<リソース名>.cognitiveservices.azure.com", isSecret: false)
                ApiKeyRow(item: .elevenlabs, title: "ElevenLabs")
                ApiKeyRow(item: .groq, title: "Groq", note: "文字起こしと文章の自動整形・翻訳入力で共用")
            }
            Section("ライブ字幕の翻訳") {
                ApiKeyRow(item: .gemini, title: "Gemini", note: "未設定でも Apple 翻訳（キー不要）で動きます")
            }
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }
}

/// 1 項目分の入力行。状態（どこから読めるか）だけを出し、値そのもの（末尾も含む）は一切表示しない。
private struct ApiKeyRow: View {
    let item: ApiKeyItem
    let title: String
    var note: String? = nil
    var placeholder: String = "API キーを入力"
    /// エンドポイント（URL）は秘密ではないので通常の入力欄にする
    var isSecret: Bool = true

    @State private var input = ""
    /// 取得元。nil = 未設定。読み込み前は loaded=false で「確認中…」を出す
    @State private var source: APIKeySource?
    @State private var loaded = false
    /// 直近の保存に失敗したか（入力は残して、行の下にエラーを出す）
    @State private var saveFailed = false
    /// 取得元の読み直しの世代。保存・削除・読み直しのたびに進め、裏で走っている古い読み直しの
    /// 結果を捨てる（onAppear の読み直しが保存より後に終わると、保存前の取得元で表示を上書きするため）
    @State private var refreshGeneration = 0

    private var trimmedInput: String { APIKeyStore.sanitize(input) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title)
                    .fontWeight(.medium)
                Spacer()
                statusLabel
            }
            if let note {
                Text(note)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack {
                // グループ化フォーム内の既定スタイルは枠が描画されず
                // 入力欄と認識できないため、明示的に枠付きにする
                // grouped Form ではタイトルが左側のラベルになって欄を狭めるので、
                // ラベルは隠してプレースホルダ（prompt）として欄の中に出す
                Group {
                    if isSecret {
                        SecureField("", text: $input, prompt: Text(placeholder))
                    } else {
                        TextField("", text: $input, prompt: Text(placeholder))
                    }
                }
                .labelsHidden()
                .textFieldStyle(.roundedBorder)
                Button("保存") {
                    let value = trimmedInput
                    guard !value.isEmpty else { return }
                    // 走っている読み直しの結果はこの保存より古いので捨てる
                    refreshGeneration &+= 1
                    if Keychain.setApiKey(value, for: item) {
                        source = .app
                        loaded = true
                        saveFailed = false
                        input = ""
                    } else {
                        // 失敗を黙って入力欄を空にすると「保存できた」と誤解されるので、入力は残して知らせる
                        saveFailed = true
                    }
                }
                .disabled(trimmedInput.isEmpty)
                // 消せるのはアプリに保存した値だけ（環境変数・中央 Keychain には触らない）
                Button("削除") {
                    Keychain.deleteApiKey(for: item)
                    saveFailed = false
                    refresh()
                }
                .disabled(source != .app)
            }
            if saveFailed {
                Text("保存できませんでした（Keychain に書き込めませんでした）。もう一度お試しください")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
        // Keychain の読み出し（中央 Keychain は子プロセス起動）は body で呼ばず、表示時に一度だけ裏で行う
        .onAppear { refresh() }
    }

    @ViewBuilder private var statusLabel: some View {
        if !loaded {
            Text("確認中…").font(.caption).foregroundStyle(.secondary)
        } else if let source {
            Label(source.statusLabel, systemImage: "checkmark.circle.fill")
                .font(.caption)
                .foregroundStyle(.green)
        } else {
            Text("未設定").font(.caption).foregroundStyle(.secondary)
        }
    }

    /// 取得元を裏で調べ直す（値は受け取らない）
    private func refresh() {
        let item = item
        refreshGeneration &+= 1
        let generation = refreshGeneration
        Task {
            let found = await Task.detached { Keychain.apiKeySource(for: item) }.value
            // 読んでいる間に保存・削除・別の読み直しが入っていたら、この結果は古いので使わない
            guard generation == refreshGeneration else { return }
            source = found
            loaded = true
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
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
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
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
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

/// バージョン情報タブ。現在版を表示し、Sparkle の更新確認・更新検知時の更新ボタンを出す。
/// 自動アップデート（6 時間ごとに確認・裏でダウンロード）は配布ビルドのみ有効。
/// 準備済みなら確認なしで入れ替え＋再起動し、未準備なら Sparkle 既定のダイアログへ進む。
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
                Text("新しいバージョンは 6 時間ごとに確認し、裏でダウンロードしておきます。準備ができたら「更新して再起動」を押すとすぐ入れ替わります。押さなくても、次に voicekey を終了したときに自動で入ります。")
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            Section {
                if updater.isAvailable {
                    // 準備済み・押した後は Sparkle が確認を受け付けない（押しても無反応）ので出さない
                    if updater.readyVersion == nil, updater.pendingInstallMessage == nil {
                        Button("アップデートを確認") { updater.checkForUpdates() }
                    }
                    // 押した後は手応えだけ出して連打させない
                    if let pending = updater.pendingInstallMessage {
                        Text(pending)
                            .foregroundStyle(.secondary)
                    // 準備済みなら押すだけで入れ替え＋再起動。検知だけなら Sparkle の DL→インストールへ
                    } else if let ready = updater.readyVersion {
                        Button("v\(ready) に更新して再起動") { updater.installUpdate() }
                            .glassProminentButton()  // 主要アクション（accent 色ガラス）
                    } else if updater.availableVersionString != nil {
                        Button("今すぐ更新する") { updater.installUpdate() }
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
        if let version = updater.readyVersion {
            Label("新しいバージョン \(version) の準備ができました", systemImage: "arrow.down.circle.fill")
                .foregroundStyle(.green)
        } else if let version = updater.availableVersionString {
            Label("新しいバージョン \(version) が利用可能です", systemImage: "arrow.down.circle.fill")
                .foregroundStyle(.green)
        } else if updater.isAvailable {
            Label("最新です", systemImage: "checkmark.circle.fill")
                .foregroundStyle(.secondary)
        }
    }
}

// MARK: - 辞書と数字（確定置換・数字の書き方）

/// 「辞書と数字」タブ。文字起こし・整形が終わった最終テキストに対し、貼り付け直前で
/// from→to を機械置換するルール（API を通さないので遅延ゼロ）と、数字の書き方（旧「一般」タブの「数字入力」）を
/// 1 ページにまとめる。どちらも「入力される文字をどう直すか」の設定なので同じ場所に置く。
///
/// 追加の入力欄はいつも出しておく（以前は「追加」を押すと空の行が増える作りで、
/// 空のまま残った行が溜まり、どこに打てばよいかも分かりにくかった。HANDOFF の改善候補「辞書の常時入力欄」）。
private struct DictionaryTab: View {
    @ObservedObject var config: ConfigStore
    /// 追加欄の「聞き取られた表記」
    @State private var newFrom = ""
    /// 追加欄の「直したい表記」
    @State private var newTo = ""
    /// 「変換しない語」の追加入力欄（保護リスト編集用）
    @State private var newProtectWord = ""

    var body: some View {
        Form {
            Section {
                HStack(spacing: 8) {
                    TextField("", text: $newFrom, prompt: Text("聞き取られた表記（例: ボイスキー）"))
                        .labelsHidden()
                        .textFieldStyle(.roundedBorder)
                        .multilineTextAlignment(.leading)
                        .onSubmit { addRule() }
                    Image(systemName: "arrow.right")
                        .foregroundStyle(.secondary)
                    TextField("", text: $newTo, prompt: Text("直したい表記（例: voicekey）"))
                        .labelsHidden()
                        .textFieldStyle(.roundedBorder)
                        .multilineTextAlignment(.leading)
                        .onSubmit { addRule() }
                    Button("追加") { addRule() }
                        .disabled(newFrom.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            } header: {
                Text("置き換えを追加")
            } footer: {
                Text("文字起こし・整形が終わった文に、貼り付ける直前で当てます（部分一致・上から順に）。"
                    + "AI を通さないので待ち時間は増えません。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Section("登録済みの置き換え（\(config.replacements.count) 件）") {
                if config.replacements.isEmpty {
                    // 空状態の案内（待ち/未設定をユーザーが判別できるようにする）
                    Label("まだありません。上の欄に入れて「追加」で登録します。", systemImage: "character.book.closed")
                        .foregroundStyle(.secondary)
                } else {
                    // $config.replacements の各行をその場で編集（変更は ConfigStore が自動保存）
                    ForEach($config.replacements) { $rule in
                        HStack(spacing: 8) {
                            // 行全体スイッチ（設定画面の共通スタイル）だと空ラベルが行幅を取るので、素のスイッチに戻す
                            Toggle("", isOn: $rule.enabled)
                                .toggleStyle(.switch)
                                .labelsHidden()
                                .help("この行を有効/無効にする")
                            TextField("", text: $rule.from, prompt: Text("変換元"))
                                .labelsHidden()
                                .textFieldStyle(.roundedBorder)
                                .multilineTextAlignment(.leading)
                            Image(systemName: "arrow.right")
                                .foregroundStyle(.secondary)
                            TextField("", text: $rule.to, prompt: Text("変換先"))
                                .labelsHidden()
                                .textFieldStyle(.roundedBorder)
                                .multilineTextAlignment(.leading)
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
            }

            Section("数字の書き方") {
                // マスター。OFF なら数字の正規化を一切しない（完全パススルー）
                Toggle(isOn: $config.numeralNormalizeEnabled) {
                    SettingRowLabel(
                        title: "数字を半角で入力",
                        detail: "「十二人→12人」「千二百三十四→1234」のように、漢数字や全角の数字を半角にします。"
                    )
                }
                // 単独漢数字＋助数詞（三時→3時）。マスター OFF のときは無効表示
                Toggle("助数詞つきの漢数字も変換（三時→3時）", isOn: $config.numeralConvertCounter)
                    .disabled(!config.numeralNormalizeEnabled)

                // 変換しない語（保護リスト）: 入力欄＋追加、各行に削除ボタン
                LabeledContent {
                    HStack(spacing: 8) {
                        TextField("", text: $newProtectWord, prompt: Text("語を追加"))
                            .labelsHidden()
                            .textFieldStyle(.roundedBorder)
                            .multilineTextAlignment(.leading)
                            .frame(minWidth: 140)
                            .onSubmit { addProtectWord() }
                        Button("追加") { addProtectWord() }
                            .disabled(newProtectWord.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                } label: {
                    SettingRowLabel(
                        title: "数字にしない語",
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
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }

    /// 追加欄の内容でルールを 1 件足す（変換元が空なら何もしない。足したら欄を空にする）
    private func addRule() {
        let from = newFrom.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !from.isEmpty else { return }
        config.replacements.append(ReplacementRule(from: from, to: newTo.trimmingCharacters(in: .whitespacesAndNewlines)))
        newFrom = ""
        newTo = ""
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
