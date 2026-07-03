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
        /// 待機中も常時表示する小型ピル（mic アイコンのみ・薄め）
        case idlePill
        case recording(autoEnter: Bool, handsFree: Bool)
        case transcribing
        case notice(String)
    }

    @Published var mode: Mode = .hidden
    /// 直近の音声レベル履歴（波形バー描画用）
    @Published var levels: [Float] = Array(repeating: 0, count: HudView.barCount)
    /// ストリーミング中のライブ字幕（空なら波形バーを表示）
    @Published var caption: String = ""
    /// 貼り付け先アプリのアイコン（録音中/変換中にピル左端へ表示。nil なら非表示）
    @Published var appIcon: NSImage?

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
    /// 待機中も小型ピルを常時表示するか（config.hudAlwaysVisible）
    var alwaysVisible = false

    /// アプリ状態に応じて HUD を更新する
    func update(for state: AppState) {
        lastState = state
        switch state {
        case .idle:
            // 通知表示中は消さない（通知は自身のタイマーで消える）
            if case .notice = model.mode { return }
            // 常時表示 ON なら待機中も小型ピル（mic のみ）を残す
            if alwaysVisible {
                model.appIcon = nil  // 待機中は貼り付け先アイコンを出さない
                model.caption = ""
                model.mode = .idlePill
                show()
            } else {
                hide()
            }
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

    /// 貼り付け先アプリのアイコンを設定する（録音開始時にスナップショットを渡す）
    func setAppIcon(_ icon: NSImage?) {
        model.appIcon = icon
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

    /// 中身（アイコン/波形/字幕/変換マーク）の実測サイズ。これに padding を足した値を
    /// カプセルの .frame に spring で反映することで、mode が変わってもカプセルは削除・挿入
    /// されず「サイズだけ」が連続変形する（＝形が飛ばない）。初期値は待機ピル相当にしておく。
    @State private var contentSize: CGSize = CGSize(width: 40, height: 8)
    /// 変換中マークの明滅トグル。transcribing に入った瞬間だけ true にして往復を開始し、
    /// 離脱で false に戻す（repeatForever を確実に止め、非表示中に裏で回し続けないため）。
    @State private var pulseOn = false

    /// 待機ピルか（極小サイズ・薄めの mic のみ）
    private var isIdlePill: Bool { model.mode == .idlePill }
    /// 変換中か（明滅アニメを駆動してよいかの判定に使う）
    private var isTranscribing: Bool { model.mode == .transcribing }
    // 待機ピルは極小の横長、録音/変換中は通常サイズ。この padding 差でモーフの「育ち」を出す
    private var horizontalPadding: CGFloat { isIdlePill ? 12 : 16 }
    private var verticalPadding: CGFloat { isIdlePill ? 3 : 8 }
    /// カプセルの目標サイズ（実測した中身＋左右/上下の padding）
    private var capsuleWidth: CGFloat { contentSize.width + horizontalPadding * 2 }
    private var capsuleHeight: CGFloat { contentSize.height + verticalPadding * 2 }

    var body: some View {
        ZStack {
            if model.mode != .hidden {
                // 単一 identity のガラスカプセル。中身を switch で差し替えず、カプセル自体は
                // 常に「Color.clear.frame(実測サイズ) の背景」として同じ identity を保つ。
                // mode 間ではこの frame の width/height だけが spring 補間される＝連続変形になる
                // （旧実装は switch した中身に直接カプセルを付けていたため、削除＋挿入の transition
                // が毎回発火して「消して出し直す・形が飛ぶ」感が出ていた。そこを identity 固定で断つ）。
                Color.clear
                    .frame(width: capsuleWidth, height: capsuleHeight)
                    // macOS 26 は本物のガラスピル、旧 OS は極薄マテリアル近似（描画のみ・待ち時間は足さない）
                    .glassCapsule()
                    .shadow(color: .black.opacity(isIdlePill ? 0.15 : 0.25), radius: isIdlePill ? 6 : 12, y: isIdlePill ? 2 : 4)
                    // 中身はカプセルの上に重ね、mode 間では opacity クロスフェードのみで出入りさせる
                    // （scale を使わない＝「消して出し直す」感を排除）。カプセルからはみ出さないよう
                    // カプセル形にクリップし、育つ／広がるのに合わせて中身が現れるようにする。
                    .overlay {
                        modeContent(for: model.mode)
                            .fixedSize()
                            .transition(.opacity)
                            .frame(width: capsuleWidth, height: capsuleHeight)
                            .clipShape(Capsule())
                    }
                    // この transition は hidden⇄表示（HUD 自体の出現・消滅）のときだけ発火する。
                    // mode 間の切替では上位の if が真のまま＝ identity を保つので発火しない。
                    .transition(.opacity.combined(with: .scale(scale: 0.8)))
            }
        }
        .frame(width: Self.width, height: Self.height)
        // カプセルのサイズ決定に使う不可視サイザー（active mode の中身の素の大きさを測る）
        .background { sizer }
        // 待機ピル⇄録音インジケーターの連続変形（mode 変化）。所要時間は伸ばさず spring 表現だけで出す。
        .animation(.spring(response: 0.3, dampingFraction: 0.6), value: model.mode)
        // 波形バー⇄字幕の切替は「空⇄非空」を境に 1 回だけアニメする
        //（caption 文字列そのものを value にすると毎文字アニメが走るため、isEmpty だけを見る）。
        .animation(.spring(response: 0.3, dampingFraction: 0.6), value: model.caption.isEmpty)
        // 実測サイズの変化を spring でカプセル frame に反映する（＝カプセルがサイズだけ連続変形する本体）。
        .animation(.spring(response: 0.3, dampingFraction: 0.6), value: contentSize)
        .onPreferenceChange(HudContentSizeKey.self) { contentSize = $0 }
        // transcribing に入った瞬間に往復開始、離脱で停止（明滅を非表示中に裏で回し続けない）。
        .onChange(of: isTranscribing) { _, active in pulseOn = active }
    }

    /// カプセルのサイズ決定に使う不可視サイザー。active mode の中身だけを素のサイズで描画して測る。
    /// ここでアニメを走らせると遷移中に旧新の中身が重なって union サイズになり、カプセルが一瞬
    /// 膨らむため、測定はアニメ無効（transaction.animation = nil）で即時に確定させる。
    private var sizer: some View {
        modeContent(for: model.mode)
            .fixedSize()
            .background {
                GeometryReader { geo in
                    Color.clear.preference(key: HudContentSizeKey.self, value: geo.size)
                }
            }
            .opacity(0)
            .allowsHitTesting(false)
            .transaction { $0.animation = nil }
    }

    /// mode ごとの中身。カプセルの上に重ねる本体と、サイザーの測定対象で共用する。
    @ViewBuilder
    private func modeContent(for mode: HudModel.Mode) -> some View {
        switch mode {
        case .hidden:
            EmptyView()

        case .idlePill:
            // 待機中の常時表示ピル（本当に小さい横長・mic のみ・薄め）。
            // 録音開始でこのピルがそのまま大きく育って録音インジケーターになる（モーフの起点）。
            Image(systemName: "mic.fill")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(.secondary)
                .opacity(0.5)
                .frame(width: 40, height: 8)  // 余白込みで概ね 64×14 の極小ピルにする

        case .recording(let autoEnter, let handsFree):
            recordingContent(autoEnter: autoEnter, handsFree: handsFree)

        case .transcribing:
            transcribingContent

        case .notice(let text):
            Text(text)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }

    /// 貼り付け先アプリのアイコン（18pt 角丸）。取得失敗時は非表示でレイアウトを崩さない
    @ViewBuilder
    private var appIconView: some View {
        if let icon = model.appIcon {
            Image(nsImage: icon)
                .resizable()
                .frame(width: 18, height: 18)
                .clipShape(RoundedRectangle(cornerRadius: 4))
        }
    }

    /// 録音中の中身（アプリアイコン・状態ドット・波形／字幕・各バッジ）
    @ViewBuilder
    private func recordingContent(autoEnter: Bool, handsFree: Bool) -> some View {
        HStack(spacing: 10) {
            appIconView  // 貼り付け先アプリのアイコン（左端）
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
            // 字幕が最初に届いたら固定幅 360 へ一度だけ広げ、以後の逐次更新では幅を動かさない。
            // 固定幅（maxWidth ではなく width）にするのは、短い字幕でも幅が伸縮せず毎文字の
            // ガタつき・再アニメを防ぐため。頭を省略（.head）して常に最新の語尾を見せる。
            if model.caption.isEmpty {
                levelBars
            } else {
                Text(model.caption)
                    .font(.system(size: 13, weight: .medium))
                    .lineLimit(1)
                    .truncationMode(.head)
                    .frame(width: Self.captionMaxWidth, alignment: .trailing)
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
    }

    /// 変換中の中身。スピナー（くるくる）は廃止し、waveform マークを「ふわんふわん」明滅させる
    private var transcribingContent: some View {
        HStack(spacing: 10) {
            appIconView  // 貼り付け先アプリのアイコン（左端）
            // opacity 1.0⇄0.35 とごく僅かな scale 0.92⇄1.0 をゆっくり往復させ、柔らかい明滅にする。
            // repeatForever は transcribing の間だけ（isTranscribing で切替）駆動し、離脱時は非反復
            // アニメに切り替えて確実に止める＝非表示中に裏で回り続けて CPU を食わないようにする。
            Image(systemName: "waveform")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.secondary)
                .opacity(pulseOn ? 0.35 : 1.0)
                .scaleEffect(pulseOn ? 0.92 : 1.0)
                .animation(
                    isTranscribing
                        ? .easeInOut(duration: 0.8).repeatForever(autoreverses: true)
                        : .easeInOut(duration: 0.2),
                    value: pulseOn
                )
            Text("変換中…")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)
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

/// 中身の実測サイズをカプセルへ伝える PreferenceKey。
/// .zero（未測定）は無視して有効値だけを採用し、初期化時の 0 で潰さないようにする。
private struct HudContentSizeKey: PreferenceKey {
    static let defaultValue: CGSize = .zero
    static func reduce(value: inout CGSize, nextValue: () -> CGSize) {
        let next = nextValue()
        if next != .zero { value = next }
    }
}
