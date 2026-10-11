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
    /// 翻訳の採番（クラウドの訳で差し替えるときに同じ行を探すのに使う。無ければ nil）
    var sequence: Int?
    /// 画面に出た時刻（最低表示時間の判定に使う）
    let shownAt: CFAbsoluteTime
    /// この行を消してよくなるまでの秒数（差し替えで文が伸びたら延ばす）
    var minimumDuration: TimeInterval
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
    /// 新しい字幕が来ないまま消えるまでの秒数
    private static let autoHideDelay: TimeInterval = 8.0

    /// ピルのカプセル下端の、画面下端（visibleFrame.minY）からの高さ
    ///
    /// voicekey 本体の HUD パネルは `visibleFrame.minY + 2` に置かれ、その中でカプセルは
    /// 下端から 6pt 浮かせてある（`Hud.swift` の `positionPanel` と `.padding(.bottom, 6)`）。
    /// 字幕もここへ下端を揃えることで「ピルがそのまま上へ育った」ように見せる。
    ///
    /// Hud.swift の内部には触れない方針のため数値をここに写している。
    /// あちらを変えたらここも合わせること。
    private static let pillCapsuleBottom: CGFloat = 2 + 6

    /// 角丸半径の上限
    ///
    /// 1 行のときは高さの半分＝完全なカプセルにしてピルと同じ形にする。行が増えても
    /// 半径を上げ続けると端の曲率に文字が食い込むため、ここで頭打ちにする。
    private static let maximumCornerRadius: CGFloat = 22

    /// 「ピルから生えて／ピルへ戻る」ときの種になるカプセルの大きさ
    ///
    /// 待機ピルとほぼ同じ見かけの小ささ。ここから目標サイズへ伸ばすことで、
    /// ピルがそのまま字幕へ育ったように見せる。
    private static let seedWidth: CGFloat = 96
    private static let seedHeight: CGFloat = 22

    /// 伸びるときの所要時間（voicekey ピルの spring(response:0.3) に合わせる）
    private static let growDuration: TimeInterval = 0.3
    /// 縮んで消えるときの所要時間（出るときより速く畳む）
    private static let shrinkDuration: TimeInterval = 0.22

    /// 伸縮のイージング
    ///
    /// ピル側は `spring(response: 0.3, dampingFraction: 0.82)`。減衰が強く行き過ぎが
    /// ほぼ無いので、AppKit 側は同じ立ち上がりのイーズアウトで合わせる。
    private static var growTiming: CAMediaTimingFunction {
        CAMediaTimingFunction(controlPoints: 0.22, 1.0, 0.36, 1.0)
    }

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

    /// 確定した行（古い順）。新しいものほど下に出す。
    private var confirmedLines: [CaptionHUDLine] = []
    /// いま喋っている途中の行（最下段）
    private var liveLine: CaptionHUDLine?

    /// 大きさ倍率（幅とフォントに掛かる）
    private var scale: CGFloat = CaptionSettings.hudScale

    private var hideTimer: Timer?
    /// 3 行目に残った確定行が最低表示時間を満たした時点で追い出すタイマー
    private var overflowTimer: Timer?
    private var isVisible = false

    /// 音声入力（録音・変換中・通知）の最中は字幕を隠す
    ///
    /// 「音声入力しているときは字幕は表示されなくていい」（2026-08-10 ユーザー指示）。
    /// 隠すのは表示だけで、認識・翻訳は止めない（戻ったときに話が飛ばないように）。
    var isSuppressed = false {
        didSet {
            guard isSuppressed != oldValue else { return }
            if isSuppressed {
                wasVisibleBeforeSuppression = isVisible
                if isVisible { fadeOut(clearsLines: false) }
            } else if wasVisibleBeforeSuppression, !orderedLines.isEmpty {
                // 隠す前に出ていた行がまだ残っていれば出し直す
                relayout()
                fadeIn()
                scheduleAutoHide()
            }
        }
    }
    /// 隠す直前に表示されていたか（音声入力が終わったときに戻すかの判断に使う）
    private var wasVisibleBeforeSuppression = false

    /// ライブ行を確定行と同じ見た目で出すか（文字起こしモード）
    ///
    /// 文字起こしモードは訳を待たないので、認識途中の本文がそのまま字幕になる。
    /// 薄い字で出すと「まだ途中」に見えて読みにくいため、確定行と同じ字体・濃さにする。
    var liveLooksConfirmed = false {
        didSet {
            guard liveLooksConfirmed != oldValue else { return }
            relayout()
        }
    }

    /// 字幕パネルの表示/非表示が変わったときに呼ばれる（メインスレッド）
    ///
    /// なぜ要るか: 字幕はピルの真上に固定され「ピルがそのまま字幕へ育つ」設計なので、
    /// 字幕を出している間もピルが残っていると、半透明のガラス（alpha 0.62）越しに濃いカプセルが
    /// 字幕テキストの裏へ透け、字幕とピルが別々の物体に見える。voicekey 本体側で待機ピルを
    /// 引っ込めてもらうために、可視状態だけを外へ通知する（誰が聞いているかは知らない）。
    var onVisibilityChanged: ((Bool) -> Void)?

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
        // 常にクリックを下のアプリへ通す（2026-10-12 ユーザー指摘「ライブ字幕は邪魔」）。
        // 以前はホバーで操作バーとリサイズグリップを出すために受けていたが、字幕の上で
        // 動画のシークバーや字幕の下の文字が押せなくなるのが邪魔だった。操作はメニューと設定だけ。
        panel.ignoresMouseEvents = true

        container = HUDContainerView(frame: initialFrame)
        // 映像を隠しすぎないよう、字幕 HUD だけ既定より透けさせる（2026-08-09 ユーザー指示）。
        // ピル（0.7）に合わせず字幕は字幕の透明度を保つ（2026-08-10 ユーザー指示）。
        glass = CaptionGlassBackground(frame: initialFrame, alpha: 0.62)
        glass.autoresizingMask = [.width, .height]
        rim = GlassRimView(frame: initialFrame)
        rim.autoresizingMask = [.width, .height]

        container.addSubview(glass)
        container.addSubview(rim)
        panel.contentView = container

        // 画面構成が変わったら位置を取り直す（Dock 出没・解像度変更・外部ディスプレイの抜き差し）
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

        applyPillAnchoredPosition()
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        NSWorkspace.shared.notificationCenter.removeObserver(self)
    }

    // MARK: - 大きさ

    /// 倍率を掛けたフォントサイズ
    private func scaled(_ size: CGFloat) -> CGFloat {
        (size * scale).rounded()
    }

    /// 大きさ倍率を設定して即座に反映する
    ///
    /// - Parameter newScale: 0.7〜2.0 に丸める
    func setScale(_ newScale: CGFloat) {
        let clamped = min(max(newScale, 0.7), 2.0)
        guard abs(clamped - scale) > 0.001 else { return }
        scale = clamped
        CaptionSettings.hudScale = clamped
        // 幅・高さ・はみ出しの調整はすべて relayout が決める
        relayout()
    }

    /// いまの大きさ倍率
    var currentScale: CGFloat { scale }

    // MARK: - 表示更新

    /// 認識途中のテキストを最下段のライブ行に薄く出す
    ///
    /// - Parameter text: ボラタイルな認識結果
    func showListening(_ text: String) {
        guard !text.isEmpty else { return }
        updateLiveLine(.listening(text), japanese: "", source: text)
    }

    /// 原文が確定した（翻訳待ち）
    ///
    /// 訳が届くまでの数十 ms を空白で待たせないために、ライブ行が空のときだけ原文を出しておく
    /// （新しい話が出ているところへ古い原文を出すと巻き戻って見える）。
    func showPendingSource(_ text: String) {
        updateLiveLine(.pendingSource(text), japanese: "", source: text)
    }

    /// 文字起こしモードの認識途中を出す（確定行と同じ見た目・`liveLooksConfirmed`）
    func showLiveTranscript(_ text: String) {
        guard !text.isEmpty else { return }
        updateLiveLine(.transcript(text), japanese: text, source: "")
    }

    /// 訳文が届いた
    ///
    /// - Parameters:
    ///   - source: 原文
    ///   - japanese: 訳文
    ///   - isPartial: 認識途中の暫定訳なら true（ライブ行を更新するだけ）
    ///   - sequence: 確定訳の採番（後でクラウドの訳に差し替えるときの目印）
    func showTranslation(source: String, japanese: String, isPartial: Bool = false, sequence: Int? = nil) {
        if isPartial {
            updateLiveLine(.partial(source: source, japanese: japanese), japanese: japanese, source: source)
        } else {
            // 確定した文は行として積み上げる。ライブ行は役目を終えるので畳む。
            appendConfirmedLine(kind: .confirmed, japanese: japanese, source: source, sequence: sequence)
        }
    }

    /// 先に出した確定訳を、より良い訳（クラウド）で書き換える
    ///
    /// 行の位置も表示開始時刻も変えない（読んでいる行が飛ばないように）。訳が長くなったら
    /// 読む時間の見積りだけ延ばす（最低表示時間の恒久要件を守るため）。
    /// 該当行がもう消えていたら何もしない。
    ///
    /// - Parameters:
    ///   - sequence: `showTranslation` で渡した採番
    ///   - japanese: 差し替える訳文
    func reviseTranslation(sequence: Int, japanese: String) {
        guard let line = confirmedLines.first(where: { $0.sequence == sequence && !$0.isRemoving }),
              line.japanese != japanese else { return }
        line.japanese = japanese
        let needed = Self.minimumDuration(for: japanese)
        if needed > line.minimumDuration {
            line.minimumDuration = needed
            // 退場タイマーは表示開始からの残り時間で張り直す
            line.retireTimer?.invalidate()
            scheduleSelfRetire(line)
        }
        relayout()
    }

    /// 文字起こしモード（訳さない）の確定文を積む
    ///
    /// 訳文の行と同じ扱い（大きい文字）で出したいので `japanese` に本文を入れ、原文は空にする。
    /// 原文が空なら併記ラベルは出ない（`CaptionLineView` の `showsSecondary` 判定）ので、
    /// 「翻訳できなかった原文」の小さい表示（`.sourceOnly`）とは見た目が別になる。
    func showTranscript(_ text: String) {
        appendConfirmedLine(kind: .confirmed, japanese: text, source: "", sequence: nil)
    }

    /// 翻訳できないので原文のみ確定行として積む
    func showSourceOnly(_ text: String) {
        appendConfirmedLine(kind: .sourceOnly, japanese: "", source: text, sequence: nil)
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
        overflowTimer?.invalidate()
        overflowTimer = nil
        fadeOut()
    }

    /// 大きさを既定に戻す
    ///
    /// 位置はピルに固定なので動かせない。小さくしすぎた／大きくしすぎたときの復帰用。
    func resetSize() {
        // 幅も倍率で決まるので、位置合わせを含めて relayout に任せる
        setScale(1.0)
    }

    // MARK: - 行の管理

    /// ライブ行（最下段）を作る／更新する
    ///
    /// 反映してよいかの判定は `CaptionLiveLinePolicy`（巻き戻り・重複の防止）。
    private func updateLiveLine(_ update: CaptionLiveUpdate, japanese: String, source: String) {
        let current = liveLine.map { CaptionLiveLineText(japanese: $0.japanese, source: $0.source) }
        let last = confirmedLines.last(where: { !$0.isRemoving })
            .map { CaptionLiveLineText(japanese: $0.japanese, source: $0.source) }
        guard CaptionLiveLinePolicy.accepts(update, current: current, lastConfirmed: last) else { return }
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
    private func appendConfirmedLine(kind: CaptionLineKind, japanese: String, source: String, sequence: Int?) {
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
        line.sequence = sequence
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
        let elapsed = CFAbsoluteTimeGetCurrent() - line.shownAt
        let delay = max(0, line.minimumDuration + Self.selfRetireGrace - elapsed)
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
        overflowTimer?.invalidate()
        overflowTimer = nil
        while true {
            let active = confirmedLines.filter { !$0.isRemoving }
            guard active.count > Self.maxConfirmedLines, let oldest = active.first else { return }
            let shown = CFAbsoluteTimeGetCurrent() - oldest.shownAt
            let metMinimum = shown >= oldest.minimumDuration
            // まだ読む時間が残っている行は、上限を 1 行だけ超えて待つ。
            // その間はライブ行を隠して 3 段に収め、読み終わった瞬間に追い出す
            // （以前は次の確定が来るまで 3 行目が居座り、ライブ行と合わせて 4 段になっていた）。
            if !metMinimum, active.count <= Self.hardMaxConfirmedLines {
                let wait = oldest.minimumDuration - shown
                overflowTimer = Timer.scheduledTimer(withTimeInterval: wait, repeats: false) { [weak self] _ in
                    self?.evictOverflowingLines()
                    self?.relayout()
                }
                return
            }
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
        overflowTimer?.invalidate()
        overflowTimer = nil
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
    ///
    /// 確定行が 3 行残っている間はライブ行を出さない（最大 3 段）。
    private var orderedLines: [CaptionHUDLine] {
        guard let liveLine, CaptionLiveLinePolicy.showsLiveLine(confirmedCount: confirmedLines.count) else {
            liveLine?.view.isHidden = true
            return confirmedLines
        }
        liveLine.view.isHidden = false
        return confirmedLines + [liveLine]
    }

    /// パネルの大きさを決め、各行を配置する
    ///
    /// - 幅は倍率から決まる値で**固定**（2026-10-12 ユーザー指摘「ライブ字幕は邪魔」）。
    ///   以前は文字幅に合わせて毎回伸縮させていたため、更新のたびにパネルが横へ揺れて目障りだった。
    /// - 高さは行ごとの高さの合計。ライブ行は常に 1 行ぶんの固定の高さにしてあるので、
    ///   途中経過が伸びても訳が届いても高さは変わらず、変わるのは行が増減したときだけ。
    /// - **下端を固定して上へ伸ばす**（字幕が下から積み上がる見え方になり、読んでいる行が動かない）。
    private func relayout() {
        let width = Self.preferredWidth(scale: scale)
        let contentWidth = width - Self.horizontalPadding * 2
        let lines = orderedLines

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
                    showsSource: showsSourceText,
                    liveLooksConfirmed: liveLooksConfirmed,
                    fixedHeight: line.kind == .live ? Self.liveLineHeight(fontSize: scaled(26)) : nil
                )
            )
        }

        var totalHeight = Self.verticalPadding * 2 + heights.reduce(0, +)
        if lines.count > 1 { totalHeight += Self.lineSpacing * CGFloat(lines.count - 1) }

        // 位置はピル固定。下端をピルのカプセル下端に揃え、上へ伸ばす。
        let anchor = Self.pillAnchor
        let size = NSSize(width: width, height: totalHeight)
        setPanelFrame(
            NSRect(origin: Self.clampedOrigin(CGPoint(x: anchor.x - width / 2, y: anchor.y), size: size), size: size),
            animated: isVisible
        )

        // 1 行のときは完全なカプセル＝ピルと同じ形。行が増えたら上限で頭打ちにする。
        let radius = min(totalHeight / 2, Self.maximumCornerRadius)
        glass.setCornerRadius(radius)
        rim.cornerRadius = radius
        rim.needsDisplay = true

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

        container.needsDisplay = true
    }

    /// ライブ行の固定の高さ（確定行の 1 行ぶん）
    ///
    /// 原文だけの間（小さい字）も暫定訳が出たとき（大きい字）も同じ高さにして、
    /// 途中経過のたびに字幕全体の高さが揺れないようにする。
    private static func liveLineHeight(fontSize: CGFloat) -> CGFloat {
        let font = NSFont.systemFont(ofSize: fontSize, weight: .semibold)
        return ceil(font.ascender - font.descender + font.leading)
    }

    /// 行の種類ごとの訳文フォントサイズ
    private static func japaneseFontSize(for line: CaptionHUDLine) -> CGFloat {
        switch line.kind {
        // 原文だけの間は小さめの字（高さはライブ行の固定値なので揺れない）
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

    /// ピルの真上（下辺の中心）へ位置を合わせる
    ///
    /// ピル固定なので保存も復元もしない。`visibleFrame` 基準なので、Dock の出没・
    /// 解像度変更・外部ディスプレイの抜き差しにはピルと同じように追従する。
    private func applyPillAnchoredPosition() {
        let anchor = Self.pillAnchor
        var frame = panel.frame
        frame.origin = CGPoint(x: anchor.x - frame.width / 2, y: anchor.y)
        setPanelFrame(frame)
        relayout()
    }

    /// 検証用: パネルの現在フレーム
    var panelFrame: NSRect { panel.frame }

    /// 検証用: いま実際に画面へ出ているか
    var isOnScreen: Bool { panel.isVisible && panel.alphaValue > 0.5 }

    /// ピルのカプセル下端の中心（字幕はここへ下辺を揃えて上へ伸びる）
    static var pillAnchor: CGPoint {
        guard let visible = NSScreen.main?.visibleFrame else { return .zero }
        return CGPoint(x: visible.midX, y: visible.minY + pillCapsuleBottom)
    }

    /// パネルのフレームを反映する
    ///
    /// - Parameters:
    ///   - frame: 目標フレーム
    ///   - animated: 行が増減したときに、ピルのモーフィングと同じ速さで伸縮させるか
    private func setPanelFrame(_ frame: NSRect, animated: Bool = false) {
        guard animated, panel.isVisible else {
            panel.setFrame(frame, display: true)
            return
        }
        NSAnimationContext.runAnimationGroup { context in
            context.duration = Self.growDuration
            context.timingFunction = Self.growTiming
            panel.animator().setFrame(frame, display: true)
        }
    }

    /// ピルから生える／ピルへ戻るときの種フレーム（下辺の中心は目標と同じ）
    ///
    /// - Parameter target: 伸ばしきったときのフレーム
    private func seedFrame(for target: NSRect) -> NSRect {
        let width = min(target.width, Self.seedWidth)
        let height = min(target.height, Self.seedHeight)
        return NSRect(x: target.midX - width / 2, y: target.minY, width: width, height: height)
    }

    @objc private func screenParametersChanged() {
        applyPillAnchoredPosition()
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

    /// ピルから生えるように伸ばしながら出す
    private func fadeIn() {
        // 音声入力中は出さない（表示だけ止めて、認識・翻訳は裏で進める）
        guard !isSuppressed else { return }
        // 字幕が更新されるたびに最前面へ出し直す。フルスクリーン動画や Space 切替の直後に
        // 裏へ回ったまま「時々表示されない」状態になるのを塞ぐ（呼び出しは軽い）。
        panel.orderFrontRegardless()
        guard !isVisible else { return }
        isVisible = true
        // 伸び始めと同時にピルを引っ込めてもらう（育ちきってからだと、伸びる途中の
        // 半透明のガラス越しにピルが透けて二重に見える）
        onVisibilityChanged?(true)

        // いったんピル相当の小ささへ畳んでから目標サイズへ伸ばす＝「ピルが字幕に育つ」。
        let target = panel.frame
        panel.setFrame(seedFrame(for: target), display: false)
        panel.alphaValue = 0
        NSAnimationContext.runAnimationGroup { context in
            context.duration = Self.growDuration
            context.timingFunction = Self.growTiming
            panel.animator().alphaValue = 1.0
            panel.animator().setFrame(target, display: true)
        }
    }

    /// ピルへ戻るように縮めながら消す
    ///
    /// - Parameter clearsLines: 行も片付けるか。音声入力で一時的に隠すだけのときは残す。
    private func fadeOut(clearsLines: Bool = true) {
        guard isVisible else {
            panel.alphaValue = 0
            if clearsLines { removeAllLines() }
            // すでに非表示でもここで false を流す。流さないとピル側と状態が食い違い、
            // 一度引っ込んだ待機ピルが二度と戻らなくなる。
            onVisibilityChanged?(false)
            return
        }
        isVisible = false
        let seed = seedFrame(for: panel.frame)
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = Self.shrinkDuration
            context.timingFunction = Self.growTiming
            panel.animator().alphaValue = 0
            panel.animator().setFrame(seed, display: true)
        }, completionHandler: { [weak self] in
            guard let self, !self.isVisible else { return }
            self.panel.orderOut(nil)
            // 次に喋り出したとき、前の話の行が残っていると混乱するので片付ける
            if clearsLines { self.removeAllLines() }
            // ピルを戻すのは畳み終わってから。縮んでいく 0.22 秒の途中で戻すと、
            // 半透明の字幕の裏にピルが透けて二重に見える（今回直した不具合そのもの）。
            self.onVisibilityChanged?(false)
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
    ///   - liveLooksConfirmed: ライブ行を確定行と同じ見た目にするか（文字起こしモード）
    ///   - fixedHeight: 行の高さを固定する場合の値（ライブ行）。nil なら内容ぶん
    /// - Returns: この行に要る高さ
    @discardableResult
    func apply(
        japanese: String,
        source: String,
        kind: CaptionLineKind,
        width: CGFloat,
        japaneseFontSize: CGFloat,
        sourceFontSize: CGFloat,
        showsSource: Bool,
        liveLooksConfirmed: Bool = false,
        fixedHeight: CGFloat? = nil
    ) -> CGFloat {
        // 訳がまだ無い行は原文そのものを主役として出す（空白で待たせない）
        let primary = japanese.isEmpty ? source : japanese
        let hasTranslation = !japanese.isEmpty
        // 文字起こしモードのライブ行は確定行と同じ字体・濃さで描く
        let styleKind: CaptionLineKind = (kind == .live && liveLooksConfirmed) ? .confirmed : kind

        // ライブ行は 1 行に収め、はみ出したら**先頭**を省く（いま喋っている末尾を見せるため）
        let isLive = kind == .live
        japaneseLabel.maximumNumberOfLines = isLive ? 1 : 3
        japaneseLabel.lineBreakMode = isLive ? .byTruncatingHead : .byWordWrapping
        japaneseLabel.cell?.wraps = !isLive

        japaneseLabel.stringValue = primary
        japaneseLabel.font = Self.primaryFont(size: japaneseFontSize, kind: styleKind, hasTranslation: hasTranslation)
        japaneseLabel.textColor = NSColor.labelColor.withAlphaComponent(Self.primaryAlpha(kind: styleKind, hasTranslation: hasTranslation))
        japaneseLabel.preferredMaxLayoutWidth = isLive ? 0 : width

        // ライブ行には原文を併記しない（訳が届いた瞬間に 2 段へ伸びて高さが揺れるため）
        let showsSecondary = !isLive && showsSource && hasTranslation && !source.isEmpty
        sourceLabel.isHidden = !showsSecondary
        if showsSecondary {
            sourceLabel.stringValue = source
            sourceLabel.font = .systemFont(ofSize: sourceFontSize, weight: .regular)
            sourceLabel.textColor = NSColor.secondaryLabelColor.withAlphaComponent(0.85)
            sourceLabel.preferredMaxLayoutWidth = width
        }

        if let fixedHeight {
            // 固定の高さの中で縦中央に置く（字の大きさが変わっても行の位置は動かない）
            let textHeight = min(ceil(japaneseLabel.intrinsicContentSize.height), fixedHeight)
            japaneseLabel.frame = NSRect(
                x: 0, y: floor((fixedHeight - textHeight) / 2), width: width, height: textHeight
            )
            return fixedHeight
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
/// クリックはすべて下のアプリへ通す（パネル側も `ignoresMouseEvents = true`）。
/// macOS は透明ピクセルを自動で透過させるが、ガラス背景は不透明なピクセルなので
/// hitTest でも明示的に落としておく。
@available(macOS 26.0, *)
final class HUDContainerView: NSView {

    override func hitTest(_ point: NSPoint) -> NSView? {
        nil
    }
}
