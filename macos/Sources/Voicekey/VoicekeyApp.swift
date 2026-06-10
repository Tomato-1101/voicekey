//
//  VoicekeyApp.swift
//  アプリエントリポイント（メニューバー常駐）
//

import AppKit
import SwiftUI

@main
struct VoicekeyApp: App {
    @StateObject private var controller = AppController()

    init() {
        // メニューバー常駐アプリとして Dock / Cmd+Tab から隠す
        // （バンドル時は Info.plist の LSUIElement でも指定するが、
        //  swift run での開発実行でも同じ挙動になるようここでも設定する）
        NSApplication.shared.setActivationPolicy(.accessory)
    }

    var body: some Scene {
        MenuBarExtra {
            // 状態表示（情報行）
            Text(controller.state.label)

            Divider()

            SettingsLink {
                Text("設定…")
            }
            .keyboardShortcut(",")

            Divider()

            Button("voicekey を終了") {
                controller.shutdown()
                NSApp.terminate(nil)
            }
            .keyboardShortcut("q")
        } label: {
            Image(nsImage: StatusIcon.image(for: controller.state))
                .task {
                    // メニューバーアイテム表示後に一度だけ起動処理を走らせる
                    controller.startup()
                }
        }

        Settings {
            SettingsView(config: controller.config)
        }
    }
}

/// メニューバーアイコンの生成（状態で色が変わる）
enum StatusIcon {

    /// 状態に応じたメニューバーアイコンを返す
    static func image(for state: AppState) -> NSImage {
        switch state {
        case .idle:
            // テンプレート画像: ライト/ダークメニューバーに自動追従
            return symbol("mic.fill", color: nil)
        case .recording(let autoEnter):
            return symbol("mic.fill", color: autoEnter ? .systemPurple : .systemRed)
        case .transcribing:
            return symbol("waveform", color: .systemOrange)
        }
    }

    private static func symbol(_ name: String, color: NSColor?) -> NSImage {
        let config = NSImage.SymbolConfiguration(pointSize: 14, weight: .medium)
        guard var image = NSImage(
            systemSymbolName: name,
            accessibilityDescription: "voicekey"
        )?.withSymbolConfiguration(config) else {
            return NSImage()
        }
        if let color {
            // 色付き = 非テンプレート（録音中などの状態を色で示す）
            image = tinted(image, color: color)
            image.isTemplate = false
        } else {
            image.isTemplate = true
        }
        return image
    }

    private static func tinted(_ image: NSImage, color: NSColor) -> NSImage {
        NSImage(size: image.size, flipped: false) { rect in
            image.draw(in: rect)
            color.set()
            rect.fill(using: .sourceAtop)
            return true
        }
    }
}
