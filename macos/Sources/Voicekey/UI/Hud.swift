//
//  Hud.swift
//  録音中 HUD（画面下部中央の小型ピル）
//
//  方針: 「録音中のみ・小型・情報最小限」。
//  - 録音中: 音声レベル連動の波形バー（auto_enter 時は ⏎ バッジ）
//  - 変換中: スピナー
//  - 通知: エラー・無音検出などを 2 秒だけ表示
//  非アクティベートのフローティングパネルで、クリックは透過し
//  フルスクリーンアプリの上にも表示される。
//

import AppKit
import SwiftUI

/// HUD の表示状態モデル
@MainActor
final class HudModel: ObservableObject {
    enum Mode: Equatable {
        case hidden
        case recording(autoEnter: Bool, handsFree: Bool)
        case transcribing
        case notice(String)
    }

    @Published var mode: Mode = .hidden
    /// 直近の音声レベル履歴（波形バー描画用）
    @Published var levels: [Float] = Array(repeating: 0, count: HudView.barCount)
    /// ストリーミング中のライブ字幕（空なら波形バーを表示）
    @Published var caption: String = ""

    func pushLevel(_ value: Float) {
        levels.removeFirst()
        levels.append(value)
    }

    func resetLevels() {
        levels = Array(repeating: 0, count: HudView.barCount)
    }
}

/// HUD パネル（NSPanel）の管理
@MainActor
final class HudController {

    let model = HudModel()
    private var panel: NSPanel?
    private var noticeTask: Task<Void, Never>?
    /// 直近のアプリ状態。通知が消えるとき、進行中ならその表示へ戻すために保持する
    private var lastState: AppState = .idle

    /// HUD を表示するか（設定で無効化可能）
    var enabled = true

    /// アプリ状態に応じて HUD を更新する
    func update(for state: AppState) {
        lastState = state
        switch state {
        case .idle:
            // 通知表示中は消さない（通知は自身のタイマーで消える）
            if case .notice = model.mode { return }
            hide()
        case .recording(let autoEnter, let handsFree):
            noticeTask?.cancel()
            // 録音中の auto_enter 昇格（ダブルタップ確定）では波形・字幕を維持する
            // （リセットすると表示が一瞬消えて見える）
            if case .recording = model.mode {} else {
                model.resetLevels()
                model.caption = ""  // 新しい録音のたびに字幕をリセット
            }
            model.mode = .recording(autoEnter: autoEnter, handsFree: handsFree)
            show()
        case .transcribing:
            noticeTask?.cancel()
            model.mode = .transcribing
            show()
        }
    }

    /// 一時通知を 2 秒間表示する
    func notice(_ text: String) {
        noticeTask?.cancel()
        model.mode = .notice(text)
        show()
        noticeTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(2))
            guard let self, !Task.isCancelled else { return }
            if case .notice = self.model.mode {
                // 連続録音の変換が進行中なら、隠さず「変換中…」表示へ戻す
                if case .transcribing = self.lastState {
                    self.model.mode = .transcribing
                    self.show()
                } else {
                    self.hide()
                }
            }
        }
    }

    /// 音声レベルを反映する（メインスレッドから呼ぶ）
    func pushLevel(_ value: Float) {
        if case .recording = model.mode {
            model.pushLevel(value)
        }
    }

    /// ストリーミングのライブ字幕を更新する（録音中のみ反映）
    func setCaption(_ text: String) {
        if case .recording = model.mode {
            model.caption = text
        }
    }

    /// ライブ字幕を消す（確定貼り付け後に呼ぶ）
    func clearCaption() {
        model.caption = ""
    }

    // MARK: - パネル管理

    private func show() {
        guard enabled else { return }
        if panel == nil {
            panel = makePanel()
        }
        positionPanel()
        panel?.orderFrontRegardless()
    }

    private func hide() {
        model.mode = .hidden
        panel?.orderOut(nil)
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: HudView.width, height: HudView.height),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .statusBar                    // ほぼすべてのウィンドウより上
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false                     // 影は SwiftUI 側で描く
        panel.ignoresMouseEvents = true             // クリック透過
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [
            .canJoinAllSpaces,                      // 全スペースに表示
            .fullScreenAuxiliary,                   // フルスクリーンアプリの上にも表示
            .ignoresCycle,
        ]
        panel.contentView = NSHostingView(rootView: HudView(model: model))
        return panel
    }

    private func positionPanel() {
        guard let panel, let screen = NSScreen.main else { return }
        let frame = screen.visibleFrame
        let x = frame.midX - HudView.width / 2
        let y = frame.minY + 24  // 画面下部に少し浮かせる
        panel.setFrame(
            NSRect(x: x, y: y, width: HudView.width, height: HudView.height),
            display: true
        )
    }
}

/// HUD の描画（SwiftUI）
struct HudView: View {
    // 幅はライブ字幕が入る余白を確保（ピル自体は内容に応じて縮む）
    static let width: CGFloat = 460
    static let height: CGFloat = 56
    static let barCount = 24
    /// ライブ字幕の最大幅（これを超えると末尾を残して頭を省略表示）
    static let captionMaxWidth: CGFloat = 360
    /// ハンズフリー録音のアクセント色（赤＝通常録音 / 紫＝自動送信 と区別するティール）
    static let handsFreeAccent = Color(red: 0.0, green: 0.78, blue: 0.72)

    @ObservedObject var model: HudModel

    var body: some View {
        ZStack {
            if model.mode != .hidden {
                content
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
                    // macOS 26 は本物のガラスピル、旧 OS は極薄マテリアル近似（描画のみ・待ち時間は足さない）
                    .glassCapsule()
                    .shadow(color: .black.opacity(0.25), radius: 12, y: 4)
                    .transition(.opacity.combined(with: .scale(scale: 0.9)))
            }
        }
        .frame(width: Self.width, height: Self.height)
        .animation(.easeOut(duration: 0.15), value: model.mode)
    }

    @ViewBuilder
    private var content: some View {
        switch model.mode {
        case .hidden:
            EmptyView()

        case .recording(let autoEnter, let handsFree):
            HStack(spacing: 10) {
                // 状態ドット: ハンズフリー=ティール / 自動送信=パープル / 通常=レッド
                Circle()
                    .fill(handsFree ? Self.handsFreeAccent : (autoEnter ? Color.purple : Color.red))
                    .frame(width: 8, height: 8)
                // ハンズフリー中は「いまハンズフリー録音だ」と一目で分かるラベルを出す
                if handsFree {
                    Text("ハンズフリー")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Self.handsFreeAccent)
                        .fixedSize()
                }
                // ストリーミング字幕があればそれを、なければ波形バーを表示。
                // 頭を省略（.head）して常に最新の語尾が見えるようにする
                if model.caption.isEmpty {
                    levelBars
                } else {
                    Text(model.caption)
                        .font(.system(size: 13, weight: .medium))
                        .lineLimit(1)
                        .truncationMode(.head)
                        .frame(maxWidth: Self.captionMaxWidth, alignment: .trailing)
                }
                if autoEnter {
                    Image(systemName: "return")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(.purple)
                }
                // ハンズフリー中は停止方法を控えめに添える（字幕が無いときだけ＝混雑回避）
                if handsFree && model.caption.isEmpty {
                    stopHint
                }
            }

        case .transcribing:
            HStack(spacing: 10) {
                ProgressView()
                    .controlSize(.small)
                Text("変換中…")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
            }

        case .notice(let text):
            Text(text)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }

    /// ハンズフリー停止方法のヒントバッジ（同じホットキーをもう一度押すと停止）
    private var stopHint: some View {
        HStack(spacing: 4) {
            Image(systemName: "stop.fill")
                .font(.system(size: 9, weight: .bold))
            Text("もう一度押すと停止")
                .font(.system(size: 10, weight: .medium))
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(.quaternary, in: Capsule())
        .fixedSize()
    }

    /// 音声レベル連動の波形バー
    private var levelBars: some View {
        HStack(spacing: 2.5) {
            ForEach(0..<Self.barCount, id: \.self) { i in
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(Color.primary.opacity(0.75))
                    .frame(width: 3, height: barHeight(model.levels[i]))
            }
        }
        .animation(.linear(duration: 0.05), value: model.levels)
    }

    private func barHeight(_ level: Float) -> CGFloat {
        let minH: CGFloat = 3
        let maxH: CGFloat = 22
        return minH + (maxH - minH) * CGFloat(min(1, max(0, level)))
    }
}
