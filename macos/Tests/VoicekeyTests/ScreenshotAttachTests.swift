//
//  ScreenshotAttachTests.swift
//  スクリーンショット添付（録音中に撮った画像を文と一緒に貼る）の単体テスト
//
//  計画書 docs/SCREENSHOT_ATTACH_PLAN.md の検証項目のうち、範囲選択の UI と実際の貼り付け先を
//  使わずに確かめられるもの（キー判定・印の位置・貼る順番・記号削除との共存・空出力の判定・
//  クリップボードの退避と復元）を固定する。
//

import AppKit
import XCTest

@testable import voicekey

@MainActor
final class ScreenshotAttachTests: XCTestCase {

    // MARK: - 設定

    // 既定は左⌃。空配列（外した）は保存し直しても空のまま（既定へ戻さない）
    func testConfigDefaultAndEmptyPersists() {
        let suite = testDefaultsSuite()
        let defaults = UserDefaults(suiteName: suite)!
        defer { removeTestDefaults(defaults, suite) }

        let store = ConfigStore(defaults: defaults)
        XCTAssertEqual(store.screenshotKey, ["ctrl_l"])
        store.screenshotKey = []
        XCTAssertEqual(defaults.array(forKey: "screenshotKey") as? [String], [])
        XCTAssertEqual(ConfigStore(defaults: defaults).screenshotKey, [])
        store.screenshotKey = ["f13"]
        XCTAssertEqual(ConfigStore(defaults: defaults).screenshotKey, ["f13"])
    }

    // MARK: - キー

    func testIsToken() {
        XCTAssertTrue(ScreenshotAttach.isToken("ctrl_l", key: ["ctrl_l"]))
        XCTAssertFalse(ScreenshotAttach.isToken("cmd_r", key: ["ctrl_l"]))
        XCTAssertFalse(ScreenshotAttach.isToken("ctrl_l", key: []))
    }

    // 録音キーと一緒に押されていれば撮影
    func testRequestedWithSlotKey() {
        XCTAssertTrue(ScreenshotAttach.isRequested(
            key: ["ctrl_l"], slotHotkey: ["cmd_r"], pressed: ["cmd_r", "ctrl_l"]))
    }

    // 撮影キーが押されていなければ撮影しない・空（無効）なら何を押しても撮影しない
    func testNotRequestedWithoutKeyOrWhenDisabled() {
        XCTAssertFalse(ScreenshotAttach.isRequested(key: ["ctrl_l"], slotHotkey: ["cmd_r"], pressed: ["cmd_r"]))
        XCTAssertFalse(ScreenshotAttach.isRequested(key: [], slotHotkey: ["cmd_r"], pressed: ["cmd_r", "ctrl_l"]))
    }

    // 撮影キーが録音キーに含まれると、録音を始めるたびに撮影になるので撮影扱いにしない
    func testNotRequestedWhenCoveredBySlotKey() {
        XCTAssertFalse(ScreenshotAttach.isRequested(
            key: ["ctrl_l"], slotHotkey: ["ctrl_l"], pressed: ["ctrl_l"]))
    }

    // 重なりが無ければ警告なし
    func testNoWarningsWhenIndependent() {
        XCTAssertEqual(ScreenshotAttach.keyWarnings(
            key: ["ctrl_l"], slotHotkeys: [["cmd_r"], ["alt_r"]], handsfreeKey: ["shift_r"],
            translateOnceKey: ["shift_r"]), [])
        XCTAssertEqual(ScreenshotAttach.keyWarnings(
            key: [], slotHotkeys: [["ctrl_l"]], handsfreeKey: [], translateOnceKey: []), [])
    }

    // 録音キー・ハンズフリーキー・1 回翻訳キーと重なると警告する
    func testWarningsOnOverlap() {
        XCTAssertEqual(ScreenshotAttach.keyWarnings(
            key: ["ctrl_l"], slotHotkeys: [["cmd_r"], ["ctrl_l"]], handsfreeKey: [], translateOnceKey: []).count, 1)
        XCTAssertEqual(ScreenshotAttach.keyWarnings(
            key: ["ctrl_l"], slotHotkeys: [["cmd_r"]], handsfreeKey: ["ctrl_l"], translateOnceKey: []).count, 1)
        XCTAssertEqual(ScreenshotAttach.keyWarnings(
            key: ["ctrl_l"], slotHotkeys: [["cmd_r"]], handsfreeKey: [], translateOnceKey: ["ctrl_l"]).count, 1)
    }

    // MARK: - 貼り先

    func testIsTerminal() {
        XCTAssertTrue(ScreenshotAttach.isTerminal(bundleID: "com.apple.Terminal"))
        XCTAssertTrue(ScreenshotAttach.isTerminal(bundleID: "com.googlecode.iterm2"))
        XCTAssertTrue(ScreenshotAttach.isTerminal(bundleID: "com.mitchellh.ghostty"))
        XCTAssertFalse(ScreenshotAttach.isTerminal(bundleID: "com.anthropic.claudefordesktop"))
        XCTAssertFalse(ScreenshotAttach.isTerminal(bundleID: "com.google.Chrome"))
        XCTAssertFalse(ScreenshotAttach.isTerminal(bundleID: nil))
    }

    // GUI: 画像を順に貼ってから印入りの文
    func testPastePlanGUI() {
        XCTAssertEqual(ScreenshotAttach.pastePlan(text: "a[画像1]b[画像2]", imageCount: 2, terminal: false),
                       [.image(0), .image(1), .text("a[画像1]b[画像2]")])
        // 文が空なら画像だけ
        XCTAssertEqual(ScreenshotAttach.pastePlan(text: "", imageCount: 1, terminal: false), [.image(0)])
    }

    // ターミナル: 印で文を区切って画像を差し込み、印そのものは貼らない
    func testPastePlanTerminal() {
        XCTAssertEqual(ScreenshotAttach.pastePlan(text: "a[画像1]b[画像2]c", imageCount: 2, terminal: true),
                       [.text("a"), .image(0), .text("b"), .image(1), .text("c")])
    }

    // ターミナルで印が見つからなかった画像は最後に足す（画像を落とさない）
    func testPastePlanTerminalMissingMarkerAppendsImage() {
        XCTAssertEqual(ScreenshotAttach.pastePlan(text: "a[画像1]b", imageCount: 2, terminal: true),
                       [.text("a"), .image(0), .text("b"), .image(1)])
        XCTAssertEqual(ScreenshotAttach.pastePlan(text: "", imageCount: 2, terminal: true), [.image(0), .image(1)])
    }

    // MARK: - 印の位置

    // 日本語: 押した時のライブ文字の末尾を確定文で探し、直後の句点へ寄せる
    func testMarkerAnchoredOnLiveTextJapanese() {
        let text = "今日は会議です。明日は休みです。"
        let offsets = ScreenshotAttach.markerOffsets(
            text: text, shots: [.init(elapsed: 2, liveText: "今日は会議です")],
            finalLiveText: text, recordingSeconds: 5)
        XCTAssertEqual(ScreenshotAttach.insertMarkers(into: text, offsets: offsets), "今日は会議です。[画像1]明日は休みです。")
    }

    // 英語: 単語の途中に入れず、句読点の直後へ寄せる
    func testMarkerAnchoredOnLiveTextEnglish() {
        let text = "Look at this. And this one."
        let offsets = ScreenshotAttach.markerOffsets(
            text: text, shots: [.init(elapsed: 1, liveText: "Look at this")],
            finalLiveText: text, recordingSeconds: 3)
        XCTAssertEqual(ScreenshotAttach.insertMarkers(into: text, offsets: offsets), "Look at this.[画像1] And this one.")
    }

    // 英数字に隣接するときだけ空白を挟む
    func testInsertMarkerSpacesAroundAlphanumerics() {
        XCTAssertEqual(ScreenshotAttach.insertMarkers(into: "abcdef", offsets: [3]), "abc [画像1] def")
        XCTAssertEqual(ScreenshotAttach.insertMarkers(into: "あいうえ", offsets: [2]), "あい[画像1]うえ")
    }

    // ライブ文字が無い（REST 経路）なら経過秒の割合。複数枚は順番どおり
    func testMarkerByElapsedRatioWithMultipleShots() {
        let text = "一二三四五六七八九十。あいうえおかきくけこ。"
        let offsets = ScreenshotAttach.markerOffsets(
            text: text, shots: [.init(elapsed: 5, liveText: ""), .init(elapsed: 10, liveText: "")],
            finalLiveText: "", recordingSeconds: 10)
        XCTAssertEqual(ScreenshotAttach.insertMarkers(into: text, offsets: offsets),
                       "一二三四五六七八九十。[画像1]あいうえおかきくけこ。[画像2]")
    }

    // 録音の長さが分からなければ末尾
    func testMarkerFallsBackToEnd() {
        let offsets = ScreenshotAttach.markerOffsets(
            text: "こんにちは", shots: [.init(elapsed: 1, liveText: "")], finalLiveText: "", recordingSeconds: 0)
        XCTAssertEqual(offsets, [5])
    }

    // 整形・翻訳で崩れた印は、印らしきものを消して相対位置で置き直す
    func testEnsureMarkersReplacesMangledMarker() {
        XCTAssertEqual(ScreenshotAttach.ensureMarkers(in: "見てください。[Image 1]以上。", count: 1, ratios: [0.5]),
                       "見てください。[画像1]以上。")
        // 印が消えていても置き直す
        XCTAssertEqual(ScreenshotAttach.ensureMarkers(in: "見てください。以上。", count: 1, ratios: [0.5]),
                       "見てください。[画像1]以上。")
    }

    // 印が順番どおり 1 回ずつ残っていれば触らない
    func testEnsureMarkersKeepsIntactMarkers() {
        let text = "a [画像1] b [画像2]"
        XCTAssertEqual(ScreenshotAttach.ensureMarkers(in: text, count: 2, ratios: [0.1, 0.9]), text)
    }

    // MARK: - 記号の自動削除との共存（回帰）

    // 既定の記号削除は角かっこを消すが、[画像N] の印は残し、印の外の記号は従来どおり消す
    func testMarkerSurvivesSymbolRemovalWithDefaultKeepGroups() {
        let filtered = ScreenshotAttach.preservingMarkers(in: "見て！[画像1]これです【重要】", active: true) {
            SymbolFilter.filter($0, enabled: true, keepGroups: SymbolFilter.defaultKeepGroups)
        }
        XCTAssertTrue(filtered.contains("[画像1]"))
        XCTAssertFalse(filtered.contains("！"))
        XCTAssertFalse(filtered.contains("【"))
    }

    // 翻訳で崩れた印（[Image 1]）も記号削除から守り、ensureMarkers で正しい形に戻せる
    func testMangledMarkerSurvivesSymbolRemoval() {
        let filtered = ScreenshotAttach.preservingMarkers(in: "Look! [Image 1] here.", active: true) {
            SymbolFilter.filter($0)
        }
        XCTAssertTrue(filtered.contains("[Image 1]"))
        XCTAssertEqual(ScreenshotAttach.ensureMarkers(in: filtered, count: 1, ratios: [0.5]).components(
            separatedBy: "[画像1]").count, 2)
    }

    // 撮影ゼロ枚の録音（active=false）は区切らずにそのまま加工する＝今の経路を変えない
    func testInactiveLeavesPipelineUnchanged() {
        let text = "見て！[画像1]これ"
        let plain = SymbolFilter.filter(text)
        XCTAssertEqual(ScreenshotAttach.preservingMarkers(in: text, active: false) { SymbolFilter.filter($0) }, plain)
    }

    // MARK: - 空出力の判定

    // 記号削除で文が空・空白だけ、かつ画像 0 枚なら貼るものなし（履歴・貼り付け・Enter をしない）
    func testNothingToPasteWhenEmptyAndNoImages() {
        XCTAssertTrue(ScreenshotAttach.isNothingToPaste(text: SymbolFilter.filter("！"), imageCount: 0))
        XCTAssertTrue(ScreenshotAttach.isNothingToPaste(text: " \n\u{3000}", imageCount: 0))
        XCTAssertTrue(ScreenshotAttach.isNothingToPaste(text: "", imageCount: 0))
    }

    // 画像があれば文が空でも画像だけ貼る・文があれば貼る
    func testSomethingToPasteWithImagesOrText() {
        XCTAssertFalse(ScreenshotAttach.isNothingToPaste(text: "", imageCount: 1))
        XCTAssertFalse(ScreenshotAttach.isNothingToPaste(text: "はい", imageCount: 0))
    }

    // MARK: - 撮影の記録

    // 取消した 1 枚は番号を詰め、撮れた分だけ [画像1][画像2] になる
    func testCancelledShotClosesNumbering() {
        let session = ScreenshotSession()
        let first = session.arm(.init(elapsed: 1, liveText: ""))
        session.finish(first, capture: Self.sampleCapture())
        let second = session.arm(.init(elapsed: 2, liveText: ""))
        session.finish(second, capture: nil)
        let third = session.arm(.init(elapsed: 3, liveText: ""))
        session.finish(third, capture: Self.sampleCapture())
        session.recordingSeconds = 4
        XCTAssertEqual(session.completed.count, 2)
        XCTAssertEqual(session.visibleCount, 2)
        let text = session.insertMarkers(into: "一。二。三。四。")
        XCTAssertTrue(text.contains("[画像1]"))
        XCTAssertTrue(text.contains("[画像2]"))
        XCTAssertFalse(text.contains("[画像3]"))
    }

    // 撮影中は busy。終わるまで待ってから確定できる
    func testWaitUntilSettledResumesAfterFinish() async {
        let session = ScreenshotSession()
        let shot = session.arm(.init(elapsed: 1, liveText: ""))
        session.markCapturing(shot)
        XCTAssertTrue(session.isBusy)
        Task { @MainActor in session.finish(shot, capture: Self.sampleCapture()) }
        await session.waitUntilSettled()
        XCTAssertFalse(session.isBusy)
        XCTAssertEqual(session.completed.count, 1)
    }

    // 押したまま録音が終わった 1 枚は取消になる
    func testCancelArmed() {
        let session = ScreenshotSession()
        session.arm(.init(elapsed: 1, liveText: ""))
        XCTAssertNotNil(session.armedShot)
        session.cancelArmed()
        XCTAssertNil(session.armedShot)
        XCTAssertFalse(session.isBusy)
        XCTAssertEqual(session.visibleCount, 0)
    }

    // MARK: - 連続貼り付け（実ペーストボード・キーは送らない）

    // GUI: 画像 → 画像 → 文の順に、全部 ⌘V で、画像は PNG と TIFF の両方。最後に原本へ 1 回で戻る
    func testPasteSequenceGUIOrderAndSingleRestore() async throws {
        let board = NSPasteboard(name: NSPasteboard.Name("com.voicekey.test.screenshot-\(UUID().uuidString)"))
        defer { board.releaseGlobally() }
        board.clearContents()
        board.setString("USER-ORIGINAL", forType: .string)
        var seen: [(png: Bool, tiff: Bool, text: String?, controlV: Bool)] = []
        Paster.sequenceProbe = { pb, controlV in
            seen.append((pb.data(forType: .png) != nil, pb.data(forType: .tiff) != nil,
                         pb.string(forType: .string), controlV))
        }
        defer { Paster.sequenceProbe = nil }

        let session = Self.sessionWithTwoShots()
        let ok = await Paster.pasteSequence(session.pasteItems(text: "a[画像1]b[画像2]", terminal: false),
                                            fallbackText: "a[画像1]b[画像2]", pasteboard: board, sendKeystroke: false)
        XCTAssertTrue(ok)
        XCTAssertEqual(seen.count, 3)
        XCTAssertTrue(seen[0].png && seen[0].tiff)
        XCTAssertTrue(seen[1].png && seen[1].tiff)
        XCTAssertEqual(seen[2].text, "a[画像1]b[画像2]")
        XCTAssertFalse(seen.contains { $0.controlV })
        try await Task.sleep(for: .seconds(1.6))
        XCTAssertEqual(board.string(forType: .string), "USER-ORIGINAL")
    }

    // ターミナル: 文と画像を交互に、画像だけ ⌃V で。印は貼らない。最後に原本へ戻る
    func testPasteSequenceTerminalInterleaveWithControlV() async throws {
        let board = NSPasteboard(name: NSPasteboard.Name("com.voicekey.test.screenshot-\(UUID().uuidString)"))
        defer { board.releaseGlobally() }
        board.clearContents()
        board.setString("USER-ORIGINAL", forType: .string)
        var seen: [(image: Bool, text: String?, controlV: Bool)] = []
        Paster.sequenceProbe = { pb, controlV in
            seen.append((pb.data(forType: .png) != nil, pb.string(forType: .string), controlV))
        }
        defer { Paster.sequenceProbe = nil }

        let session = Self.sessionWithTwoShots()
        let ok = await Paster.pasteSequence(session.pasteItems(text: "a[画像1]b[画像2]c", terminal: true),
                                            fallbackText: "a[画像1]b[画像2]c", pasteboard: board, sendKeystroke: false)
        XCTAssertTrue(ok)
        XCTAssertEqual(seen.map(\.image), [false, true, false, true, false])
        XCTAssertEqual(seen.map(\.controlV), [false, true, false, true, false])
        XCTAssertEqual(seen.compactMap(\.text), ["a", "b", "c"])
        try await Task.sleep(for: .seconds(1.6))
        XCTAssertEqual(board.string(forType: .string), "USER-ORIGINAL")
    }

    // MARK: - 見本

    private static func sampleCapture() -> ScreenshotSession.Capture {
        let image = NSImage(size: NSSize(width: 4, height: 4))
        image.lockFocus()
        NSColor.systemRed.setFill()
        NSRect(x: 0, y: 0, width: 4, height: 4).fill()
        image.unlockFocus()
        let tiff = image.tiffRepresentation
        let png = tiff.flatMap { NSBitmapImageRep(data: $0)?.representation(using: .png, properties: [:]) } ?? Data()
        return ScreenshotSession.Capture(png: png, tiff: tiff)
    }

    private static func sessionWithTwoShots() -> ScreenshotSession {
        let session = ScreenshotSession()
        for index in 0..<2 {
            let shot = session.arm(.init(elapsed: Double(index + 1), liveText: ""))
            session.finish(shot, capture: sampleCapture())
        }
        return session
    }
}
