//
//  SideNotch.swift
//  画面左端のサイドノッチ（履歴スリット → クリックで履歴パネル）
//
//  方針:
//  - 常駐スリット: 画面左端・垂直中央に非アクティベーティングの細いバー（アクセント色）。
//    ホバーで少し太くなり、録音中は明るく点灯する。config.sideNotchEnabled で表示切替。
//  - クリックで履歴パネル（glass 島）が左端から出る。行クリックでコピー、下部に
//    「ホームを開く」「消去」。外側クリック or スリット再クリックで閉じる。
//  - 最重要: フォーカスを奪わない。パネルは非アクティベーティング NSPanel で makeKey せず、
//    非キーでもクリックが届くよう acceptsFirstMouse=true にする（音声入力の入力先を壊さない）。
//    描画更新はメインスレッドの軽い処理だけで、録音パイプラインには一切干渉しない。
//

import AppKit
import Combine
import SwiftUI

// MARK: - スリット本体（常駐の細いバー）

/// 画面左端に常駐する細いスリット（NSView）。
/// 非キーのパネルでもホバー拡大が効くよう、SwiftUI の .onHover ではなく
/// NSTrackingArea（.activeAlways）で入退室を検出する。描画は CALayer 1 枚で軽く行う。
@MainActor
final class SideNotchSlitView: NSView {

    /// クリック時のコールバック（履歴パネルの開閉トグル）
    var onClick: () -> Void = {}

    /// スリットの高さ（バー本体）
    static let slitHeight: CGFloat = 96
    /// 通常時の幅
    static let normalWidth: CGFloat = 2
    /// ホバー時の幅
    static let hoverWidth: CGFloat = 10

    /// アクセント色のバー（角丸は右側のみ）
    private let bar = CALayer()
    private var trackingArea: NSTrackingArea?
    private var hovering = false
    private var recording = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.masksToBounds = false           // 録音中グローがバーの外へ滲めるように
        bar.masksToBounds = false
        // 右側 2 隅だけ角丸（左端は画面の縁にぴったり付く）
        bar.maskedCorners = [.layerMaxXMinYCorner, .layerMaxXMaxYCorner]
        layer?.addSublayer(bar)
        layoutBar(animated: false)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) は未使用") }

    override func layout() {
        super.layout()
        layoutBar(animated: false)
    }

    /// ホバー / 録音状態に応じてバーの幅・色・グローを更新する（軽量）
    private func layoutBar(animated: Bool) {
        let w = hovering ? Self.hoverWidth : Self.normalWidth
        let h = Self.slitHeight
        let y = (bounds.height - h) / 2
        CATransaction.begin()
        CATransaction.setDisableActions(!animated)
        if animated { CATransaction.setAnimationDuration(0.15) }
        bar.frame = NSRect(x: 0, y: y, width: w, height: h)  // 左端に密着
        bar.cornerRadius = min(w, 4)
        bar.backgroundColor = accentCGColor()
        if recording {
            // 録音中は同色のソフトグローで「点灯」を表す
            bar.shadowColor = NSColor.controlAccentColor.cgColor
            bar.shadowOpacity = 0.9
            bar.shadowRadius = 4
            bar.shadowOffset = .zero
        } else {
            bar.shadowOpacity = 0
        }
        CATransaction.commit()
    }

    /// 状態別のアクセント色（録音中は最も明るく、ホバーで中間、通常は控えめ）
    private func accentCGColor() -> CGColor {
        let alpha: CGFloat = recording ? 1.0 : (hovering ? 0.85 : 0.55)
        return NSColor.controlAccentColor.withAlphaComponent(alpha).cgColor
    }

    /// 録音状態を反映する（AppState 連動。値が変わったときだけ再描画）
    func setRecording(_ value: Bool) {
        guard recording != value else { return }
        recording = value
        layoutBar(animated: true)
    }

    // MARK: マウス処理

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let trackingArea { removeTrackingArea(trackingArea) }
        // .activeAlways: パネルが非キーでもホバー入退室を受け取る
        let area = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self, userInfo: nil
        )
        addTrackingArea(area)
        trackingArea = area
    }

    override func mouseEntered(with event: NSEvent) {
        hovering = true
        layoutBar(animated: true)
    }

    override func mouseExited(with event: NSEvent) {
        hovering = false
        layoutBar(animated: true)
    }

    override func mouseDown(with event: NSEvent) {
        onClick()
    }

    /// 非キーのパネルでも 1 クリック目からクリックを受ける
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

// MARK: - 履歴パネルのホスティングビュー（非キーでもボタンを 1 クリックで押せるように）

/// NSHostingView のサブクラス。非アクティベーティング＝非キーのパネル上でも、
/// 履歴行や下部ボタンが 1 クリック目から反応するよう acceptsFirstMouse を返す。
@MainActor
final class FirstMouseHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

// MARK: - 履歴パネルの中身（SwiftUI）

/// サイドノッチから開く履歴リスト。HomeView の履歴行と同等（アプリアイコン・2 行省略・相対時刻）
/// だが、幅の狭いパネル向けに自己完結した簡潔なレイアウトにしている（無理な共通化はしない）。
struct SideNotchHistoryView: View {
    @ObservedObject var history: HistoryStore
    /// 「ホームを開く」= パネルを閉じてホームを開く
    let onOpenHome: () -> Void
    /// ヘッダの × でパネルを閉じる
    let onClose: () -> Void

    /// 直近にコピーした行に「コピーしました」を一時表示する
    @State private var copiedId: UUID?
    @Environment(\.colorScheme) private var colorScheme

    static let panelWidth: CGFloat = 320
    static let panelHeight: CGFloat = 440

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().opacity(0.5)
            if history.items.isEmpty {
                emptyState
            } else {
                list
            }
            Divider().opacity(0.5)
            footer
        }
        .frame(width: Self.panelWidth, height: Self.panelHeight)
        .glassIsland(cornerRadius: 18)  // 浮遊するガラス島（HUD と同系の質感）
        .padding(12)                    // 島の影が出る余白（パネルは一回り大きい）
    }

    // MARK: ヘッダ

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "clock.arrow.circlepath")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.secondary)
            Text("履歴").font(.system(size: 14, weight: .semibold))
            Spacer()
            Button(action: onClose) {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .padding(4)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("閉じる")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }

    // MARK: リスト

    private var list: some View {
        ScrollView {
            VStack(spacing: 0) {
                ForEach(Array(history.items.enumerated()), id: \.element.id) { index, entry in
                    row(entry)
                    if index < history.items.count - 1 {
                        Divider().padding(.leading, 42).opacity(0.5)
                    }
                }
            }
            .padding(.vertical, 4)
        }
    }

    /// 履歴 1 行（アプリアイコン＋テキスト 2 行＋相対時刻。クリックでコピー）
    private func row(_ entry: HistoryItem) -> some View {
        Button {
            copy(entry)
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Self.appIconView(bundleID: entry.appBundleID, size: 22)
                VStack(alignment: .leading, spacing: 3) {
                    Text(entry.text)
                        .font(.system(size: 12))
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    HStack(spacing: 6) {
                        Text(Self.relativeFormatter.localizedString(for: entry.date, relativeTo: Date()))
                            .font(.caption2).foregroundStyle(.secondary)
                        Spacer()
                        if copiedId == entry.id {
                            Label("コピーしました", systemImage: "checkmark.circle.fill")
                                .font(.caption2).foregroundStyle(.green)
                        } else {
                            Image(systemName: "doc.on.doc")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .contentShape(Rectangle())  // 行全体をクリック領域にする
        }
        .buttonStyle(.plain)
    }

    // MARK: フッタ

    private var footer: some View {
        HStack {
            Button {
                onOpenHome()
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "house")
                    Text("ホームを開く").font(.system(size: 12))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("ホーム画面を開く")

            Spacer()

            if !history.items.isEmpty {
                Button {
                    history.clear()
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "trash")
                        Text("消去").font(.system(size: 12))
                    }
                    .foregroundStyle(.red)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("履歴をすべて消去する")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }

    // MARK: 空状態

    private var emptyState: some View {
        VStack {
            Spacer()
            Text("音声入力すると、ここに履歴が残ります。\nクリックでコピーできます。")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(.horizontal, 20)
    }

    // MARK: 動作

    /// 履歴テキストをクリップボードへコピーし、その行に一時フィードバックを出す。
    /// NSPasteboard 経由なのでフォーカスを奪わない（前面アプリの入力状態を壊さない）。
    private func copy(_ entry: HistoryItem) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(entry.text, forType: .string)
        copiedId = entry.id
        Task {
            try? await Task.sleep(for: .seconds(1.5))
            if copiedId == entry.id { copiedId = nil }
        }
    }

    // MARK: 共通ヘルパ（HomeView と同方針・この画面に閉じて持つ）

    /// bundleID からアプリアイコンを描画する（解決できなければ汎用アイコン）
    @ViewBuilder
    static func appIconView(bundleID: String?, size: CGFloat) -> some View {
        if let icon = appIcon(bundleID: bundleID) {
            Image(nsImage: icon)
                .resizable()
                .frame(width: size, height: size)
                .clipShape(RoundedRectangle(cornerRadius: size * 0.22))
        } else {
            Image(systemName: "app.dashed")
                .font(.system(size: size * 0.8))
                .foregroundStyle(.secondary)
                .frame(width: size, height: size)
        }
    }

    /// bundleID から実行ファイルのアイコンを解決する（未インストール等で見つからなければ nil）
    private static func appIcon(bundleID: String?) -> NSImage? {
        guard let bundleID, !bundleID.isEmpty,
              let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID)
        else { return nil }
        return NSWorkspace.shared.icon(forFile: url.path)
    }

    /// 相対時刻フォーマッタ（「3 分前」等・日本語短縮表記）
    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.locale = Locale(identifier: "ja_JP")
        f.unitsStyle = .short
        return f
    }()
}

// MARK: - サイドノッチの常駐コントローラ

/// スリット（常駐パネル）と履歴パネルの生成・配置・状態連動を管理する。
/// StatusItemController が所有し、AppState の録音状態と config.sideNotchEnabled を橋渡しする。
@MainActor
final class SideNotchController {

    private let history: HistoryStore
    private let config: ConfigStore
    /// 「ホームを開く」で呼ぶ（StatusItemController.showHome）
    private let onOpenHome: () -> Void

    /// 常駐スリットのパネル（幅は狭いが、ホバー / クリックを受けるため mouse は透過しない）
    private var slitPanel: NSPanel?
    private var slitView: SideNotchSlitView?
    /// クリックで開く履歴パネル
    private var historyPanel: NSPanel?
    /// 履歴パネル外のクリックで閉じるためのグローバル監視（開いている間だけ張る）
    private var outsideClickMonitor: Any?

    private var cancellables: Set<AnyCancellable> = []

    /// スリットパネルのサイズ（バー本体＋ホバー拡大＋録音グローの余白を含む）
    private static let slitPanelWidth: CGFloat = 12
    private static let slitPanelHeight: CGFloat = SideNotchSlitView.slitHeight + 16

    init(history: HistoryStore, config: ConfigStore, onOpenHome: @escaping () -> Void) {
        self.history = history
        self.config = config
        self.onOpenHome = onOpenHome

        // 表示トグルの購読。@Published は購読時に現在値を即時に流すため、初期表示もここで決まる。
        config.$sideNotchEnabled
            .sink { [weak self] enabled in self?.applyEnabled(enabled) }
            .store(in: &cancellables)

        // 画面構成が変わったら位置を再計算する（解像度変更・外部ディスプレイ着脱）
        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.repositionAll() }
        }
    }

    // MARK: 録音状態の反映

    /// AppState の録音状態をスリットの点灯に反映する（StatusItemController から呼ぶ）
    func setRecording(_ recording: Bool) {
        slitView?.setRecording(recording)
    }

    // MARK: 表示切替

    /// 表示トグルの反映。ON でスリットを出し、OFF で履歴パネルごと隠す。
    private func applyEnabled(_ enabled: Bool) {
        if enabled {
            showSlit()
        } else {
            closeHistory()
            slitPanel?.orderOut(nil)
        }
    }

    /// スリットを表示する（無ければ生成）。フォーカスは奪わない。
    private func showSlit() {
        if slitPanel == nil { makeSlitPanel() }
        positionSlit()
        slitPanel?.orderFrontRegardless()
    }

    private func makeSlitPanel() {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: Self.slitPanelWidth, height: Self.slitPanelHeight),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false
        )
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]

        let view = SideNotchSlitView(
            frame: NSRect(x: 0, y: 0, width: Self.slitPanelWidth, height: Self.slitPanelHeight)
        )
        view.onClick = { [weak self] in self?.toggleHistory() }
        panel.contentView = view
        slitPanel = panel
        slitView = view
    }

    /// スリットを画面左端・垂直中央へ置く
    private func positionSlit() {
        guard let slitPanel, let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        let x = visible.minX
        let y = visible.midY - Self.slitPanelHeight / 2
        slitPanel.setFrame(
            NSRect(x: x, y: y, width: Self.slitPanelWidth, height: Self.slitPanelHeight),
            display: true
        )
    }

    // MARK: 履歴パネルの開閉

    /// スリットクリックで開閉をトグルする
    private func toggleHistory() {
        if historyPanel?.isVisible == true {
            closeHistory()
        } else {
            openHistory()
        }
    }

    /// 履歴パネルを開く（デバッグ用にも公開）。makeKey せず前面のフォーカスを奪わない。
    func openHistory() {
        guard config.sideNotchEnabled else { return }
        if historyPanel == nil { makeHistoryPanel() }
        positionHistory()
        historyPanel?.orderFrontRegardless()
        // 開いている間だけ、他アプリのクリックで閉じるグローバル監視を張る
        if outsideClickMonitor == nil {
            outsideClickMonitor = NSEvent.addGlobalMonitorForEvents(
                matching: [.leftMouseDown, .rightMouseDown]
            ) { [weak self] _ in
                MainActor.assumeIsolated { self?.closeHistory() }
            }
        }
    }

    /// 履歴パネルを閉じる（グローバル監視も外す）
    func closeHistory() {
        historyPanel?.orderOut(nil)
        if let outsideClickMonitor {
            NSEvent.removeMonitor(outsideClickMonitor)
            self.outsideClickMonitor = nil
        }
    }

    private func makeHistoryPanel() {
        // パネルはガラス島の影ぶんだけ内容より一回り大きい（padding 12 × 2）
        let width = SideNotchHistoryView.panelWidth + 24
        let height = SideNotchHistoryView.panelHeight + 24
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false
        )
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear            // 背後のデスクトップ/他アプリをガラスがサンプルできる
        panel.hasShadow = false                   // 影は glassIsland 側で描く
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]

        let view = FirstMouseHostingView(
            rootView: SideNotchHistoryView(
                history: history,
                onOpenHome: { [weak self] in
                    // ホームを開く前にパネルを閉じる（重ならないように）
                    self?.closeHistory()
                    self?.onOpenHome()
                },
                onClose: { [weak self] in self?.closeHistory() }
            )
        )
        view.frame = NSRect(x: 0, y: 0, width: width, height: height)
        panel.contentView = view
        historyPanel = panel
    }

    /// 履歴パネルをスリットの右隣・垂直中央へ置く（画面外へはみ出さないよう clamp）
    private func positionHistory() {
        guard let historyPanel, let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        let size = historyPanel.frame.size
        let x = visible.minX  // 島は padding ぶん内側に浮くので、スリットのすぐ右に見える
        let unclampedY = visible.midY - size.height / 2
        let y = min(max(unclampedY, visible.minY), visible.maxY - size.height)
        historyPanel.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: true)
    }

    // MARK: 位置の再計算

    /// 画面構成変更時にスリットと（開いていれば）履歴パネルを置き直す
    private func repositionAll() {
        if slitPanel?.isVisible == true { positionSlit() }
        if historyPanel?.isVisible == true { positionHistory() }
    }
}
