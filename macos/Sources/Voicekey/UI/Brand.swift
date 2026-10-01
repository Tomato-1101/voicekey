//
//  Brand.swift
//  voicekey のブランド（ロゴ「17 LEGEND」）の配色とアプリアイコン表示を 1 か所に集める。
//
//  色をここ以外で直書きしないための置き場。アクセントは signal（灯りの橙）の 1 色だけで、
//  システムのアクセント色（ユーザー設定で青や紫に変わる）には頼らない。
//  アイコンはライト=ボーン／ダーク=カーボンの 2 枚を外観で選ぶ（OS の外観に追従させるため）。
//

import AppKit
import SwiftUI

/// ブランドのパレット。SwiftUI からは `Brand.signal`、AppKit からは `Brand.NS.signal` で使う。
enum Brand {

    /// AppKit 用（メニューバーの描画・CALayer・NSView から使う）
    enum NS {
        /// 灯り。唯一のアクセント（録音の点・トグル ON・選択・進捗）
        static let signal = rgb(0xFF5A1F)
        /// ライト背景の上に小さい文字で signal を使うときだけ（コントラスト確保）
        static let signalDeep = rgb(0xD9480F)
        /// ライトの地
        static let bone = rgb(0xF4F1EB)
        /// 線・弱い面
        static let stone = rgb(0xCFC9BE)
        /// 補助文字（刻印）
        static let legend = rgb(0x7F796F)
        /// ダークの地・ライトの濃い文字
        static let carbon = rgb(0x2B2A27)

        /// 0xRRGGBB を sRGB の NSColor にする
        static func rgb(_ hex: UInt32) -> NSColor {
            NSColor(
                srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
                green: CGFloat((hex >> 8) & 0xFF) / 255,
                blue: CGFloat(hex & 0xFF) / 255,
                alpha: 1
            )
        }
    }

    static let signal = Color(nsColor: NS.signal)
    static let signalDeep = Color(nsColor: NS.signalDeep)
    static let bone = Color(nsColor: NS.bone)
    static let stone = Color(nsColor: NS.stone)
    static let legend = Color(nsColor: NS.legend)
    static let carbon = Color(nsColor: NS.carbon)

    // MARK: - アプリアイコン

    /// ライト外観用（ボーン地）のアイコン。バンドル外（swift test 等）では nil
    private static let lightIcon: NSImage? = bundledImage("AppIconLight")
    /// ダーク外観用（カーボン地）のアイコン
    private static let darkIcon: NSImage? = bundledImage("AppIconDark")

    /// 外観に合ったアプリアイコンを返す
    ///
    /// build_app.sh が同梱する PNG を使う。`swift run` などバンドルに PNG が無い起動では
    /// 従来どおり `NSApp.applicationIconImage`（icns 由来）に落とす。
    ///
    /// - Parameter dark: ダーク外観なら true
    @MainActor
    static func appIcon(dark: Bool) -> NSImage {
        (dark ? darkIcon : lightIcon) ?? NSApp.applicationIconImage ?? NSImage()
    }

    /// 外観（NSAppearance）がダーク系かを判定する
    static func isDark(_ appearance: NSAppearance) -> Bool {
        appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
    }

    private static func bundledImage(_ name: String) -> NSImage? {
        guard let url = Bundle.main.url(forResource: name, withExtension: "png") else { return nil }
        return NSImage(contentsOf: url)
    }
}

/// 外観でボーン／カーボンを切り替えるアプリアイコン
///
/// `NSApp.applicationIconImage` は外観が変わっても SwiftUI に再描画を通知しないため、
/// `colorScheme` を見て自分で選ぶ。サイズ・角丸は呼び出し側の modifier で決める。
struct BrandIcon: View {
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        Image(nsImage: Brand.appIcon(dark: colorScheme == .dark))
            .resizable()
            .interpolation(.high)
    }
}
