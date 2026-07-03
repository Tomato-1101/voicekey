//
//  FeedbackView.swift
//  フィードバック入力フォーム
//
//  メニューバーの「フィードバックを送る…」から開く小さな入力フォーム。
//  本文を自社サーバー（/api/v1/feedback）へ送る。ログイン済みならアカウントに
//  紐づき、未ログインでも匿名（device_id + app_version）で送れる。
//  送信成功/失敗を画面に明示する（誤送信防止より「送れた確証」を優先）。
//

import SwiftUI

struct FeedbackView: View {

    /// ウィンドウを閉じるコールバック（NSWindow の管理は呼び出し側で行う）
    let onClose: () -> Void

    @State private var message = ""
    @State private var phase: Phase = .editing

    /// 送信フローの状態。.failed は表示用のエラーメッセージを持つ
    private enum Phase: Equatable {
        case editing
        case sending
        case sent
        case failed(String)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("フィードバックを送る")
                .font(.headline)
            Text("ご意見・不具合・要望をお寄せください。開発の参考にします。")
                .font(.callout)
                .foregroundStyle(.secondary)
            Text(BackendClient.isLoggedIn
                 ? "お使いのアカウントに紐づけて送信されます。"
                 : "未ログインのため匿名で送信されます。")
                .font(.caption)
                .foregroundStyle(.tertiary)

            TextEditor(text: $message)
                .font(.body)
                .frame(minHeight: 140)
                // 既定の不透明背景を消してウィンドウの下地を透かす（ガラス化）
                .scrollContentBackground(.hidden)
                .padding(4)
                // 入力欄と分かるよう極薄の下地を敷く（下地が透けすぎて枠だけに見えるのを防ぐ）
                .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 6))
                .overlay(
                    RoundedRectangle(cornerRadius: 6)
                        .stroke(Color.secondary.opacity(0.3))
                )
                // 送信中・送信後は編集不可（再送・二重送信を防ぐ）
                .disabled(phase == .sending || phase == .sent)

            statusLine

            HStack {
                Spacer()
                Button(phase == .sent ? "閉じる" : "キャンセル") { onClose() }
                if phase != .sent {
                    Button("送信") { submit() }
                        .glassProminentButton()  // 主要アクション（accent 色ガラス）
                        .keyboardShortcut(.defaultAction)
                        .disabled(trimmed.isEmpty || phase == .sending)
                }
            }
        }
        .padding(20)
        .frame(width: 420)
        .glassButtons()             // 配下の Button を一括ガラス化（.plain 明示ボタンは影響なし）
        .frostedWindowBackground()  // ウィンドウ全面のすりガラス下地
    }

    /// 送信状態・結果の表示行
    @ViewBuilder
    private var statusLine: some View {
        switch phase {
        case .sending:
            Label("送信中…", systemImage: "paperplane")
                .font(.caption)
                .foregroundStyle(.secondary)
        case .sent:
            Label("送信しました。ありがとうございます！", systemImage: "checkmark.circle.fill")
                .font(.caption)
                .foregroundStyle(.green)
        case .failed(let msg):
            Label(msg, systemImage: "exclamationmark.triangle.fill")
                .font(.caption)
                .foregroundStyle(.red)
        case .editing:
            EmptyView()
        }
    }

    /// 前後の空白を除いた本文
    private var trimmed: String {
        message.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 送信を実行する（成功なら .sent、失敗なら .failed に遷移）
    private func submit() {
        let text = trimmed
        guard !text.isEmpty else { return }
        phase = .sending
        Task { @MainActor in
            do {
                try await BackendClient.submitFeedback(text)
                phase = .sent
            } catch {
                let msg = (error as? BackendClient.BackendError)?.userMessage
                    ?? "送信に失敗しました"
                phase = .failed(msg)
            }
        }
    }
}
