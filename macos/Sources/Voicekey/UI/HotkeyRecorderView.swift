//
//  HotkeyRecorderView.swift
//  ホットキー入力欄（クリックしてキーを押すと記録される）
//
//  修飾キーのみのホットキー（右⌘ 単独など）にも対応する:
//  - 通常キーが押されたら「修飾キー + そのキー」で確定
//  - 修飾キーだけが押されて全部離されたら「押されていた修飾キー」で確定
//  - Esc は取り消し（元の割り当てのまま捕捉をやめる）。以前は「割り当てなし」で確定していたが、
//    やめるつもりで Esc を押すと割り当てが消えてしまうため。外すときは各行の「クリア」
//    「割り当てを外す」ボタンを使う
//

import AppKit
import SwiftUI

struct HotkeyRecorderView: View {
    @Binding var hotkey: [String]

    /// 未割り当て（空）のときに表示する文言。スロットでは「未割り当て」を渡す
    /// （ハンズフリー切替キー等では「クリックして設定」の CTA を既定とする）。
    var emptyLabel: String = "クリックして設定"

    @State private var isRecording = false
    @State private var heldModifiers: [String] = []
    @State private var monitor: Any?

    var body: some View {
        Button(action: toggleRecording) {
            HStack(spacing: 6) {
                // 割り当て済みのキーはキーキャップ（チップ）で見せる。文字列の「右⌘+Space」より
                // 「どのキーを押すのか」が一目で分かるため（HANDOFF の改善候補「ホットキーのチップ化」）
                if !displayTokens.isEmpty {
                    ForEach(Array(displayTokens.enumerated()), id: \.offset) { _, token in
                        KeyCapChip(text: KeyToken.displayName(token), highlighted: isRecording)
                    }
                } else {
                    Text(placeholderText)
                        .foregroundStyle(isRecording ? Brand.signal : Color.secondary)
                }
                Spacer(minLength: 4)
                if isRecording {
                    Text("ESC で取り消し")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    // 押すと割り当て直せることを示す（チップだけだとただの表示に見えるため）
                    Image(systemName: "keyboard")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .frame(maxWidth: .infinity, minHeight: 30)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(isRecording ? Brand.signal.opacity(0.08) : Color.primary.opacity(0.03))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(
                        isRecording ? Brand.signal : Color.secondary.opacity(0.35),
                        lineWidth: isRecording ? 1.5 : 1
                    )
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onDisappear { stopRecording() }
    }

    /// チップで見せるキー（捕捉中は押されている修飾キー、それ以外は割り当て済みのキー）
    private var displayTokens: [String] {
        isRecording ? heldModifiers : hotkey
    }

    /// キーが 1 つも無いときの文言（捕捉中は「キーを押してください」、未割り当ては emptyLabel）
    private var placeholderText: String {
        isRecording ? "キーを押してください…" : emptyLabel
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
            if event.keyCode == 53 {  // Esc は取り消し（元の割り当てを残す）
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

/// キーキャップ風のチップ（1 キー分）。録音キー・ハンズフリーキーなどの表示とサイドバーで使う。
struct KeyCapChip: View {
    let text: String
    /// 捕捉中など、灯り色で縁取るか
    var highlighted: Bool = false

    var body: some View {
        Text(text)
            .font(.system(size: 12, weight: .semibold, design: .rounded))
            .lineLimit(1)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .frame(minWidth: 24)
            .background(
                RoundedRectangle(cornerRadius: 5)
                    .fill(Color.primary.opacity(0.08))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 5)
                    .strokeBorder(highlighted ? Brand.signal : Color.primary.opacity(0.20), lineWidth: 1)
            )
            // 下辺にだけ薄い影を落として、押せるキーの厚みを出す
            .shadow(color: .black.opacity(0.10), radius: 0, x: 0, y: 1)
    }
}
