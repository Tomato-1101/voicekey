//
//  fullscreen_helper.swift
//  HUD のフルスクリーン挙動を実測するための開発用ヘルパー（単体で実行する）。
//
//  用途（恒久回帰ハーネス）:
//   1. 待機ピルのフルスクリーン非表示テスト: 本物のフルスクリーン Space を作り、
//      その上で voicekey の待機ピルが消える／録音ピルは出る、を screencapture で検証する。
//   2. 変換中ピルの横揺れ計測: 単色（既定=白）の全画面背景を敷くことで、ガラス越しの
//      「変換中…」文字を高コントラスト化し、テキストの x 位置の時系列を画素で測れるようにする。
//
//  TCC（アクセシビリティ／画面収録）を一切要求しないのが要点。自分のウィンドウを
//  toggleFullScreen(nil) するだけなので承認ダイアログは出ない。
//
//  使い方:
//    swift macos/scripts/dev/fullscreen_helper.swift [--color FFFFFF] [--seconds 20] [--no-fullscreen]
//    --color      背景色（6桁 hex・既定 FFFFFF=白）
//    --seconds    自動終了までの秒数（既定 0=終了しない。kill で止める）
//    --no-fullscreen  フルスクリーンにせず通常ウィンドウ（単色背景だけ欲しいとき）
//
//  標準出力に "FS_READY"（ウィンドウ表示）／"FS_ENTERED"（フルスクリーン遷移完了）を
//  出すので、キャプチャ側スクリプトはこれを待ってから撮影できる。
//

import AppKit

// MARK: - 引数パース
func argValue(_ name: String) -> String? {
    let a = CommandLine.arguments
    guard let i = a.firstIndex(of: name), i + 1 < a.count else { return nil }
    return a[i + 1]
}
let colorHex = argValue("--color") ?? "FFFFFF"
let autoQuit = Double(argValue("--seconds") ?? "0") ?? 0
let noFullScreen = CommandLine.arguments.contains("--no-fullscreen")

// 6桁 hex → NSColor
func colorFromHex(_ hex: String) -> NSColor {
    var s = hex.trimmingCharacters(in: .whitespaces)
    if s.hasPrefix("#") { s.removeFirst() }
    guard s.count == 6, let v = UInt32(s, radix: 16) else { return .white }
    let r = CGFloat((v >> 16) & 0xFF) / 255.0
    let g = CGFloat((v >> 8) & 0xFF) / 255.0
    let b = CGFloat(v & 0xFF) / 255.0
    return NSColor(srgbRed: r, green: g, blue: b, alpha: 1.0)
}

final class Delegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var window: NSWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        let screen = NSScreen.main ?? NSScreen.screens[0]
        window = NSWindow(
            contentRect: screen.frame,
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "voicekey fullscreen helper"
        let view = NSView(frame: screen.frame)
        view.wantsLayer = true
        view.layer?.backgroundColor = colorFromHex(colorHex).cgColor
        window.contentView = view
        window.delegate = self
        window.setFrame(screen.frame, display: true)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        print("FS_READY frame=\(Int(screen.frame.width))x\(Int(screen.frame.height))")
        fflush(stdout)

        if !noFullScreen {
            // ウィンドウ表示が落ち着いてから本物のフルスクリーン Space へ遷移する。
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                self?.window.toggleFullScreen(nil)
            }
        }
        if autoQuit > 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + autoQuit) {
                NSApp.terminate(nil)
            }
        }
    }

    func windowDidEnterFullScreen(_ notification: Notification) {
        print("FS_ENTERED")
        fflush(stdout)
    }

    func windowDidExitFullScreen(_ notification: Notification) {
        print("FS_EXITED")
        fflush(stdout)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = Delegate()
app.delegate = delegate
app.run()
