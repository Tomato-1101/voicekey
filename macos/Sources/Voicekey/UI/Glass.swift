//
//  Glass.swift
//  Liquid Glass デザインの共通ヘルパ（macOS 26 の本物のガラス / 旧 OS の近似フォールバック）
//
//  なぜ 1 ファイルに集約するか:
//  - 呼び出し側（SettingsView / OnboardingView / FeedbackView / Hud）に #available 分岐を
//    一切書かせないため。OS 判定・フォールバック描画はすべてここへ閉じ込める。
//  - release / main 両ブランチで使い回すため、LoginCoordinator 等の release 固有型に
//    依存させない（import は AppKit / SwiftUI のみ）。main へはこのファイルをコピーし、
//    呼び出し側の modifier 適用行だけ個別に移植すればよい。
//
//  注意: SDK 26 の SwiftUI には `Glass` という構造体があるため、ここで独自の `Glass` 型を
//  宣言してはならない（型名衝突する）。ファイル名が Glass.swift なのは問題ない。
//
//  新規にボタンを追加するときの注意: コンテナで .glassButtons() を適用しているため、
//  native 挙動を保ちたいボタン（行内・サイドバー等）は必ず .buttonStyle(.plain) /
//  .borderless を明示すること（明示済みボタンはコンテナのスタイルを継承しない）。
//

import AppKit
import SwiftUI

/// macOS 26 上で旧 OS のフォールバック描画を視覚 QA するためのフラグ。
/// なぜ必要か: 実機で毎回旧 OS を用意できないため、VOICEKEY_GLASS_FALLBACK=1 で
/// 近似スタイルを強制し、フォールバック側の見た目を macOS 26 上で確認できるようにする。
private var glassForceFallback: Bool {
    ProcessInfo.processInfo.environment["VOICEKEY_GLASS_FALLBACK"] == "1"
}

// MARK: - ガラス面（カード・チップ・選択ピル）

extension View {

    /// 角丸カード状のガラス面にする。tint を渡すと色付きガラス（選択状態のピル等）。
    /// - Parameters:
    ///   - cornerRadius: 角丸半径
    ///   - tint: 色付きガラスにする場合の色（nil なら無色ガラス）
    @ViewBuilder
    func glassSurface(cornerRadius: CGFloat = 12, tint: Color? = nil) -> some View {
        if #available(macOS 26.0, *), !glassForceFallback {
            // 本物のガラス。tint 付きは選択状態の色付きピルに使う
            self.glassEffect(
                tint.map { Glass.regular.tint($0) } ?? Glass.regular,
                in: .rect(cornerRadius: cornerRadius)
            )
        } else if let tint {
            // 旧 OS の色付き面はマテリアルを重ねず単色 85% で近似（現行の選択ピルと同等の見た目）
            self.background(tint.opacity(0.85), in: RoundedRectangle(cornerRadius: cornerRadius))
        } else {
            // 旧 OS の無色ガラスは極薄マテリアル + 薄い縁取りで近似。
            // 縁は .primary 由来にする（.white だとライトモードで縁が消える）
            self
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: cornerRadius))
                .overlay(
                    RoundedRectangle(cornerRadius: cornerRadius)
                        .strokeBorder(Color.primary.opacity(0.10), lineWidth: 0.5)
                )
        }
    }

    /// カプセル形のガラス面（ステップドットのチップ等）。glassSurface のカプセル版。
    /// - Parameter tint: 色付きガラスにする場合の色（nil なら無色ガラス）
    @ViewBuilder
    func glassCapsule(tint: Color? = nil) -> some View {
        if #available(macOS 26.0, *), !glassForceFallback {
            self.glassEffect(
                tint.map { Glass.regular.tint($0) } ?? Glass.regular,
                in: .capsule
            )
        } else if let tint {
            self.background(tint.opacity(0.85), in: Capsule())
        } else {
            self
                .background(.ultraThinMaterial, in: Capsule())
                .overlay(Capsule().strokeBorder(Color.primary.opacity(0.10), lineWidth: 0.5))
        }
    }
}

// MARK: - ガラスボタン

extension View {

    /// 配下の全 Button の既定スタイルをガラスにする（コンテナに 1 回だけ適用）。
    /// .plain / .borderless を明示したボタンには影響しない（native 挙動を維持したい行内ボタン用）。
    /// stock の .buttonStyle(.glass) は「透け感が弱い」と却下されたため、26 でも自作の
    /// LiquidButtonStyle を使う（本物のガラス＋リム＋影で「透け」を強く出す）。
    func glassButtons() -> some View {
        buttonStyle(LiquidButtonStyle(prominent: false))
    }

    /// 主要アクション 1 個に適用するガラス（accent グラデ）。.borderedProminent の置換先。
    func glassProminentButton() -> some View {
        buttonStyle(LiquidButtonStyle(prominent: true))
    }
}

/// ガラスボタンスタイル（全 OS 共通）。
/// - 通常: 本物のガラス（26）/ 単色半透明（旧 OS）＋上縁リム＋影で「透け」を出す。
/// - prominent: アクセントのグラデ塗り＋白リム＋アクセントのグロー影で主張させる。
/// なぜ Capsule 固定か: macOS 26 のガラスボタン既定形状（カプセル）に印象を揃えるため。
struct LiquidButtonStyle: ButtonStyle {
    /// 主要アクション（accent グラデ・白文字）かどうか
    var prominent: Bool
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.controlSize) private var controlSize
    @Environment(\.colorScheme) private var colorScheme

    func makeBody(configuration: Configuration) -> some View {
        let small = (controlSize == .small)
        return configuration.label
            .font(.system(size: small ? 11 : 13))
            .padding(.horizontal, small ? 8 : 12)
            .padding(.vertical, small ? 3 : 5)
            .foregroundStyle(foreground(for: configuration))
            .modifier(LiquidButtonBackground(prominent: prominent, colorScheme: colorScheme))
            .opacity(configuration.isPressed ? 0.7 : (isEnabled ? 1 : 0.5))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
    }

    /// 文字色: prominent=白 / destructive=赤（「履歴を消去」等）/ 通常=primary
    private func foreground(for configuration: Configuration) -> Color {
        if prominent { return .white }
        if configuration.role == .destructive { return .red }
        return .primary
    }
}

/// ボタン背景（カプセル）。prominent はアクセントグラデ、通常は透けるガラス。
private struct LiquidButtonBackground: ViewModifier {
    let prominent: Bool
    let colorScheme: ColorScheme

    func body(content: Content) -> some View {
        if prominent {
            content
                // アクセントのグラデ塗り（全 OS 共通・glassEffect 不使用）。上が明るく立体感を出す
                .background(
                    LinearGradient(
                        colors: [Color.accentColor, Color.accentColor.opacity(0.78)],
                        startPoint: .top, endPoint: .bottom
                    ),
                    in: Capsule()
                )
                // 上縁の白リム（強め）でガラスの厚みを出す
                .overlay(
                    Capsule().strokeBorder(
                        LinearGradient(
                            colors: [.white.opacity(0.55), .white.opacity(0.0)],
                            startPoint: .top, endPoint: .bottom
                        ),
                        lineWidth: 1
                    )
                )
                // アクセント色のグロー影で「光っている」主張を作る
                .shadow(color: Color.accentColor.opacity(0.5), radius: 10, x: 0, y: 4)
        } else {
            content
                .modifier(LiquidButtonGlassFill(colorScheme: colorScheme))
                // 上半分の白リム
                .overlay(
                    Capsule().strokeBorder(
                        LinearGradient(
                            colors: [.white.opacity(0.45), .white.opacity(0.05)],
                            startPoint: .top, endPoint: .bottom
                        ),
                        lineWidth: 1
                    )
                )
                .shadow(color: .black.opacity(0.20), radius: 6, x: 0, y: 3)
        }
    }
}

/// 通常ボタンのガラスフィル。26 は interactive な本物のガラス、旧 OS は単色半透明。
/// マテリアルではなく単色半透明にするのは「後ろの色が透ける」感を強めるため（設計方針）。
private struct LiquidButtonGlassFill: ViewModifier {
    let colorScheme: ColorScheme

    func body(content: Content) -> some View {
        if #available(macOS 26.0, *), !glassForceFallback {
            // clear は regular より透けるので優先採用。押下でにじむ interactive を付ける
            content.glassEffect(Glass.clear.interactive(), in: .capsule)
        } else {
            content.background(
                colorScheme == .dark ? Color.white.opacity(0.08) : Color.white.opacity(0.35),
                in: Capsule()
            )
        }
    }
}

// MARK: - ウィンドウ backdrop（すりガラス下地）

/// NSVisualEffectView（behindWindow）の SwiftUI ラッパ。デスクトップが透ける下地。
/// なぜ NSVisualEffectView か: glassEffect はコントロール層用で、ウィンドウ全面の
/// デスクトップ透過は behindWindow のブレンドが macOS 14〜26 で同一に動く鉄板構成のため。
struct VisualEffectBackdrop: NSViewRepresentable {
    var material: NSVisualEffectView.Material = .sidebar

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = .behindWindow          // ウィンドウの「後ろ」＝デスクトップをぼかす
        view.state = .followsWindowActiveState      // 非アクティブ時に沈む native 挙動
        return view
    }

    func updateNSView(_ view: NSVisualEffectView, context: Context) {
        view.material = material
    }
}

extension View {
    /// ウィンドウのルートビューに 1 回だけ適用する全面すりガラス背景（backdrop v2）。
    /// - Parameter material: 透過素材。v1 の .sidebar は「透けが弱い／2 画面に見える」と
    ///   却下されたため、より強く透ける .hudWindow を既定にする（派手さ優先の方針転換）。
    ///   その上にアクセント寄りのグラデーションウォッシュを重ねて色 depth（高級感）を出す。
    func frostedWindowBackground(material: NSVisualEffectView.Material = .hudWindow) -> some View {
        background(FrostedBackdrop(material: material).ignoresSafeArea())
    }
}

/// backdrop 本体（すりガラス下地＋グラデーションウォッシュ）。
/// colorScheme でライト/ダークのウォッシュ色を切り替えるため、View 拡張ではなく
/// 独立 View にする（View 拡張メソッドから @Environment は読めないため）。
private struct FrostedBackdrop: View {
    let material: NSVisualEffectView.Material
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        ZStack {
            VisualEffectBackdrop(material: material)
            wash  // デスクトップ透過の上に薄く色を乗せて「色気」を出す（可読性を壊さない低 opacity）
        }
    }

    /// アクセント寄りのグラデーションウォッシュ。数値は実写で可読性が落ちない範囲に調整。
    private var wash: some View {
        LinearGradient(
            colors: colorScheme == .dark
                ? [Color.accentColor.opacity(0.16), Color.purple.opacity(0.10), Color.black.opacity(0.25)]
                : [Color.accentColor.opacity(0.10), Color.white.opacity(0.20), Color.white.opacity(0.05)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }
}

// MARK: - 浮遊するガラス島

extension View {
    /// 浮遊するガラス島にする（影＋上縁リムライト付き）。
    /// 「サイドバーとコンテンツが別々に浮いて見える」1 枚ガラス構成の本体。
    /// - Parameter cornerRadius: 角丸半径
    func glassIsland(cornerRadius: CGFloat = 18) -> some View {
        modifier(GlassIslandModifier(cornerRadius: cornerRadius))
    }

    /// Form/List の行フィルを半透明にする（島の中で行が「ガラスの棚」に見えるように）。
    /// grouped Form の既定行フィルはほぼ不透明で、島の上で浮いてしまうため半透明化する。
    func glassFormRows() -> some View {
        modifier(GlassFormRowsModifier())
    }
}

/// ガラス島の描画（フィル→リム→影の順に重ねる）。shadow は最外なのでクリップされない。
private struct GlassIslandModifier: ViewModifier {
    let cornerRadius: CGFloat
    @Environment(\.colorScheme) private var colorScheme

    func body(content: Content) -> some View {
        content
            .modifier(GlassIslandFill(cornerRadius: cornerRadius))
            // 上縁の光るリム＝ガラスの厚み表現。ライトでも低 opacity なので飛ばない
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius)
                    .strokeBorder(
                        LinearGradient(
                            colors: [.white.opacity(0.40), .white.opacity(0.06)],
                            startPoint: .top, endPoint: .bottom
                        ),
                        lineWidth: 1
                    )
            )
            // 影で浮遊感を出す。ライトは影が濃すぎると汚く見えるので弱める
            .shadow(
                color: .black.opacity(colorScheme == .dark ? 0.30 : 0.15),
                radius: 22, x: 0, y: 10
            )
    }
}

/// ガラス島のフィル。26 は本物のガラス、旧 OS は極薄マテリアルで近似。
private struct GlassIslandFill: ViewModifier {
    let cornerRadius: CGFloat

    func body(content: Content) -> some View {
        if #available(macOS 26.0, *), !glassForceFallback {
            content.glassEffect(Glass.regular, in: .rect(cornerRadius: cornerRadius))
        } else {
            content.background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: cornerRadius))
        }
    }
}

/// 行フィル半透明化。listRowBackground を角丸の単色フィルにして「ガラスの棚」に見せる。
private struct GlassFormRowsModifier: ViewModifier {
    @Environment(\.colorScheme) private var colorScheme

    func body(content: Content) -> some View {
        content.listRowBackground(
            RoundedRectangle(cornerRadius: 8)
                .fill(colorScheme == .dark ? Color.white.opacity(0.06) : Color.white.opacity(0.40))
        )
    }
}

// MARK: - NSWindow chrome 設定

/// すりガラスウィンドウの NSWindow 側設定（ウィンドウ生成直後に 1 回呼ぶ）。
@MainActor
enum GlassWindow {
    /// タイトルバーをコンテンツと一体化し、背景を透明にして backdrop がデスクトップを
    /// サンプルできるようにする。
    static func applyFrostedChrome(to window: NSWindow) {
        window.styleMask.insert(.fullSizeContentView)  // コンテンツをタイトルバー下まで拡張
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden               // タイトル文字は消す（window.title は Mission Control 用に残す）
        window.isOpaque = false
        window.backgroundColor = .clear                // これで backdrop がデスクトップをサンプルできる
        window.isMovableByWindowBackground = true      // タイトルバー非表示化に伴い背景ドラッグで動かせるように
    }
}

// MARK: - GlassEffectContainer ラッパ

/// 兄弟ガラス形状を 1 パスで描画する（26 未満ではただの素通し）。サイドバーのナビ選択ピル用。
/// なぜコンテナが要るか: 隣接するガラス形状同士のブレンド（にじみ・モーフィング）を
/// 正しく合成させるため。ネストは禁止（層は最大 2 段）。
struct GlassGroup<Content: View>: View {
    var spacing: CGFloat? = nil
    @ViewBuilder var content: Content

    var body: some View {
        if #available(macOS 26.0, *), !glassForceFallback {
            GlassEffectContainer(spacing: spacing) { content }
        } else {
            content
        }
    }
}
