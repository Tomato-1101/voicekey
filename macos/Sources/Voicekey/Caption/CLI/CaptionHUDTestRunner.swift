/// `--caption-hud-test` モード（字幕 HUD の位置と、音声入力中の自動非表示を実測する）
///
/// **なぜ要るか**: 2026-08-10 のユーザー指示で字幕の見た目が 2 点変わった。
///   - 字幕はドラッグで動かさず、**voicekey ピルの真上に固定**して上へ伸びる
///     （「ピルが字幕に大きくなっていく感じ」）。
///   - **音声入力中（録音・変換中・通知）は字幕を隠す**。入力が終わったら戻す。
/// どちらも目で見ただけでは「たまたまそう見えた」と区別が付かないので、
/// ピルと同じ計算式で出した基準点と実フレームを突き合わせて機械判定する。
///
/// **判定**:
///   (a) 字幕パネルの下辺がピルのカプセル下端と一致する（±1pt）
///   (b) 字幕パネルの横中心が画面の横中心と一致する（±1pt）
///   (c) 行を積んでも下辺が動かない＝上へ伸びる
///   (d) 音声入力中にすると画面から消える
///   (e) 音声入力が終わると同じ位置に戻る
///   (f) 字幕が出ている間は待機ピルが引っ込んでいる／字幕を畳んだら戻る（2026-08-12 追加）
///
/// **(f) をなぜ足したか**: 旧版は字幕パネルの座標を定数 `pillAnchor` と突き合わせるだけで、
/// 実在するピルのウィンドウを一度も見ていなかった。そのため「字幕の裏に待機ピルが残り、
/// 半透明のガラス越しに濃いカプセルが透ける」不具合を素通しした。ここでは実物の
/// `HudController` のウィンドウの可視状態を直接見る。
import AppKit
import Foundation

/// 字幕 HUD の位置と自動非表示のテスト
@available(macOS 26.0, *)
enum CaptionHUDTestRunner {

    /// 各フェーズを保つ秒数（この間にスクリーンショットを撮れるようにしている）
    ///
    /// 字幕行は「読み終わる頃に自分から退場する」ため、測っている途中で消えないよう
    /// 退場までの時間（文字数 × 0.15 秒 + 余韻 1 秒）より短く刻む。
    private static let phaseSeconds: Double = 2.5
    /// 位置判定の許容誤差（pt）
    private static let tolerance: CGFloat = 1.0
    /// 行が 1 つも無いときのパネル高さ（上下の余白だけ）。これを超えていれば中身がある。
    private static let emptyPanelHeight: CGFloat = 12

    /// テストを実行する
    ///
    /// - Parameter logFilePath: ログの追加出力先（nil なら標準出力のみ）
    /// - Returns: 終了コード（0=PASS / 1=FAIL）
    @MainActor
    static func run(logFilePath: String?) async -> Int32 {
        let writer = CaptionTestLogWriter(path: logFilePath)
        defer { writer.close() }

        // ピルと字幕を同じ画面に並べたいので、GUI として最低限の初期化をする
        // （メニューバーにもDockにも出さないアクセサリ扱い）
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        writer.write("[INFO] voicekey caption-hud-test 開始 pid=\(getpid())")

        // 1. 待機ピルを出す（字幕の基準になる相手）
        let hud = HudController()
        hud.enabled = true
        hud.alwaysVisible = true
        hud.update(for: .idle)

        // 2. 字幕を出す
        //    本番と同じ結線（CaptionService → AppController → HudController）の要点だけを張る。
        //    字幕が出ている間は待機ピルを引っ込め、畳み終わったら戻す。
        writer.write("[PHASE-START] 1行")
        let caption = CaptionHUDController()
        caption.onVisibilityChanged = { visible in
            MainActor.assumeIsolated { hud.captionCovering = visible }
        }
        caption.showTranslation(
            source: "The weather in Tokyo is very nice today, so I decided to walk to the station.",
            japanese: "今日の東京はとても良い天気なので、駅まで歩いて行くことにしました。"
        )
        try? await Task.sleep(for: .seconds(phaseSeconds))

        var failures: [String] = []
        let anchor = CaptionHUDController.pillAnchor
        let oneLine = caption.panelFrame
        writer.write(
            line(phase: "1行", frame: oneLine, anchor: anchor, onScreen: caption.isOnScreen,
                 pillOnScreen: pillOnScreen(hud)))

        if abs(oneLine.minY - anchor.y) > tolerance { failures.append("下辺がピルに揃っていない") }
        if abs(oneLine.midX - anchor.x) > tolerance { failures.append("横中心がずれている") }
        if !caption.isOnScreen { failures.append("1行目が表示されていない") }

        // 3. 行を積んでも下辺が動かない（＝上へ伸びる）ことを見る
        writer.write("[PHASE-START] 2行")
        caption.showTranslation(
            source: "I am testing whether the live caption panel stays anchored to the pill.",
            japanese: "字幕パネルがピルの真上に固定されたままかどうかをテストしています。"
        )
        try? await Task.sleep(for: .seconds(phaseSeconds))
        let twoLines = caption.panelFrame
        writer.write(
            line(phase: "2行", frame: twoLines, anchor: anchor, onScreen: caption.isOnScreen,
                 pillOnScreen: pillOnScreen(hud)))

        if abs(twoLines.minY - anchor.y) > tolerance { failures.append("行が増えたら下辺が動いた") }
        if twoLines.height <= oneLine.height { failures.append("行が増えても高さが伸びていない") }

        // 3.5 字幕が出ている間は待機ピルが引っ込んでいる（字幕の裏に透けない）
        writer.write("[PHASE-START] ピル退避")
        writer.write(
            line(phase: "ピル退避", frame: caption.panelFrame, anchor: anchor, onScreen: caption.isOnScreen,
                 pillOnScreen: pillOnScreen(hud)))
        if pillOnScreen(hud) { failures.append("字幕が出ているのに待機ピルが残っている") }

        // 4. 音声入力中は隠れる
        writer.write("[PHASE-START] 音声入力中")
        hud.update(for: .recording(autoEnter: false, handsFree: false))
        caption.isSuppressed = true
        try? await Task.sleep(for: .seconds(phaseSeconds))
        writer.write(
            line(phase: "音声入力中", frame: caption.panelFrame, anchor: anchor, onScreen: caption.isOnScreen,
                 pillOnScreen: pillOnScreen(hud)))
        if caption.isOnScreen { failures.append("音声入力中なのに字幕が出ている") }

        // 5. 音声入力が終わったら、また同じ場所に字幕が出る
        //
        // 隠している間も行の「読み終わりで自主退場」は動き続けるため、隠す前の行が
        // 残っているとは限らない。実運用と同じく「次の文が来たら出る」ことを確かめる。
        writer.write("[PHASE-START] 復帰")
        hud.update(for: .idle)
        caption.isSuppressed = false
        caption.showTranslation(
            source: "Dictation has finished, so the captions are back above the pill.",
            japanese: "音声入力が終わったので、字幕はまたピルの上に戻ってきました。"
        )
        try? await Task.sleep(for: .seconds(phaseSeconds))
        let restored = caption.panelFrame
        writer.write(
            line(phase: "復帰", frame: restored, anchor: anchor, onScreen: caption.isOnScreen,
                 pillOnScreen: pillOnScreen(hud)))
        if !caption.isOnScreen { failures.append("音声入力が終わっても字幕が戻らない") }
        if abs(restored.minY - anchor.y) > tolerance { failures.append("復帰後の下辺がずれている") }
        if restored.height <= Self.emptyPanelHeight { failures.append("復帰したが中身が無い") }

        // 6. 字幕を畳んだら待機ピルが戻る（引っ込めたまま二度と戻らない、を塞ぐ）
        //    ピルへ縮み終わってから戻る仕様なので、畳みアニメ（0.22 秒）の完了を待つ。
        writer.write("[PHASE-START] ピル復帰")
        caption.clear()
        try? await Task.sleep(for: .seconds(phaseSeconds))
        writer.write(
            line(phase: "ピル復帰", frame: caption.panelFrame, anchor: anchor, onScreen: caption.isOnScreen,
                 pillOnScreen: pillOnScreen(hud)))
        if caption.isOnScreen { failures.append("字幕を畳んだのにまだ出ている") }
        if !pillOnScreen(hud) { failures.append("字幕を畳んでも待機ピルが戻らない") }

        let isPass = failures.isEmpty
        writer.write(
            "[VERDICT] status=\(isPass ? "ok" : "fail") "
                + "failures=\(failures.isEmpty ? "なし" : failures.joined(separator: ","))"
        )
        return isPass ? 0 : 1
    }

    /// 待機ピルが実際に画面に出ているか
    ///
    /// 定数ではなく `HudController` が持っている本物のウィンドウを見る（ここを見ていなかったのが
    /// 「字幕の裏にピルが残る」不具合を素通しした原因）。
    @MainActor
    private static func pillOnScreen(_ hud: HudController) -> Bool {
        hud.pillPanelForTesting?.isVisible ?? false
    }

    /// 1 フェーズぶんの計測行
    private static func line(
        phase: String, frame: NSRect, anchor: CGPoint, onScreen: Bool, pillOnScreen: Bool
    ) -> String {
        String(
            format: "[HUD] phase=%@ onScreen=%@ pill=%@ bottom=%.1f 期待=%.1f 中心X=%.1f 期待=%.1f 幅=%.1f 高さ=%.1f",
            phase, onScreen ? "yes" : "no", pillOnScreen ? "yes" : "no",
            frame.minY, anchor.y, frame.midX, anchor.x, frame.width, frame.height
        )
    }
}
