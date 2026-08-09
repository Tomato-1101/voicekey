/// 字幕 HUD の背景ガラス
///
/// 背後に「他アプリの画面」を映す必要があるため、SwiftUI の `glassEffect` や
/// `.ultraThinMaterial` は使えない（同一ウィンドウ内しかサンプルできず、中身が透明な
/// HUD では灰色の塊になる）。ウィンドウ越しのライブなぼかしは
/// `NSVisualEffectView(.behindWindow)` だけが出せる。
import AppKit

/// 角丸のすりガラス背景
@available(macOS 26.0, *)
final class CaptionGlassBackground: NSVisualEffectView {

    /// 角丸半径の既定値（字幕 HUD 用）
    private static let cornerRadius: CGFloat = 16

    /// 透け感の既定値。1.0 は不透明な塊、0.4 以下は素通しで存在感が消える。
    private static let defaultAlpha: CGFloat = 0.72

    /// - Parameters:
    ///   - frameRect: 初期フレーム
    ///   - cornerRadius: 角丸半径。操作ピルのように背の低いものは高さの半分を渡して
    ///     完全な丸端にする（既定のままだとマスクが縦に潰れて角が歪む）
    ///   - alpha: 透け感。映像を隠したくない字幕 HUD だけ既定より下げている
    init(
        frame frameRect: NSRect,
        cornerRadius: CGFloat = CaptionGlassBackground.cornerRadius,
        alpha: CGFloat = CaptionGlassBackground.defaultAlpha
    ) {
        currentRadius = cornerRadius
        super.init(frame: frameRect)
        // HUD 用に設計された material。迷ったらこれ。
        material = .hudWindow
        blendingMode = .behindWindow
        // これを忘れると非アクティブ時にぼかしが無効化される（常駐 HUD では致命的）
        state = .active
        alphaValue = alpha
        maskImage = Self.roundedMask(radius: cornerRadius)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) は使わない") }

    /// いま適用している角丸半径（作り直しの要否判定に使う）
    private var currentRadius: CGFloat

    /// 角丸半径を差し替える
    ///
    /// 字幕は行数で高さが変わるため、ピル状の見た目を保つには高さに応じて半径も変える。
    /// マスク画像の作り直しはちらつくので、実際に変わったときだけ作り直す。
    ///
    /// - Parameter radius: 新しい角丸半径
    func setCornerRadius(_ radius: CGFloat) {
        guard abs(radius - currentRadius) > 0.5 else { return }
        currentRadius = radius
        maskImage = Self.roundedMask(radius: radius)
    }

    /// 角丸マスクを作る
    ///
    /// 上下左右すべてに `capInsets` を入れて stretch させるため、幅・高さがどう変わっても
    /// 画像を作り直す必要がない（字幕は行数で高さが変わるので、毎回作り直すとちらつく）。
    ///
    /// - Parameter radius: 角丸半径
    private static func roundedMask(radius: CGFloat) -> NSImage {
        let side = radius * 2 + 2
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { rect in
            NSColor.black.setFill()
            NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
            return true
        }
        image.capInsets = NSEdgeInsets(top: radius, left: radius, bottom: radius, right: radius)
        image.resizingMode = .stretch
        return image
    }
}

/// ガラスの上に重ねる細い輪郭線と上縁ハイライト
///
/// すりガラスは屈折が出ないぶん、この 2 本の線が「ガラスらしさ」を担う。
/// 数値は控えめにする（濃くすると一気に安っぽくなる）。
@available(macOS 26.0, *)
final class GlassRimView: NSView {

    /// 角丸半径（背景と揃える）
    var cornerRadius: CGFloat = 16

    override var isFlipped: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        let inset = bounds.insetBy(dx: 0.25, dy: 0.25)
        let path = NSBezierPath(roundedRect: inset, xRadius: cornerRadius, yRadius: cornerRadius)
        path.lineWidth = 0.5

        // 輪郭（ライト/ダーク両対応にするため labelColor 由来にする）
        NSColor.labelColor.withAlphaComponent(0.10).setStroke()
        path.stroke()

        // 上縁のハイライト（レンズの立ち上がりの代わり）
        let highlight = NSBezierPath()
        let radius = cornerRadius
        highlight.move(to: NSPoint(x: inset.minX + radius, y: inset.maxY))
        highlight.line(to: NSPoint(x: inset.maxX - radius, y: inset.maxY))
        highlight.lineWidth = 0.75
        NSColor.white.withAlphaComponent(0.16).setStroke()
        highlight.stroke()
    }

    /// 装飾なのでクリックは常に透過させる
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}
