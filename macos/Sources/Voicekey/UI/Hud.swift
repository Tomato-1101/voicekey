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
import QuartzCore  // CAMediaTimingFunction（位置追従アニメの easeOut カーブ）
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
    /// 画面構成変化（Dock 出没・解像度変更・外部ディスプレイ着脱）を監視するオブザーバ（deinit で解除）
    private var screenObserver: NSObjectProtocol?

    /// デバッグ用: 録音→変換中→待機を実タイマーで循環駆動する検証ハーネス（VOICEKEY_HUD_DEBUG_STATE=cycle）。
    /// 定常状態では再現しない「遷移中・連続入力での横揺れ」を実録音なしに駆動して計測するために使う。
    /// App Nap 下でも確実に発火させるため beginActivity で抑止する（検証中のみ・既定挙動は不変）。
    private var debugCycleTimer: Timer?
    private var debugActivity: NSObjectProtocol?
    private var debugCyclePhase = 0            // 0=録音 / 1=変換中 / 2=待機
    private var debugCyclePhaseStart = Date()

    /// デバッグ用の固定表示状態（環境変数 VOICEKEY_HUD_DEBUG_STATE）。
    /// 設定されている間は実状態（update(for:)）で上書きせず、この状態を出しっぱなしにして
    /// HUD の見え方（変換中の横揺れ・フルスクリーン時の待機ピル可否等）を目視・スクショで
    /// 検証するための最小ハーネス。値: transcribing / recording / idle（それ以外・未設定は nil＝通常動作）。
    private var debugState: HudModel.Mode? {
        switch ProcessInfo.processInfo.environment["VOICEKEY_HUD_DEBUG_STATE"] {
        case "transcribing": return .transcribing
        case "recording": return .recording(autoEnter: false, handsFree: false)
        case "idle": return .idlePill
        default: return nil
        }
    }

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
            // 重要: ここは同期の assumeIsolated で refreshBackdrop を即実行する（背景・App Nap 下でも
            // Task/asyncAfter に頼らず確実に走らせるため）。フルスクリーン時の待機ピル非表示は OS の
            // collectionBehavior に委ねたので、ここはガラス背景の再サンプルだけを担う。
            let token = nc.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.refreshBackdrop() }
            }
            spaceObservers.append(token)
        }

        // Dock（タスクバー相当）の自動非表示 ON/OFF・解像度変更・外部ディスプレイ着脱で
        // visibleFrame が変わる＝ピルの正しい縦位置がずれる。表示中なら位置を再計算して
        // 滑らかに追従させる（SideNotch と同じ didChangeScreenParameters 経路）。
        screenObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.handleScreenChange() }
        }
    }

    deinit {
        let nc = NSWorkspace.shared.notificationCenter
        for token in spaceObservers { nc.removeObserver(token) }
        if let screenObserver { NotificationCenter.default.removeObserver(screenObserver) }
    }

    /// 環境変数 VOICEKEY_HUD_DEBUG_STATE が指定されていれば HUD を固定状態で強制表示する。
    /// AppController の初期化から一度だけ呼ぶ検証専用フック（未設定なら何もせず false）。
    /// 変換/録音はアプリアイコン込みの見た目を確認したいのでダミーアイコンを立てる。
    @discardableResult
    func applyDebugStateIfNeeded() -> Bool {
        // cycle: 録音→変換中→待機を循環駆動して遷移中の横揺れ等を計測する（検証専用ハーネス）
        if ProcessInfo.processInfo.environment["VOICEKEY_HUD_DEBUG_STATE"] == "cycle" {
            startDebugCycle()
            return true
        }
        guard let debugState else { return false }
        enabled = true
        if debugState != .idlePill { model.appIcon = NSApp.applicationIconImage }
        model.mode = debugState
        show()
        return true
    }

    /// 録音→変換中→待機を実タイマーで循環させる検証ハーネス（VOICEKEY_HUD_DEBUG_STATE=cycle 専用）。
    /// 実際のディクテーション連打（録音のたびに変換中を挟む）に相当する遷移を再現し、遷移中・
    /// 連続入力での「変換中…」の横揺れを screencapture で計測できるようにする。
    private func startDebugCycle() {
        enabled = true
        // 背景常駐の App Nap でタイマーが沈黙しないよう抑止する（検証中のみ）
        debugActivity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiated, .idleSystemSleepDisabled], reason: "HUD debug cycle")
        debugCyclePhase = 0
        debugCyclePhaseStart = Date()
        model.appIcon = NSApp.applicationIconImage
        model.resetLevels()
        model.mode = .recording(autoEnter: false, handsFree: false)
        show()
        let t = Timer(timeInterval: 0.05, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tickDebugCycle() }
        }
        RunLoop.main.add(t, forMode: .common)
        debugCycleTimer = t
    }

    /// cycle ハーネスの毎チック処理。フェーズの経過時間で録音/変換中/待機を切り替える。
    private func tickDebugCycle() {
        let elapsed = Date().timeIntervalSince(debugCyclePhaseStart)
        switch debugCyclePhase {
        case 0:  // 録音（約1.0秒）: 波形を動かしてカプセルを大きく育てる
            model.pushLevel(Float.random(in: 0.15...0.9))
            if elapsed > 1.0 {
                debugCyclePhase = 1
                debugCyclePhaseStart = Date()
                model.mode = .transcribing  // ここで録音(大)→変換中(小)のモーフが発火する
                show()
            }
        case 1:  // 変換中（約1.6秒＝明滅2往復ぶん）: この間の横揺れを計測する
            if elapsed > 1.6 {
                debugCyclePhase = 2
                debugCyclePhaseStart = Date()
                model.appIcon = nil          // 待機は貼り付け先アイコンを出さない（実挙動と同じ）
                model.mode = .idlePill
                show()
            }
        default:  // 待機（約0.5秒）→ 次の録音へ
            if elapsed > 0.5 {
                debugCyclePhase = 0
                debugCyclePhaseStart = Date()
                model.appIcon = NSApp.applicationIconImage
                model.resetLevels()
                model.mode = .recording(autoEnter: false, handsFree: false)
                show()
            }
        }
    }

    /// アプリ状態に応じて HUD を更新する
    func update(for state: AppState) {
        // デバッグ固定表示中（VOICEKEY_HUD_DEBUG_STATE）は実状態で上書きしない
        if debugState != nil { return }
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
        // フルスクリーン Space での待機ピル非表示は OS の Space 管理に委ねる（applyFullScreenPolicy）。
        // 録音/変換/通知はフルスクリーンでも出す（ユーザー操作起点なので塞いでよい）。
        applyFullScreenPolicy()
        positionPanel()
        panel?.orderFrontRegardless()
    }

    /// パネルのフルスクリーン可否を collectionBehavior で切り替える（検出・タイマー不要で確実）。
    /// - 待機ピル: `.canJoinAllSpaces` を付けない → 他アプリのフルスクリーン Space に OS が重ねない
    ///   （＝フルスクリーン中は自動で消える）。現在の Space にだけ出る。Space 切替時は refreshBackdrop
    ///   から order し直して通常デスクトップへ追従させる（フルスクリーンには OS が出さないので安全）。
    /// - 録音/変換/通知: `.canJoinAllSpaces + .fullScreenAuxiliary` → フルスクリーン上にも出す
    ///   （フルスクリーンアプリでもディクテーションするため。ここは従来どおり全 Space 常駐）。
    /// 【なぜ collectionBehavior にしたか】旧実装は CGWindowList で全画面被覆を検出し asyncAfter で
    /// 再評価していたが、常駐 App Nap 下では遅延再評価が沈黙し（遷移アニメ中の同期評価も全画面未確定で
    /// false）待機ピルが出っぱなしになっていた。実測でも `.canJoinAllSpaces` を外すだけでフルスクリーン
    /// 上の待機ピルが消える（`.fullScreenAuxiliary` を外すだけでは canJoinAllSpaces が全 Space へ
    /// 強制表示するため消えなかった）。OS の Space 属性に委ねてタイミングレースを根絶する。
    private func applyFullScreenPolicy() {
        guard let panel else { return }
        if model.mode == .idlePill {
            panel.collectionBehavior = [.ignoresCycle]
        } else {
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        }
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
        // 初期値。実際のフルスクリーン可否は show() 時に applyFullScreenPolicy() が mode に応じて
        // 付け外しする（待機ピルは .fullScreenAuxiliary を外す＝フルスクリーンでは OS が自動で隠す）。
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.contentView = NSHostingView(rootView: HudView(model: model))
        return panel
    }

    private func positionPanel(animated: Bool = false) {
        guard let panel, let screen = NSScreen.main else { return }
        let frame = screen.visibleFrame
        let x = frame.midX - HudView.width / 2
        // パネル下端をほぼ画面下端に置く。カプセルはパネル内で下端＋6pt に寄せてあるので、
        // 実際のカプセル下端は画面下端（Dock 上端）+ 約 8pt に固定される。
        let y = frame.minY + 2
        let target = NSRect(x: x, y: y, width: HudView.width, height: HudView.height)
        if animated, panel.isVisible {
            // Dock 出没・解像度変化での縦位置追従はジャンプさせず、easeOut で 0.22 秒かけてスッと移動させる。
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.22
                ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().setFrame(target, display: true)
            }
        } else {
            panel.setFrame(target, display: true)
        }
    }

    /// 画面構成変化（Dock 出没・解像度変更・フルスクリーン Space 出入り）の通知を受けて位置を再評価する。
    /// フルスクリーン時の待機ピル非表示は OS（collectionBehavior）に委ねているため、ここでは
    /// 表示中パネルの縦位置追従だけを行う（待機ピルも録音ピルも同じ扱いでよい）。
    private func handleScreenChange() {
        guard let panel, model.mode != .hidden else { return }
        // Dock 出没・解像度変化に縦位置を滑らかに追従させる。
        if panel.isVisible { positionPanel(animated: true) }
    }

    /// Space / フルスクリーン切替の通知を受けて、表示中のバックドロップを再評価する。
    /// パネルが全 Space に居座る都合で、切替後に旧 Space の映り込みで固まることがあるため、
    /// mode 変化と同じ再評価パス（frame 再適用 + 再レイアウト/再描画）をイベント駆動で強制する。
    private func refreshBackdrop() {
        guard let panel, model.mode != .hidden else { return }
        // 待機ピルは canJoinAllSpaces を外している（現在の Space にのみ出る）ため、Space 切替時は
        // 現在の Space へ order し直して追従させる。通常デスクトップなら出て、フルスクリーン Space
        // では OS が出さない（検出・タイマー不要で確実・App Nap でも通知callbackは同期で走る）。
        if model.mode == .idlePill {
            show()
            return
        }
        // フルスクリーン時の待機ピル非表示は OS に委ねているため、録音/変換/通知はガラス背景の
        // 再サンプルだけでよい。
        guard panel.isVisible else { return }
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
    /// 変換中に入った直後の一度だけカプセルサイズを確定し、以降は凍結するためのロック。
    /// 明滅（opacity 往復）で中身が再測定され実測幅が微揺れしてもカプセル幅を動かさない
    /// （中央寄せの「変換中…」が横に流れる回帰の防止）。transcribing を抜けたら解除する。
    @State private var transcribingSizeLocked = false

    /// 待機ピルか（極小サイズ・薄めの mic のみ）
    private var isIdlePill: Bool { model.mode == .idlePill }
    /// 変換中か（明滅アニメを駆動してよいかの判定に使う）
    private var isTranscribing: Bool { model.mode == .transcribing }
    // 三段階サイズ（待機=最小 < 変換中=小 < 録音=大）を padding 差で作る。全体を「もう少しだけ」
    // 小型化するため通常（録音/通知）を 16/8→13/6 に、変換中はさらに詰めた 12/5 にして録音より縮ませる。
    private var horizontalPadding: CGFloat {
        if isIdlePill { return 12 }
        if isTranscribing { return 12 }
        return 13
    }
    private var verticalPadding: CGFloat {
        if isIdlePill { return 3 }
        if isTranscribing { return 5 }
        return 6
    }
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
                            // 既存のリム（.primary 由来なのでライト/ダーク両対応）。
                            // ガラスを薄めた（VEV alpha 0.7）ぶん装飾も弱め、存在感を下げる
                            .overlay(Capsule().strokeBorder(Color.primary.opacity(0.09), lineWidth: 0.5))
                            // 上縁の極薄い白ハイライトでレンズの立ち上がりを補う（フォールバックの
                            // すりガラスは屈折歪みが出ないため特に効く。ダークでも破綻しない控えめ値）。
                            .overlay(
                                Capsule().strokeBorder(
                                    LinearGradient(
                                        colors: [Color.white.opacity(0.14), Color.white.opacity(0.0)],
                                        startPoint: .top, endPoint: .bottom
                                    ),
                                    lineWidth: 0.6
                                )
                            )
                    }
                    .shadow(color: .black.opacity(isIdlePill ? 0.10 : 0.16), radius: isIdlePill ? 4 : 8, y: isIdlePill ? 1 : 2)
                    // 中身はカプセルの上に重ね、mode 間では opacity クロスフェードのみで出入りさせる
                    // （scale を使わない＝「消して出し直す」感を排除）。カプセルからはみ出さないよう
                    // カプセル形にクリップし、育つ／広がるのに合わせて中身が現れるようにする。
                    .overlay {
                        modeContent(for: model.mode)
                            .fixedSize()
                            // 「カプセルが伸び、伸びた先に中身が現れる」順序を作るための非対称フェード。
                            // 挿入は少し遅らせてフェードイン（カプセルが育ち始めてから新中身が出る）、
                            // 削除は先に速く消す（旧中身を残したまま育たせない）。
                            // 録音→変換中はカプセルが一回り縮む（三段階サイズ）ため、「変換中…」は
                            // 縮んでいくカプセルの中へクロスフェードで現れる。
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
        // 三段階サイズ（待機 < 変換中 < 録音）は維持しつつ、変換中に入った後のサイズ更新だけ止める。
        // 変換中は文字が静止（明滅撤去済み）なので通常は再測定されないが、貼り付け先アイコンの遅延
        // 反映など何らかの再測定が起きてもカプセル幅を動かさないための防御として、変換中に入った
        // 直後の一度だけ実測サイズを採用し、以降 transcribing の間は凍結する。
        // 録音→変換中の一回り縮む spring モーフは、この初回採用で一度だけ発火する。
        .onPreferenceChange(HudContentSizeKey.self) { newSize in
            if isTranscribing {
                if transcribingSizeLocked { return }
                transcribingSizeLocked = true
            }
            contentSize = newSize
        }
        // 変換中を抜けたらサイズ凍結を解除し、次モードの実測サイズへ再び追従できるようにする。
        .onChange(of: isTranscribing) { _, active in
            if !active { transcribingSizeLocked = false }
        }
        // デバッグ時のみ: カプセルの目標サイズ（contentSize）が変わるたびに記録する。変換中に入った後は
        // 一度きり（初回モーフ）で止まり、以降再発火しないことを stderr で数えて検証するためのフック。
        .onChange(of: contentSize) { _, size in
            if ProcessInfo.processInfo.environment["VOICEKEY_HUD_DEBUG_STATE"] != nil {
                NSLog("VK_HUD contentSize=%.1fx%.1f mode=%@", size.width, size.height, "\(model.mode)")
            }
        }
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

    /// 録音中の中身（アプリアイコン・状態ドット・波形・自動送信バッジ）。
    /// ハンズフリーは「ハンズフリー」ラベルや停止ヒントを出さず、状態ドットと波形バーを
    /// アクセント色（ティール）に着色することだけで通常録音と区別する（情報最小限・幅も詰まる）。
    @ViewBuilder
    private func recordingContent(autoEnter: Bool, handsFree: Bool) -> some View {
        // 状態色: ハンズフリー=ティール / 自動送信=パープル / 通常=レッド
        let accent: Color = handsFree ? Self.handsFreeAccent : (autoEnter ? Color.purple : Color.red)
        HStack(spacing: 10) {
            appIconView  // 貼り付け先アプリのアイコン（左端）
            Circle()
                .fill(accent)
                .frame(width: 7, height: 7)
            // 音声レベル連動の波形バー。ハンズフリー中はバーもアクセント色で塗る（ラベル無しでも一目で判る）
            levelBars(tint: handsFree ? Self.handsFreeAccent : Color.primary.opacity(0.75))
            if autoEnter {
                Image(systemName: "return")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(.purple)
            }
        }
    }

    /// 変換中の中身。「変換中…」を完全静止で表示する（明滅・スケール等の動きを一切付けない）。
    /// 【なぜ明滅を撤去したか】opacity 明滅（1.0⇄0.35）は幾何学的にはテキストを動かさない（実測でも
    /// カプセル幅・文字の左右端は 0px で不動）が、明滅の谷で末尾「…」の細いドットが先に視認閾値を
    /// 割って消え、可視インクの重心が横に振れる（実測: 白背景で重心 ±7pt / グレー背景で ±2.4pt・
    /// 周期は明滅 1.6s と一致）。これがユーザーの言う「変換中の文字が横に揺れる」の正体で、カプセル
    /// 幅の凍結（transcribingSizeLocked）では消えなかった。動きを完全に断つには静止表示が唯一確実
    /// （対称要素を明滅させても「…」の閾値落ちは残るため）。
    private var transcribingContent: some View {
        HStack(spacing: 10) {
            appIconView  // 貼り付け先アプリのアイコン（左端・残す）
            Text("変換中…")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
        }
    }

    /// 音声レベル連動の波形バー。tint で色を差し替え、ハンズフリー中はアクセント色で塗る
    private func levelBars(tint: Color) -> some View {
        HStack(spacing: 2.5) {
            ForEach(0..<Self.barCount, id: \.self) { i in
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(tint)
                    .frame(width: 3, height: barHeight(model.levels[i]))
            }
        }
        .animation(.linear(duration: 0.05), value: model.levels)
    }

    private func barHeight(_ level: Float) -> CGFloat {
        // 全体の小型化に合わせて最大高さを 22→18 に下げる
        let minH: CGFloat = 3
        let maxH: CGFloat = 18
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
        // ダークモードの material は tint が強く、背後の文字が「平均色の塊」に塗り潰れる
        // （8 素材を実測比較済み・どれも文字が透けない）。公開 API にブラー半径の調整は
        // 無いため、alpha でブラー層と素の背景を線形混合して「軽いガラス」に薄める。
        // 0.7 前後＝背後の文字がぼんやり透ける・存在感が下がる（1.0=不透明な塊、0.5 以下=素通しすぎ）
        alphaValue = 0.7
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
