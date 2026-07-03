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
    @ViewBuilder
    func glassButtons() -> some View {
        if #available(macOS 26.0, *), !glassForceFallback {
            self.buttonStyle(.glass)
        } else {
            self.buttonStyle(GlassApproxButtonStyle(prominent: false))
        }
    }

    /// 主要アクション 1 個に適用するガラス（accent 色）。.borderedProminent の置換先。
    @ViewBuilder
    func glassProminentButton() -> some View {
        if #available(macOS 26.0, *), !glassForceFallback {
            self.buttonStyle(.glassProminent)
        } else {
            self.buttonStyle(GlassApproxButtonStyle(prominent: true))
        }
    }
}

/// macOS 14–15 用のガラス近似ボタンスタイル。
/// なぜ Capsule 固定か: macOS 26 のガラスボタン既定形状（カプセル）に寄せて、
/// 新旧 OS 間で形状の印象を揃えるため。
struct GlassApproxButtonStyle: ButtonStyle {
    /// 主要アクション（accent 塗り・白文字）かどうか
    var prominent: Bool
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.controlSize) private var controlSize

    func makeBody(configuration: Configuration) -> some View {
        let small = (controlSize == .small)
        return configuration.label
            .font(.system(size: small ? 11 : 13))
            .padding(.horizontal, small ? 8 : 12)
            .padding(.vertical, small ? 3 : 5)
            .foregroundStyle(foreground(for: configuration))
            // prominent=accent 塗り / 通常=極薄マテリアル
            .background(
                prominent ? AnyShapeStyle(Color.accentColor) : AnyShapeStyle(.ultraThinMaterial),
                in: Capsule()
            )
            // 通常ボタンだけ薄い縁取り（prominent は塗りがあるので不要）
            .overlay {
                if !prominent {
                    Capsule().strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.5)
                }
            }
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
    /// ウィンドウのルートビューに 1 回だけ適用する全面すりガラス背景。
    /// - Parameter material: 透過素材（既定は透け感のある sidebar。underWindowBackground は実表示で透けが弱すぎた。.hudWindow は透けすぎるので使わない）
    func frostedWindowBackground(material: NSVisualEffectView.Material = .sidebar) -> some View {
        background(VisualEffectBackdrop(material: material).ignoresSafeArea())
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
