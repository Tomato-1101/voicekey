/// 字幕 HUD（non-activating NSPanel + ガラス背景）
///
/// 表示は「日本語訳が主役・英語原文は小さく下」。
///
/// 2026-08-09 のユーザー指摘「切り替えが速すぎて、読む前に・翻訳される前に次へ行ってしまう」を受けて、
/// 1 行だけを差し替える方式から**積み上げ式（ロール表示）**に変更した。
/// 確定した文は消さずに行として積み、新しい文は下に足す（古い文が上へ流れていく）。
/// 最下段だけが「ライブ行」＝いま喋っている途中の暫定訳で、確定したら確定行として積み上がる。
/// 各確定行には最低表示時間を持たせ、読む前に消えないようにしている。
import AppKit
import OSLog

/// 字幕 1 行の種類
@available(macOS 26.0, *)
enum CaptionLineKind {
    /// いま喋っている途中の行（認識途中の英語 → 暫定訳）。最下段に 1 行だけ置く。
    case live
    /// 確定した文の訳
    case confirmed
    /// 翻訳できなかった確定文（原文のみ）
    case sourceOnly
    /// 字幕以外のお知らせ（翻訳モデルの準備待ちなど）
    case notice
}

/// HUD に並ぶ 1 行の状態
@available(macOS 26.0, *)
private final class CaptionHUDLine {
    let view: CaptionLineView
    var kind: CaptionLineKind
    var japanese: String
    var source: String
    /// 画面に出た時刻（最低表示時間の判定に使う）
    let shownAt: CFAbsoluteTime
    /// この行を消してよくなるまでの秒数
    let minimumDuration: TimeInterval
    /// フェードアウト中（二重に消さないための印）
    var isRemoving = false
    /// 読み終わり時刻に自分から退場するためのタイマー
    var retireTimer: Timer?

    init(kind: CaptionLineKind, japanese: String, source: String, minimumDuration: TimeInterval) {
        self.view = CaptionLineView(frame: .zero)
        self.kind = kind
        self.japanese = japanese
        self.source = source
        self.shownAt = CFAbsoluteTimeGetCurrent()
        self.minimumDuration = minimumDuration
    }
}

/// 字幕 HUD の生成と更新
@available(macOS 26.0, *)
final class CaptionHUDController {

    /// 画面幅に対する HUD の割合（上限あり）
    private static let widthRatio: CGFloat = 0.62
    private static let maximumWidth: CGFloat = 880
    /// 文字とガラス縁の余白
    ///
    /// 「余白とかもいらない・本当に文字にくっつくように」（2026-08-09 ユーザー指示）。
    /// 角丸に文字が食い込まない最低限だけ残している。
    private static let horizontalPadding: CGFloat = 8
    private static let verticalPadding: CGFloat = 6
    /// 行と行の間隔
    private static let lineSpacing: CGFloat = 4
    /// パネルの最小幅（ホバー操作バーが収まる幅）
    ///
    /// 短い訳文では文字幅まで縮めるが、これを下回ると「⏸ 停止 ⚙ 設定」がはみ出す。
    private static let minimumWidth: CGFloat = 190
    /// 画面下端からの既定マージン（Dock を避ける）
    private static let defaultBottomMargin: CGFloat = 120
    /// 新しい字幕が来ないまま消えるまでの秒数
    private static let autoHideDelay: TimeInterval = 8.0

    /// 画面に残す確定行の数
    ///
    /// ライブ行と合わせて最大 3 段。増やすほど HUD が高くなって映像を隠すため、
    /// 「あんまり邪魔にならないぐらい」（2026-08-09 ユーザー指示）で 2 行にしている。
    private static let maxConfirmedLines = 2
    /// 最低表示時間を満たしていない行を待つときの上限行数（際限なく伸びないようにする）
    private static let hardMaxConfirmedLines = 3
    /// 確定行の最低表示時間の下限
    private static let minimumLineDuration: TimeInterval = 3.0
    /// 1 文字あたりの読む時間（最低表示時間の見積り）
    private static let readingSecondsPerCharacter: TimeInterval = 0.15
    /// 最低表示時間の上限（長文で 1 行が居座り続けないように頭を打つ）
    private static let maximumLineDuration: TimeInterval = 12.0
    /// 読み終わってから自分で退場するまでの猶予
    ///
    /// 押し出しを待つと 1 行が 10 秒以上居座って映像を覆うため、読める時間を満たしたら
    /// 少しだけ余韻を置いて自分から消える。
    private static let selfRetireGrace: TimeInterval = 1.0
    /// 行を消すときのフェード時間
    private static let lineFadeDuration: TimeInterval = 0.25

    private let logger = makeCaptionLogger("SubtitleHUD")

    private let panel: NSPanel
    private let container: HUDContainerView
    private let glass: CaptionGlassBackground
    private let rim: GlassRimView
    private let handle: HUDDragHandleView
    private let hoverBar: HUDHoverBarView
    private let resizeHandle: HUDResizeHandleView

    /// 確定した行（古い順）。新しいものほど下に出す。
    private var confirmedLines: [CaptionHUDLine] = []
    /// いま喋っている途中の行（最下段）
    private var liveLine: CaptionHUDLine?

    /// 大きさ倍率（幅とフォントに掛かる）
    private var scale: CGFloat = CaptionSettings.hudScale

    private var hideTimer: Timer?
    private var isVisible = false
    /// 自前のレイアウトでフレームを動かしている最中か（位置の保存を止めるための印）
    private var isAdjustingFrame = false

    /// マウス移動の監視（ホバー操作バーの出し入れ用）
    private var globalMouseMonitor: Any?
    private var localMouseMonitor: Any?
    private var isHovering = false

    /// ホバーで操作バーを出すか（字幕が動作中のときだけ有効にする）
    var hoverControlsEnabled: Bool = false {
        didSet {
            guard hoverControlsEnabled != oldValue else { return }
            hoverControlsEnabled ? startHoverTracking() : stopHoverTracking()
        }
    }

    /// 操作バーの「停止」が押されたとき
    var onStopRequested: (() -> Void)?
    /// 操作バーの「設定」が押されたとき（基準ビューを渡してメニューを出させる）
    var onMenuRequested: ((NSView, NSPoint) -> Void)?

    /// 原文の併記を出すか（メニューのトグルと連動）
    var showsSourceText: Bool = CaptionSettings.showSourceText {
        didSet { relayout() }
    }

    init() {
        let width = Self.preferredWidth(scale: CaptionSettings.hudScale)
        let initialFrame = NSRect(x: 0, y: 0, width: width, height: 80)

        panel = NSPanel(
            contentRect: initialFrame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        // フルスクリーンの YouTube の上にも出す。ignoresCycle で Cmd+` の巡回に混ざらないようにする。
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle, .stationary]
        panel.alphaValue = 0
        // 表示専用だが、移動用ハンドルだけはクリックを受けたいので false のままにし、
        // hitTest 側でハンドル以外を透過させる（ignoresMouseEvents=true だと掴めなくなる）。
        panel.ignoresMouseEvents = false

        container = HUDContainerView(frame: initialFrame)
        // 映像を隠しすぎないよう、字幕 HUD だけ既定より透けさせる（2026-08-09 ユーザー指示）
        glass = CaptionGlassBackground(frame: initialFrame, alpha: 0.62)
        glass.autoresizingMask = [.width, .height]
        rim = GlassRimView(frame: initialFrame)
        rim.autoresizingMask = [.width, .height]

        handle = HUDDragHandleView(frame: .zero)
        hoverBar = HUDHoverBarView(frame: .zero)
        hoverBar.isHidden = true
        resizeHandle = HUDResizeHandleView(frame: .zero)
        resizeHandle.isHidden = true

        container.addSubview(glass)
        container.addSubview(rim)
        container.addSubview(handle)
        container.addSubview(hoverBar)
        container.addSubview(resizeHandle)
        container.dragHandle = handle
        container.hoverBar = hoverBar
        container.resizeHandle = resizeHandle
        panel.contentView = container

        hoverBar.onStop = { [weak self] in self?.onStopRequested?() }
        hoverBar.onMenu = { [weak self] view in
            guard let self else { return }
            self.onMenuRequested?(view, NSPoint(x: 0, y: 0))
        }
        resizeHandle.onScaleDelta = { [weak self] delta in
            self?.applyScaleDelta(delta)
        }

        // ドラッグ後の位置を覚える
        NotificationCenter.default.addObserver(
            self, selector: #selector(panelDidMove), name: NSWindow.didMoveNotification, object: panel
        )
        // 画面構成が変わったら既定位置を取り直す（外部ディスプレイの抜き差し）
        NotificationCenter.default.addObserver(
            self, selector: #selector(screenParametersChanged),
            name: NSApplication.didChangeScreenParametersNotification, object: nil
        )
        // Space（デスクトップ／フルスクリーン）を切り替えると、collectionBehavior があっても
        // 別アプリのフルスクリーンウィンドウの下に回り込むことがある。切替のたびに最前面へ出し直す。
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(activeSpaceChanged),
            name: NSWorkspace.activeSpaceDidChangeNotification, object: nil
        )

        applyStoredPosition()
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        NSWorkspace.shared.notificationCenter.removeObserver(self)
        stopHoverTracking()
    }

    // MARK: - ホバー操作バー

    /// マウス位置の監視を始める
    ///
    /// HUD はハンドル以外のクリックを `hitTest` で落としているため NSTrackingArea は発火しない。
    /// そこでマウス移動をグローバルに監視して、フレーム内に入ったら操作バーを出す。
    /// `.mouseMoved` の監視はアクセシビリティ許可が不要（許可が要るのはキーボード系）。
    private func startHoverTracking() {
        guard globalMouseMonitor == nil else { return }
        globalMouseMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved]) { [weak self] _ in
            self?.updateHover()
        }
        // 自アプリがアクティブな間はグローバル監視に届かないのでローカルも張る
        localMouseMonitor = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved]) { [weak self] event in
            self?.updateHover()
            return event
        }
    }

    /// マウス位置の監視をやめる
    private func stopHoverTracking() {
        if let globalMouseMonitor { NSEvent.removeMonitor(globalMouseMonitor) }
        if let localMouseMonitor { NSEvent.removeMonitor(localMouseMonitor) }
        globalMouseMonitor = nil
        localMouseMonitor = nil
        setHovering(false)
    }

    /// いまカーソルが HUD の上にあるかを見て操作バーを出し入れする
    private func updateHover() {
        guard isVisible, hoverControlsEnabled else {
            setHovering(false)
            return
        }
        setHovering(panel.frame.contains(NSEvent.mouseLocation))
    }

    /// 操作バーの表示状態を切り替える
    private func setHovering(_ hovering: Bool) {
        guard hovering != isHovering else { return }
        isHovering = hovering
        hoverBar.isHidden = !hovering
        resizeHandle.isHidden = !hovering
        // 操作バーが出ている間は自動で消さない（押そうとした瞬間に消えると操作できない）
        if hovering {
            hideTimer?.invalidate()
            hideTimer = nil
        } else {
            scheduleAutoHide()
        }
        layoutOverlayControls()
    }

    // MARK: - 大きさ

    /// 倍率を掛けたフォントサイズ
    private func scaled(_ size: CGFloat) -> CGFloat {
        (size * scale).rounded()
    }

    /// ドラッグ量に応じて大きさを変える
    ///
    /// - Parameter delta: 右下ハンドルの移動量から作った増減値
    func applyScaleDelta(_ delta: CGFloat) {
        setScale(scale + delta)
    }

    /// 大きさ倍率を設定して即座に反映する
    ///
    /// - Parameter newScale: 0.7〜2.0 に丸める
    func setScale(_ newScale: CGFloat) {
        let clamped = min(max(newScale, 0.7), 2.0)
        guard abs(clamped - scale) > 0.001 else { return }
        scale = clamped
        CaptionSettings.hudScale = clamped
        // 幅・高さ・はみ出しの調整はすべて relayout が内容から決める
        relayout()
    }

    /// 操作バーとリサイズハンドルを HUD の角に置く
    ///
    /// 字幕の高さ計算には混ぜず重ねるだけにしている（バーの出入りで字幕が動かないように）。
    private func layoutOverlayControls() {
        let size = hoverBar.intrinsicContentSize
        hoverBar.frame = NSRect(
            x: panel.frame.width - size.width - 8,
            y: panel.frame.height - size.height - 6,
            width: size.width,
            height: size.height
        )
        let handleSize: CGFloat = 20
        handle.frame = NSRect(x: 4, y: panel.frame.height - handleSize - 4, width: handleSize, height: handleSize)
        let resizeSize: CGFloat = 16
        resizeHandle.frame = NSRect(
            x: panel.frame.width - resizeSize - 3, y: 3, width: resizeSize, height: resizeSize
        )
    }

    // MARK: - 表示更新

    /// 認識途中のテキストを最下段のライブ行に薄く出す
    ///
    /// - Parameter text: ボラタイルな認識結果
    func showListening(_ text: String) {
        guard !text.isEmpty else { return }
        // 暫定訳が出ている間は生の英語で上書きしない（訳→原文→訳 とちらつくため）
        if let live = liveLine, !live.japanese.isEmpty { return }
        updateLiveLine(japanese: "", source: text)
    }

    /// 原文が確定した（翻訳待ち）
    ///
    /// 訳が届くまでの数十 ms を空白で待たせないために、ライブ行へ原文を出しておく。
    func showPendingSource(_ text: String) {
        updateLiveLine(japanese: "", source: text)
    }

    /// 訳文が届いた
    ///
    /// - Parameters:
    ///   - source: 原文
    ///   - japanese: 訳文
    ///   - isPartial: 認識途中の暫定訳なら true（ライブ行を更新するだけ）
    func showTranslation(source: String, japanese: String, isPartial: Bool = false) {
        if isPartial {
            updateLiveLine(japanese: japanese, source: source)
        } else {
            // 確定した文は行として積み上げる。ライブ行は役目を終えるので畳む。
            appendConfirmedLine(kind: .confirmed, japanese: japanese, source: source)
        }
    }

    /// 翻訳できないので原文のみ確定行として積む
    func showSourceOnly(_ text: String) {
        appendConfirmedLine(kind: .sourceOnly, japanese: "", source: text)
    }

    /// 字幕以外のお知らせを出す
    ///
    /// 翻訳モデルのダウンロード待ちなど「いま何をしていて字幕が出ないのか」を伝える。
    /// 無言で何も出ないのが一番困るので、待ちは必ず画面に出す。
    ///
    /// - Parameter text: 表示するお知らせ
    func showNotice(_ text: String) {
        removeAllLines()
        let line = CaptionHUDLine(kind: .notice, japanese: text, source: "", minimumDuration: Self.minimumLineDuration)
        container.addSubview(line.view)
        confirmedLines = [line]
        refresh()
    }

    /// 字幕を消す
    func clear() {
        hideTimer?.invalidate()
        hideTimer = nil
        fadeOut()
    }

    /// 位置と大きさを既定に戻す
    ///
    /// HUD をディスプレイ外へ動かした・小さくしすぎた、のどちらからも復帰できるようにする。
    func resetPosition() {
        CaptionSettings.hudAnchor = nil
        CaptionSettings.hudScale = 1.0
        scale = 1.0
        applyStoredPosition()
    }

    // MARK: - 行の管理

    /// ライブ行（最下段）を作る／更新する
    ///
    /// ここだけは「常に最新で上書き」でよい（部分訳の latest-wins はライブ行の中に閉じる）。
    private func updateLiveLine(japanese: String, source: String) {
        // 確定した直後に同じ発話の途中結果が届くことがあり、そのままだと同じ文が
        // 確定行とライブ行に 2 行並んで見える。直前の確定行と同じ内容なら出さない。
        if let last = confirmedLines.last(where: { !$0.isRemoving }) {
            if !japanese.isEmpty, japanese == last.japanese { return }
            if japanese.isEmpty, !source.isEmpty, source == last.source { return }
        }
        if let live = liveLine {
            live.japanese = japanese
            live.source = source
        } else {
            let line = CaptionHUDLine(kind: .live, japanese: japanese, source: source, minimumDuration: 0)
            container.addSubview(line.view)
            liveLine = line
        }
        refresh()
    }

    /// 確定行を積む
    ///
    /// 新しい行は下に足す。溢れた古い行は、最低表示時間を満たしていれば消す。
    private func appendConfirmedLine(kind: CaptionLineKind, japanese: String, source: String) {
        // お知らせだけが出ている状態から字幕に戻るときは、お知らせを片付ける
        confirmedLines.removeAll { line in
            guard line.kind == .notice else { return false }
            line.view.removeFromSuperview()
            return true
        }
        // この文のライブ行は確定行に置き換わるので畳む
        if let live = liveLine {
            live.view.removeFromSuperview()
            liveLine = nil
        }

        let text = japanese.isEmpty ? source : japanese
        let line = CaptionHUDLine(
            kind: kind,
            japanese: japanese,
            source: source,
            minimumDuration: Self.minimumDuration(for: text)
        )
        container.addSubview(line.view)
        confirmedLines.append(line)
        scheduleSelfRetire(line)
        evictOverflowingLines()
        refresh()
    }

    /// 読み終わる頃に自分から退場させる
    ///
    /// 押し出されるまで残すと 1 行が 10 秒以上映像を覆う。読める時間＋余韻で消す。
    private func scheduleSelfRetire(_ line: CaptionHUDLine) {
        let delay = line.minimumDuration + Self.selfRetireGrace
        line.retireTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self, weak line] _ in
            guard let self, let line, !line.isRemoving else { return }
            let shown = CFAbsoluteTimeGetCurrent() - line.shownAt
            self.logger.notice(
                """
                字幕行を消しました 表示=\(String(format: "%.2f", shown), privacy: .public)s \
                最低=\(String(format: "%.2f", line.minimumDuration), privacy: .public)s \
                下回り=no 理由=読了
                """
            )
            self.fadeOutAndRemove(line)
        }
    }

    /// 読むのに要る時間の見積り
    ///
    /// 短い相槌でも 3 秒は残し、長い文はその分だけ長く残す。
    /// 上限を付けているのは、長文 1 行が居座って新しい行が積めなくなるのを防ぐため。
    private static func minimumDuration(for text: String) -> TimeInterval {
        let estimated = Double(text.count) * readingSecondsPerCharacter
        return min(max(minimumLineDuration, estimated), maximumLineDuration)
    }

    /// 行数の上限を超えた分を消す
    ///
    /// 最低表示時間に満たない行は 1 行だけ上限を超えて残す（読む前に消さないため）。
    private func evictOverflowingLines() {
        while true {
            let active = confirmedLines.filter { !$0.isRemoving }
            guard active.count > Self.maxConfirmedLines, let oldest = active.first else { return }
            let shown = CFAbsoluteTimeGetCurrent() - oldest.shownAt
            let metMinimum = shown >= oldest.minimumDuration
            // まだ読む時間が残っている行は、上限を 1 行だけ超えて待つ
            if !metMinimum, active.count <= Self.hardMaxConfirmedLines { return }
            logger.notice(
                """
                字幕行を消しました 表示=\(String(format: "%.2f", shown), privacy: .public)s \
                最低=\(String(format: "%.2f", oldest.minimumDuration), privacy: .public)s \
                下回り=\(metMinimum ? "no" : "yes", privacy: .public) 理由=押し出し
                """
            )
            fadeOutAndRemove(oldest)
        }
    }

    /// 1 行だけフェードアウトさせて取り除く
    ///
    /// フェード中も場所を空けない（先に詰めると読んでいる行が飛び跳ねるため）。
    private func fadeOutAndRemove(_ line: CaptionHUDLine) {
        line.isRemoving = true
        line.retireTimer?.invalidate()
        line.retireTimer = nil
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = Self.lineFadeDuration
            line.view.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            guard let self else { return }
            line.view.removeFromSuperview()
            self.confirmedLines.removeAll { $0 === line }
            self.relayout()
        })
    }

    /// 全部の行を即座に取り除く
    private func removeAllLines() {
        for line in confirmedLines {
            line.retireTimer?.invalidate()
            line.retireTimer = nil
            line.view.removeFromSuperview()
        }
        confirmedLines.removeAll()
        liveLine?.view.removeFromSuperview()
        liveLine = nil
    }

    // MARK: - レイアウト

    /// 表示内容とレイアウトを更新し、フェードインさせる
    private func refresh() {
        relayout()
        fadeIn()
        scheduleAutoHide()
    }

    /// 下から順に並ぶ行（最後の要素が最下段）
    private var orderedLines: [CaptionHUDLine] {
        confirmedLines + (liveLine.map { [$0] } ?? [])
    }

    /// 表示中のテキストの実寸にパネルを合わせ、各行を配置する
    ///
    /// - 幅は「いちばん広い行の文字幅」に合わせて縮める（タイトフィット）。
    ///   ただし倍率から決まる上限幅を超えない＝長文はパネルを広げずに**折り返す**。
    /// - 高さは内容ぶんだけ。**下端を固定して上へ伸ばす**（字幕が下から積み上がる
    ///   見え方になり、行数が変わっても読んでいる行が動かない）。
    /// - 幅が変わっても字幕が横へ飛ばないよう、**下辺の中心を固定**して伸縮させる。
    private func relayout() {
        let maximumContentWidth = Self.preferredWidth(scale: scale) - Self.horizontalPadding * 2
        let lines = orderedLines

        // いちばん広い行に合わせる。長い行は上限で頭打ちになり、そこで折り返る。
        var contentWidth: CGFloat = 0
        for line in lines {
            contentWidth = max(contentWidth, line.view.naturalWidth(
                japanese: line.japanese,
                source: line.source,
                kind: line.kind,
                japaneseFontSize: scaled(Self.japaneseFontSize(for: line)),
                sourceFontSize: scaled(13),
                showsSource: showsSourceText
            ))
        }
        contentWidth = min(contentWidth, maximumContentWidth)
        contentWidth = max(contentWidth, Self.minimumWidth - Self.horizontalPadding * 2)
        let width = contentWidth + Self.horizontalPadding * 2

        // 折り返した結果の高さを測る
        var heights: [CGFloat] = []
        for line in lines {
            heights.append(
                line.view.apply(
                    japanese: line.japanese,
                    source: line.source,
                    kind: line.kind,
                    width: contentWidth,
                    japaneseFontSize: scaled(Self.japaneseFontSize(for: line)),
                    sourceFontSize: scaled(13),
                    showsSource: showsSourceText
                )
            )
        }

        var totalHeight = Self.verticalPadding * 2 + heights.reduce(0, +)
        if lines.count > 1 { totalHeight += Self.lineSpacing * CGFloat(lines.count - 1) }

        let bottom = panel.frame.minY
        let centerX = panel.frame.midX
        let size = NSSize(width: width, height: totalHeight)
        setPanelFrame(
            NSRect(origin: Self.clampedOrigin(CGPoint(x: centerX - width / 2, y: bottom), size: size), size: size)
        )

        // 最下段（＝配列の末尾）から上へ積む
        var y = Self.verticalPadding
        for index in stride(from: lines.count - 1, through: 0, by: -1) {
            let line = lines[index]
            line.view.frame = NSRect(x: Self.horizontalPadding, y: y, width: contentWidth, height: heights[index])
            // 古い行ほど少し淡くして、いま読む行が分かるようにする
            if !line.isRemoving {
                line.view.alphaValue = Self.opacity(distanceFromBottom: lines.count - 1 - index)
            }
            y += heights[index] + Self.lineSpacing
        }

        layoutOverlayControls()
        container.needsDisplay = true
    }

    /// 行の種類ごとの訳文フォントサイズ
    private static func japaneseFontSize(for line: CaptionHUDLine) -> CGFloat {
        switch line.kind {
        case .live: return line.japanese.isEmpty ? 20 : 26
        case .confirmed: return 26
        case .sourceOnly, .notice: return 22
        }
    }

    /// 下から数えた位置に応じた濃さ
    private static func opacity(distanceFromBottom: Int) -> CGFloat {
        switch distanceFromBottom {
        case 0: return 1.0
        case 1: return 0.82
        case 2: return 0.64
        default: return 0.5
        }
    }

    /// 保存位置（無ければ既定位置）へ動かす
    ///
    /// 覚えているのは「下辺の中心」なので、そこへ現在の幅を割り付ける。
    private func applyStoredPosition() {
        guard let screen = NSScreen.main else { return }
        var frame = panel.frame
        let visible = screen.visibleFrame
        let anchor: CGPoint
        if let stored = CaptionSettings.hudAnchor, isAnchorOnAnyScreen(stored) {
            anchor = stored
        } else {
            anchor = CGPoint(x: visible.midX, y: visible.minY + Self.defaultBottomMargin)
        }
        frame.origin = CGPoint(x: anchor.x - frame.width / 2, y: anchor.y)
        setPanelFrame(frame)
        relayout()
    }

    /// 保存した基準点がいずれかの画面に残っているか（外部ディスプレイを外した後の迷子対策）
    private func isAnchorOnAnyScreen(_ anchor: CGPoint) -> Bool {
        NSScreen.screens.contains { $0.frame.contains(anchor) }
    }

    /// 自前のレイアウトでパネルを動かす
    ///
    /// 幅の伸縮で中心を保つために origin も動かすため、その移動は「ユーザーが動かした」
    /// として保存しない（保存すると幅が変わるたびに記憶位置がずれていく）。
    private func setPanelFrame(_ frame: NSRect) {
        isAdjustingFrame = true
        panel.setFrame(frame, display: true)
        isAdjustingFrame = false
    }

    @objc private func panelDidMove() {
        guard !isAdjustingFrame else { return }
        CaptionSettings.hudAnchor = CGPoint(x: panel.frame.midX, y: panel.frame.minY)
    }

    @objc private func screenParametersChanged() {
        applyStoredPosition()
    }

    /// Space が切り替わったら最前面へ出し直す
    @objc private func activeSpaceChanged() {
        guard isVisible else { return }
        panel.orderFrontRegardless()
    }

    /// 一定時間新しい字幕が来なければ消す
    private func scheduleAutoHide() {
        hideTimer?.invalidate()
        hideTimer = Timer.scheduledTimer(withTimeInterval: Self.autoHideDelay, repeats: false) { [weak self] _ in
            self?.fadeOut()
        }
    }

    private func fadeIn() {
        // 字幕が更新されるたびに最前面へ出し直す。フルスクリーン動画や Space 切替の直後に
        // 裏へ回ったまま「時々表示されない」状態になるのを塞ぐ（呼び出しは軽い）。
        panel.orderFrontRegardless()
        guard !isVisible else { return }
        isVisible = true
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.18
            panel.animator().alphaValue = 1.0
        }
    }

    private func fadeOut() {
        guard isVisible else {
            panel.alphaValue = 0
            removeAllLines()
            return
        }
        isVisible = false
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.35
            panel.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            guard let self, !self.isVisible else { return }
            self.panel.orderOut(nil)
            // 次に喋り出したとき、前の話の行が残っていると混乱するので片付ける
            self.removeAllLines()
        })
    }

    /// 画面幅と倍率から HUD 幅を決める
    private static func preferredWidth(scale: CGFloat = CaptionSettings.hudScale) -> CGFloat {
        let screenWidth = NSScreen.main?.visibleFrame.width ?? 1280
        let base = min(screenWidth * widthRatio, maximumWidth)
        // 画面からはみ出さないように上限を掛ける
        return min(base * scale, screenWidth - 40)
    }

    /// 画面からはみ出さない左下座標へ丸める
    ///
    /// - Parameters:
    ///   - origin: 希望する左下座標
    ///   - size: そのときの HUD のサイズ
    private static func clampedOrigin(_ origin: CGPoint, size: NSSize) -> CGPoint {
        let rect = NSRect(origin: origin, size: size)
        // いま重なっている画面を基準にする（外部ディスプレイ上でも正しく収める）
        let screen = NSScreen.screens.first { $0.frame.intersects(rect) } ?? NSScreen.main
        guard let visible = screen?.visibleFrame else { return origin }
        // 画面より HUD が大きい場合は左下に寄せる（max で下限を優先）
        return CGPoint(
            x: min(max(origin.x, visible.minX), max(visible.minX, visible.maxX - size.width)),
            y: min(max(origin.y, visible.minY), max(visible.minY, visible.maxY - size.height))
        )
    }
}

/// 字幕 1 行を描くビュー（訳文＋小さい原文）
@available(macOS 26.0, *)
final class CaptionLineView: NSView {

    private let japaneseLabel: NSTextField
    private let sourceLabel: NSTextField
    /// 訳文と原文の間隔（余白は最小限にする方針）
    private static let gap: CGFloat = 2

    override init(frame frameRect: NSRect) {
        japaneseLabel = Self.makeLabel()
        sourceLabel = Self.makeLabel()
        super.init(frame: frameRect)
        addSubview(japaneseLabel)
        addSubview(sourceLabel)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) は使わない") }

    /// 折り返さずに書いた場合の文字幅を返す
    ///
    /// パネルを文字にぴったり合わせるために使う。`intrinsicContentSize` は
    /// `preferredMaxLayoutWidth` をそのまま幅として返すので、実寸の測定には使えない。
    func naturalWidth(
        japanese: String,
        source: String,
        kind: CaptionLineKind,
        japaneseFontSize: CGFloat,
        sourceFontSize: CGFloat,
        showsSource: Bool
    ) -> CGFloat {
        let primary = japanese.isEmpty ? source : japanese
        let hasTranslation = !japanese.isEmpty
        var width = Self.textWidth(
            primary, font: Self.primaryFont(size: japaneseFontSize, kind: kind, hasTranslation: hasTranslation)
        )
        if showsSource, hasTranslation, !source.isEmpty {
            width = max(width, Self.textWidth(source, font: .systemFont(ofSize: sourceFontSize, weight: .regular)))
        }
        return width
    }

    /// 1 行に収めた場合の描画幅
    private static func textWidth(_ text: String, font: NSFont) -> CGFloat {
        guard !text.isEmpty else { return 0 }
        return ceil((text as NSString).size(withAttributes: [.font: font]).width)
    }

    /// 主役テキストの字体（訳が入っている行だけ太くする）
    private static func primaryFont(size: CGFloat, kind: CaptionLineKind, hasTranslation: Bool) -> NSFont {
        .systemFont(ofSize: size, weight: hasTranslation && kind != .notice ? .semibold : .regular)
    }

    /// 内容を反映し、必要な高さを返す
    ///
    /// - Parameters:
    ///   - japanese: 訳文（空なら原文を主役として出す）
    ///   - source: 原文
    ///   - kind: 行の種類（濃さと字体を決める）
    ///   - width: 使える幅
    ///   - japaneseFontSize: 主役テキストの字の大きさ
    ///   - sourceFontSize: 併記する原文の字の大きさ
    ///   - showsSource: 原文を併記するか
    /// - Returns: この行に要る高さ
    @discardableResult
    func apply(
        japanese: String,
        source: String,
        kind: CaptionLineKind,
        width: CGFloat,
        japaneseFontSize: CGFloat,
        sourceFontSize: CGFloat,
        showsSource: Bool
    ) -> CGFloat {
        // 訳がまだ無い行は原文そのものを主役として出す（空白で待たせない）
        let primary = japanese.isEmpty ? source : japanese
        let hasTranslation = !japanese.isEmpty

        japaneseLabel.stringValue = primary
        japaneseLabel.font = Self.primaryFont(size: japaneseFontSize, kind: kind, hasTranslation: hasTranslation)
        japaneseLabel.textColor = NSColor.labelColor.withAlphaComponent(Self.primaryAlpha(kind: kind, hasTranslation: hasTranslation))
        japaneseLabel.preferredMaxLayoutWidth = width

        let showsSecondary = showsSource && hasTranslation && !source.isEmpty
        sourceLabel.isHidden = !showsSecondary
        if showsSecondary {
            sourceLabel.stringValue = source
            sourceLabel.font = .systemFont(ofSize: sourceFontSize, weight: .regular)
            sourceLabel.textColor = NSColor.secondaryLabelColor.withAlphaComponent(0.85)
            sourceLabel.preferredMaxLayoutWidth = width
        }

        let japaneseHeight = ceil(japaneseLabel.intrinsicContentSize.height)
        let sourceHeight = showsSecondary ? ceil(sourceLabel.intrinsicContentSize.height) : 0
        let total = japaneseHeight + (showsSecondary ? Self.gap + sourceHeight : 0)

        // 原文は訳文の下（＝小さい y）に置く
        if showsSecondary {
            sourceLabel.frame = NSRect(x: 0, y: 0, width: width, height: sourceHeight)
        }
        japaneseLabel.frame = NSRect(
            x: 0, y: showsSecondary ? sourceHeight + Self.gap : 0,
            width: width, height: japaneseHeight
        )
        return total
    }

    /// 行の種類ごとの濃さ
    private static func primaryAlpha(kind: CaptionLineKind, hasTranslation: Bool) -> CGFloat {
        switch kind {
        case .live: return hasTranslation ? 0.78 : 0.5
        case .confirmed: return 0.95
        case .sourceOnly: return 0.88
        case .notice: return 0.92
        }
    }

    /// 折り返し表示のラベルを作る
    private static func makeLabel() -> NSTextField {
        let label = NSTextField(labelWithString: "")
        label.alignment = .center
        label.lineBreakMode = .byWordWrapping
        label.usesSingleLineMode = false
        label.cell?.wraps = true
        label.cell?.isScrollable = false
        // 積み上げ表示なので 1 行あたりは 3 行までに抑える（HUD が映像を隠しすぎないように）
        label.maximumNumberOfLines = 3
        label.drawsBackground = false
        label.isBezeled = false
        label.isEditable = false
        label.isSelectable = false
        return label
    }
}

/// HUD の中身を載せるビュー
///
/// 移動ハンドル以外のクリックを下のアプリへ通す。macOS は透明ピクセルを自動で
/// 透過させるが、ガラス背景は不透明なピクセルなので hitTest で明示的に落とす必要がある。
@available(macOS 26.0, *)
final class HUDContainerView: NSView {

    /// クリックを受け付ける領域（移動ハンドル）
    weak var dragHandle: NSView?
    /// ホバー中だけクリックを受け付ける操作バー
    weak var hoverBar: NSView?
    /// ホバー中だけクリックを受け付けるリサイズハンドル
    weak var resizeHandle: NSView?

    override func hitTest(_ point: NSPoint) -> NSView? {
        let local = convert(point, from: nil)
        if let hoverBar, !hoverBar.isHidden, hoverBar.frame.contains(local) {
            return super.hitTest(point)
        }
        if let resizeHandle, !resizeHandle.isHidden, resizeHandle.frame.contains(local) {
            return super.hitTest(point)
        }
        guard let dragHandle, dragHandle.frame.contains(local) else { return nil }
        return super.hitTest(point)
    }
}

/// HUD にホバーしたときだけ出る操作バー
///
/// 「✕（終了）」は置かない。字幕を見ている最中の誤爆が痛すぎるため、終了はメニュー内だけにする。
@available(macOS 26.0, *)
final class HUDHoverBarView: NSView {

    /// 停止ボタンが押された
    var onStop: (() -> Void)?
    /// 設定ボタンが押された（メニューの基準ビューを渡す）
    var onMenu: ((NSView) -> Void)?

    private let stopButton = NSButton()
    private let menuButton = NSButton()

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        configure(stopButton, title: "⏸ 停止", action: #selector(stopTapped))
        configure(menuButton, title: "⚙ 設定", action: #selector(menuTapped))
        addSubview(stopButton)
        addSubview(menuButton)
        layoutButtons()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) は使わない") }

    override var intrinsicContentSize: NSSize {
        NSSize(width: stopButton.frame.width + menuButton.frame.width + 6, height: 22)
    }

    private func configure(_ button: NSButton, title: String, action: Selector) {
        button.title = title
        button.bezelStyle = .rounded
        button.controlSize = .small
        button.font = .systemFont(ofSize: 11)
        button.target = self
        button.action = action
        button.sizeToFit()
        button.setFrameSize(NSSize(width: ceil(button.frame.width) + 4, height: 22))
    }

    private func layoutButtons() {
        stopButton.setFrameOrigin(NSPoint(x: 0, y: 0))
        menuButton.setFrameOrigin(NSPoint(x: stopButton.frame.width + 6, y: 0))
        setFrameSize(intrinsicContentSize)
    }

    @objc private func stopTapped() { onStop?() }

    @objc private func menuTapped() { onMenu?(menuButton) }
}

/// HUD を掴んで動かすための小さなハンドル
///
/// クリックスルーを既定 ON にしているので、移動手段が無いと HUD が邪魔になったときに
/// 詰む。常時見える最小限のグリップを置く（メニューから位置リセットもできる）。
@available(macOS 26.0, *)
final class HUDDragHandleView: NSView {

    override func draw(_ dirtyRect: NSRect) {
        let dotColor = NSColor.labelColor.withAlphaComponent(0.28)
        dotColor.setFill()
        // 2×3 のグリップドット
        let radius: CGFloat = 1.3
        let spacing: CGFloat = 4.5
        let originX = bounds.midX - spacing / 2
        let originY = bounds.midY - spacing
        for column in 0..<2 {
            for row in 0..<3 {
                let center = NSPoint(
                    x: originX + CGFloat(column) * spacing,
                    y: originY + CGFloat(row) * spacing
                )
                let rect = NSRect(
                    x: center.x - radius, y: center.y - radius,
                    width: radius * 2, height: radius * 2
                )
                NSBezierPath(ovalIn: rect).fill()
            }
        }
    }

    override func mouseDown(with event: NSEvent) {
        // borderless パネルはタイトルバーが無いので、明示的にドラッグを始める
        window?.performDrag(with: event)
    }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .openHand)
    }
}

/// HUD の大きさを変える右下のハンドル
///
/// ホバー時だけ現れる。上下左右のどちらへ引いても「大きさ」1 つが変わる方式にしている
/// （幅・高さ・文字サイズを別々のつまみにすると、ドラッグの意味が分かりにくくなるため）。
@available(macOS 26.0, *)
final class HUDResizeHandleView: NSView {

    /// ドラッグ量から作った倍率の増減
    var onScaleDelta: ((CGFloat) -> Void)?

    override func draw(_ dirtyRect: NSRect) {
        NSColor.labelColor.withAlphaComponent(0.32).setStroke()
        let path = NSBezierPath()
        path.lineWidth = 1.2
        // 右下向きの斜線を 3 本（macOS の伝統的なリサイズグリップ）
        for offset in stride(from: CGFloat(3), through: 11, by: 4) {
            path.move(to: NSPoint(x: bounds.maxX - offset, y: bounds.minY + 2))
            path.line(to: NSPoint(x: bounds.maxX - 2, y: bounds.minY + offset))
        }
        path.stroke()
    }

    override func mouseDragged(with event: NSEvent) {
        // 右または下へ引いたら大きく。係数は「画面幅の数%動かして 1 段階」程度の感触。
        let delta = (event.deltaX + event.deltaY) * 0.004
        guard delta != 0 else { return }
        onScaleDelta?(delta)
    }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .crosshair)
    }
}
