//
//  HotkeyRecorderView.swift
//  ホットキー入力欄（クリックしてキーを押すと記録される）
//
//  修飾キーのみのホットキー（右⌘ 単独など）にも対応する:
//  - 通常キーが押されたら「修飾キー + そのキー」で確定
//  - 修飾キーだけが押されて全部離されたら「押されていた修飾キー」で確定
//  - Esc でキャンセル
//

import AppKit
import SwiftUI

struct HotkeyRecorderView: View {
    @Binding var hotkey: [String]

    @State private var isRecording = false
    @State private var heldModifiers: [String] = []
    @State private var monitor: Any?

    var body: some View {
        Button(action: toggleRecording) {
            HStack {
                Text(displayText)
                    .foregroundStyle(isRecording ? Color.accentColor : Color.primary)
                Spacer()
                if isRecording {
                    Text("キーを押してください…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .frame(maxWidth: .infinity)
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(
                        isRecording ? Color.accentColor : Color.secondary.opacity(0.4),
                        lineWidth: 1
                    )
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onDisappear { stopRecording() }
    }

    private var displayText: String {
        if isRecording {
            return heldModifiers.isEmpty
                ? "入力待ち"
                : heldModifiers.map { KeyToken.displayName($0) }.joined(separator: "+")
        }
        return hotkey.isEmpty
            ? "クリックして設定"
            : hotkey.map { KeyToken.displayName($0) }.joined(separator: "+")
    }

    private func toggleRecording() {
        isRecording ? stopRecording() : startRecording()
    }

    private func startRecording() {
        isRecording = true
        heldModifiers = []
        // ローカルモニタ: このアプリ（設定ウィンドウ）にフォーカスがある間のキーを捕捉
        monitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .flagsChanged]) { event in
            handle(event)
            return nil  // 設定画面側にイベントを流さない
        }
    }

    private func stopRecording() {
        if let monitor {
            NSEvent.removeMonitor(monitor)
        }
        monitor = nil
        isRecording = false
        heldModifiers = []
    }

    private func handle(_ event: NSEvent) {
        switch event.type {
        case .flagsChanged:
            // CGEventFlags と NSEvent.modifierFlags はデバイス依存ビットが共通
            let flags = CGEventFlags(rawValue: UInt64(event.modifierFlags.rawValue))
            let current = KeyToken.modifierTokens(from: flags).sorted()
            if current.isEmpty && !heldModifiers.isEmpty {
                // 修飾キーがすべて離された → 修飾キーのみのホットキーとして確定
                commit(heldModifiers)
            } else if !current.isEmpty {
                heldModifiers = current
            }

        case .keyDown:
            if event.keyCode == 53 {  // Esc はキャンセル
                stopRecording()
                return
            }
            guard let token = KeyToken.token(forKeyCode: Int64(event.keyCode)) else { return }
            commit(heldModifiers + [token])

        default:
            break
        }
    }

    private func commit(_ tokens: [String]) {
        hotkey = tokens
        stopRecording()
    }
}
