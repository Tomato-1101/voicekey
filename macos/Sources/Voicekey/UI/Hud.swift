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
    /// Space / フルスクリーン切替を監視する NSWorkspace オブザーバ（deinit で解除）
    private var spaceObservers: [NSObjectProtocol] = []

    /// HUD を表示するか（設定で無効化可能）
    var enabled = true
    /// 待機中も小型ピルを常時表示するか（config.hudAlwaysVisible）
    var alwaysVisible = false

    init() {
        // パネルは全 Space に居座る（.canJoinAllSpaces + .fullScreenAuxiliary）ため、Space や
        // フルスクリーンを切り替えると HudBackdrop（背後サンプルのガラス）が旧 Space の内容のまま
        // 固まり、次の録音（mode 変化＝再レイアウト）まで映り込みが古いまま、という事象がある。
        // タイマーの常時ポーリングはアイドル時の電池を食うので張らず、Space 切替・アプリ切替の
        // イベントで、表示中パネルの再評価（refreshBackdrop）だけをその都度蹴る。
        let nc = NSWorkspace.shared.notificationCenter
        for name in [
            NSWorkspace.activeSpaceDidChangeNotification,
            NSWorkspace.didActivateApplicationNotification,
        ] {
            let token = nc.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.refreshBackdrop() }
            }
            spaceObservers.append(token)
        }
    }

    deinit {
        let nc = NSWorkspace.shared.notificationCenter
        for token in spaceObservers { nc.removeObserver(token) }
    }

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
                model.mode = .idlePill
                show()
            } else {
                hide()
            }
        case .recording(let autoEnter, let handsFree):
            noticeTask?.cancel()
            // 録音中の auto_enter 昇格（ダブルタップ確定）では波形を維持する
            // （リセットすると表示が一瞬消えて見える）
            if case .recording = model.mode {} else {
                model.resetLevels()
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
        // パネル下端をほぼ画面下端に置く。カプセルはパネル内で下端＋6pt に寄せてあるので、
        // 実際のカプセル下端は画面下端（Dock 上端）+ 約 8pt に固定される。
        let y = frame.minY + 2
        panel.setFrame(
            NSRect(x: x, y: y, width: HudView.width, height: HudView.height),
            display: true
        )
    }

    /// Space / フルスクリーン切替の通知を受けて、表示中のバックドロップを再評価する。
    /// パネルが全 Space に居座る都合で、切替後に旧 Space の映り込みで固まることがあるため、
    /// mode 変化と同じ再評価パス（frame 再適用 + 再レイアウト/再描画）をイベント駆動で強制する。
    private func refreshBackdrop() {
        guard let panel, panel.isVisible, model.mode != .hidden else { return }
        // 1) 正規位置の frame を再適用（setFrame display:true）
        positionPanel()
        // 2) contentView 配下（HudBackdrop の NSGlassEffectView / NSVisualEffectView を含む）を
        //    再レイアウト・再描画させる
        if let content = panel.contentView { markNeedsRefresh(content) }
        // 3) 同一 frame の再適用だけでは背後の再サンプルが走らない環境に備え、0.5pt ずらして
        //    即戻す微小 nudge を入れる（同一イベント内で 2 回 setFrame・視覚的に知覚不能）。
        let f = panel.frame
        panel.setFrame(f.offsetBy(dx: 0, dy: 0.5), display: true)
        panel.setFrame(f, display: true)
    }

    /// ビュー階層を辿って needsLayout / needsDisplay を立て、次のパスで再描画させる
    private func markNeedsRefresh(_ view: NSView) {
        view.needsLayout = true
        view.needsDisplay = true
        for sub in view.subviews { markNeedsRefresh(sub) }
    }
}

/// HUD の描画（SwiftUI）
struct HudView: View {
    // 幅はカプセルが最大に育っても収まる余白（ピル自体は内容に応じて縮む）。
    // パネルは透明＋クリック透過なので、余った幅による実害はない。
    static let width: CGFloat = 460
    static let height: CGFloat = 56
    static let barCount = 24
    /// ハンズフリー録音のアクセント色（赤＝通常録音 / 紫＝自動送信 と区別するティール）
    static let handsFreeAccent = Color(red: 0.0, green: 0.78, blue: 0.72)

    @ObservedObject var model: HudModel

    /// 中身（アイコン/波形/変換マーク）の実測サイズ。これに padding を足した値を
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
                    // 背景は「本物のガラス」を AppKit のバックドロップ系ビューで敷く（HudBackdrop）。
                    // なぜ SwiftUI の glassEffect / Material を使わないか: これらは同一ウィンドウ内の
                    // 背後コンテンツしかサンプルできず、中身が透明な HUD パネルでは何もブラーできず
                    // ガラスの素材色（濃いグレーの不透明な塊）だけが出てしまう。HUD は他アプリの上に
                    // 重なるが、その「ウィンドウ越しの背景」はこれらの API では取得できない。
                    // 対して AppKit のバックドロップ系（macOS 26=NSGlassEffectView / 旧OS=NSVisualEffectView
                    // の .behindWindow）は Dock やメニューバー HUD と同じ仕組みでウィンドウ越しに背後を
                    // サンプルできるため、他アプリのウィンドウが透けて（歪んで）映り込む本物のガラスになる。
                    // カプセル形・アニメ中のサイズ追従は HudBackdrop 側で、リム・ハイライトはここで重ねる。
                    .background {
                        HudBackdrop()
                            .clipShape(Capsule())
                            // 既存のリム（.primary 由来なのでライト/ダーク両対応）
                            .overlay(Capsule().strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.5))
                            // 上縁の極薄い白ハイライトでレンズの立ち上がりを補う（フォールバックの
                            // すりガラスは屈折歪みが出ないため特に効く。ダークでも破綻しない控えめ値）。
                            .overlay(
                                Capsule().strokeBorder(
                                    LinearGradient(
                                        colors: [Color.white.opacity(0.22), Color.white.opacity(0.0)],
                                        startPoint: .top, endPoint: .bottom
                                    ),
                                    lineWidth: 0.75
                                )
                            )
                    }
                    .shadow(color: .black.opacity(isIdlePill ? 0.15 : 0.25), radius: isIdlePill ? 6 : 12, y: isIdlePill ? 2 : 4)
                    // 中身はカプセルの上に重ね、mode 間では opacity クロスフェードのみで出入りさせる
                    // （scale を使わない＝「消して出し直す」感を排除）。カプセルからはみ出さないよう
                    // カプセル形にクリップし、育つ／広がるのに合わせて中身が現れるようにする。
                    .overlay {
                        modeContent(for: model.mode)
                            .fixedSize()
                            // 「カプセルが伸び、伸びた先に中身が現れる」順序を作るための非対称フェード。
                            // 挿入は少し遅らせてフェードイン（カプセルが育ち始めてから新中身が出る）、
                            // 削除は先に速く消す（旧中身を残したまま育たせない）。
                            // 録音→変換中はカプセルサイズを凍結する（下の onPreferenceChange 参照）ため、
                            // 「変換中…」は動かないカプセルの中でその場クロスフェードになる。
                            .transition(.asymmetric(
                                insertion: .opacity.animation(.easeIn(duration: 0.12).delay(0.06)),
                                removal: .opacity.animation(.easeOut(duration: 0.08))
                            ))
                            .frame(width: capsuleWidth, height: capsuleHeight)
                            .clipShape(Capsule())
                    }
                    // この transition は hidden⇄表示（HUD 自体の出現・消滅）のときだけ発火する。
                    // mode 間の切替では上位の if が真のまま＝ identity を保つので発火しない。
                    .transition(.opacity.combined(with: .scale(scale: 0.8)))
                    // カプセルを下端に寄せ、下端から 6pt だけ浮かせる（外側 frame を alignment:.bottom に
                    // した上でこのパディングを効かせることで、どのモードでもカプセル下端が画面下端の
                    // すぐ上に揃う。モーフィングは下端アンカーで上に育つ＝Dock 的で自然）。
                    .padding(.bottom, 6)
            }
        }
        // 外側パネル（460×56）内でカプセルを下揃えにする（極小の待機ピルでも下端が浮かない）。
        .frame(width: Self.width, height: Self.height, alignment: .bottom)
        // カプセルのサイズ決定に使う不可視サイザー（active mode の中身の素の大きさを測る）
        .background { sizer }
        // 待機ピル⇄録音インジケーターの連続変形（mode 変化）。所要時間は伸ばさず spring 表現だけで出す。
        // dampingFraction を高め（0.82）にして「行き過ぎて戻る」バウンスを消し、ぬるっと一方向に育たせる。
        .animation(.spring(response: 0.3, dampingFraction: 0.82), value: model.mode)
        // 実測サイズの変化を spring でカプセル frame に反映する（＝カプセルがサイズだけ連続変形する本体）。
        .animation(.spring(response: 0.3, dampingFraction: 0.82), value: contentSize)
        // 変換中はカプセルサイズを凍結する: 録音（波形）→変換中（アイコン＋文字）でサイズが
        // 縮むと、中央寄せの中身が横に流れて見える（ユーザー指摘「左右に動く」）。変換中は直前の
        // 録音カプセルのまま動かさず、中身だけをその場でクロスフェード＋明滅させる。
        // （変換中→次の録音/通知/待機ピルでは通常どおり実測サイズへ変形する）
        .onPreferenceChange(HudContentSizeKey.self) { if !isTranscribing { contentSize = $0 } }
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
            // 待機中の常時表示ピル（中身なしの極小ピル・本当に小さい横長）。
            // 録音開始でこのカプセルがそのまま大きく育って録音インジケーターになる（モーフの起点）。
            Color.clear
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
            // 音声レベル連動の波形バー
            levelBars
            if autoEnter {
                Image(systemName: "return")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(.purple)
            }
            // ハンズフリー中は停止方法を控えめに添える
            if handsFree {
                stopHint
            }
        }
    }

    /// 変換中の中身。揺れる waveform マークは廃止し、「変換中…」の文字自体をゆっくり明滅させる
    private var transcribingContent: some View {
        HStack(spacing: 10) {
            appIconView  // 貼り付け先アプリのアイコン（左端・残す）
            // 「変換中…」の文字を opacity 1.0⇄0.35 でゆっくり往復させる明滅のみ（scale は使わない＝
            // サイズが揺れる「ふわんふわん揺れ」を撤去し、その場で明るさだけが呼吸する落ち着いた明滅）。
            // repeatForever は transcribing の間だけ（isTranscribing で切替）駆動し、離脱時は非反復
            // アニメに切り替えて確実に止める＝非表示中に裏で回り続けて CPU を食わないようにする。
            Text("変換中…")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)
                .opacity(pulseOn ? 0.35 : 1.0)
                .animation(
                    isTranscribing
                        ? .easeInOut(duration: 0.8).repeatForever(autoreverses: true)
                        : .easeInOut(duration: 0.2),
                    value: pulseOn
                )
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

// MARK: - ガラス背景（AppKit バックドロップ）

/// HUD ピルの「本物のガラス」背景。SwiftUI の glassEffect は同一ウィンドウ内しかサンプル
/// できないが、AppKit のバックドロップ系ビューはウィンドウ越しに背後（他アプリのウィンドウ）を
/// サンプルできる。macOS 26 では NSGlassEffectView（屈折込みの Liquid Glass）を使い、
/// それ未満・または VOICEKEY_GLASS_FALLBACK=1 のときは NSVisualEffectView の .behindWindow
/// （すりガラス）にフォールバックする。
///
/// 注意: パネルは全 Space に居座る（.canJoinAllSpaces + .fullScreenAuxiliary）。既定の
/// NSVisualEffectView(.behindWindow) は毎フレーム背後を再合成する＝背景の変化にリアルタイム
/// 追従する。HudController の Space 切替・アプリ切替通知での refreshBackdrop()（frame 再適用＋
/// 再描画）は、Space 切替直後の取りこぼしに対する防御として残している。
private struct HudBackdrop: NSViewRepresentable {
    /// NSGlassEffectView（Liquid Glass）を試す実験フラグ。既定は常に NSVisualEffectView。
    /// 理由: NSGlassEffectView の背後サンプルは実質スナップショットで、背後の内容が変わっても
    /// 次の再レイアウトまで古い映り込みのまま固まる（ユーザーの目視で確認済み。screencapture は
    /// 撮影自体が再合成を誘発するため、スクショ検証では常に「追従している」ように見えて欺かれる）。
    /// NSVisualEffectView(.behindWindow) は Dock・メニューバーと同じ合成経路で毎フレーム更新＝真のライブ。
    private var experimentalLiquid: Bool {
        ProcessInfo.processInfo.environment["VOICEKEY_HUD_LIQUID"] == "1"
    }

    func makeNSView(context: Context) -> NSView {
        if #available(macOS 26.0, *), experimentalLiquid {
            return GlassPillBackdrop(frame: .zero)
        } else {
            return VisualEffectPillBackdrop(frame: .zero)
        }
    }

    /// レイアウト追従（カプセルの角丸・マスク）は各ビューの layout() が bounds から自律的に
    /// 更新するため、ここでは何もしない。updateNSView 頼みにしないのは、SwiftUI の spring で
    /// bounds が毎フレーム連続変化するため、layout() で拾う方が取りこぼしなく滑らかに追従するから。
    func updateNSView(_ nsView: NSView, context: Context) {}
}

/// macOS 26 の本物のガラス（AppKit Liquid Glass）。屈折は出るが背後サンプルが
/// スナップショットで固まるため既定では使わない（VOICEKEY_HUD_LIQUID=1 の実験用に残置）。
/// カプセル形を保つため、bounds に合わせて layout() ごとに cornerRadius を height/2 に更新する。
@available(macOS 26.0, *)
private final class GlassPillBackdrop: NSGlassEffectView {
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        style = .clear      // より強く透ける（レンズ感重視・tint なし）
        tintColor = nil
        // contentView は空のまま（背後サンプルだけを使い、HUD の中身は SwiftUI 側で上に重ねる）
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func layout() {
        super.layout()
        cornerRadius = bounds.height / 2   // 常にカプセル形（アニメ中も追従）
    }
}

/// 既定のすりガラス。.behindWindow でウィンドウ越しの背後をブラーする（毎フレーム更新＝
/// 背後の変化にリアルタイム追従する唯一の公開 API）。カプセル形は maskImage で与え、
/// 高さが変わったときだけ作り直す（幅方向の変化は capInsets の stretch で吸収＝ちらつかない）。
private final class VisualEffectPillBackdrop: NSVisualEffectView {
    /// 直近に maskImage を生成した高さ。高さが実質変わったときだけ作り直す。
    private var maskHeight: CGFloat = -1

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        material = .hudWindow
        blendingMode = .behindWindow    // ウィンドウの「後ろ」＝背後アプリをぼかす
        state = .active
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func layout() {
        super.layout()
        let h = bounds.height
        // 高さが実質変わったときだけマスクを作り直す（幅変化は capInsets で吸収）
        if abs(h - maskHeight) > 0.5 {
            maskHeight = h
            maskImage = Self.capsuleMask(height: h)
        }
    }

    /// カプセル形のマスク画像。左右に radius 幅の cap を残し中央 1px を横 stretch させることで、
    /// 幅が変わってもこの 1 枚で角丸カプセルを保てる（NSVisualEffectView 推奨の resizable マスク）。
    private static func capsuleMask(height: CGFloat) -> NSImage {
        let radius = max(1, height / 2)
        let width = radius * 2 + 1
        let size = NSSize(width: width, height: height)
        let image = NSImage(size: size, flipped: false) { rect in
            let path = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
            NSColor.black.setFill()
            path.fill()
            return true
        }
        image.capInsets = NSEdgeInsets(top: 0, left: radius, bottom: 0, right: radius)
        image.resizingMode = .stretch
        return image
    }
}
