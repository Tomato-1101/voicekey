//
//  ClipboardRestorePolicyTests.swift
//  貼り付け後のクリップボード復元判定の単体テスト
//
//  ユーザー指摘 2 件（2026-09-19）への回帰テスト。
//  (1)「音声入力したのにクリップボードの内容が貼り付けられる」
//      復元が早すぎて貼り付け先が読む前にクリップボードが戻っていたのが原因。
//      待ち時間は Paster 側の定数なのでここでは扱わず、(2) の判定だけを固定する。
//  (2)「文字起こし結果がたまにクリップボードに残る」
//      旧実装は「原本が空 or 非テキスト」のとき復元を丸ごと諦めており、
//      自分が入れたテキストがクリップボードに残り続けていた。
//      その経路が .clear（明示的に空にする）へ変わったことを機械的に示す。
//

import AppKit
import XCTest

@testable import voicekey

final class ClipboardRestorePolicyTests: XCTestCase {

    /// より新しい貼り付けが走っていたら、古い復元タスクは何もしない（世代分離）
    func testSkipsWhenNewerPasteExists() {
        let decision = ClipboardRestorePolicy.decide(currentGeneration: 5,
                                                     taskGeneration: 4,
                                                     clipboardIsStillOurs: true,
                                                     original: "ユーザーの原本")
        XCTAssertEqual(decision, .skip)
    }

    /// 通常経路：クリップボードが自分の挿入テキストのままなら原本を書き戻す
    func testRestoresOriginal() {
        let decision = ClipboardRestorePolicy.decide(currentGeneration: 3,
                                                     taskGeneration: 3,
                                                     clipboardIsStillOurs: true,
                                                     original: "ユーザーの原本")
        XCTAssertEqual(decision, .restore("ユーザーの原本"))
    }

    /// 待っている間にユーザーが新しくコピーしたら、その内容を壊さない
    func testLeavesUserContentWhenClipboardChanged() {
        let decision = ClipboardRestorePolicy.decide(currentGeneration: 3,
                                                     taskGeneration: 3,
                                                     clipboardIsStillOurs: false,
                                                     original: "ユーザーの原本")
        XCTAssertEqual(decision, .leaveUserContent)
    }

    /// 退避できる原本が無かった（クリップボードが空）ケース。
    /// 旧実装はここで何もせず文字起こし結果を残していた（ユーザー指摘の主因）
    func testClearsWhenOriginalIsNil() {
        let decision = ClipboardRestorePolicy.decide(currentGeneration: 1,
                                                     taskGeneration: 1,
                                                     clipboardIsStillOurs: true,
                                                     original: nil)
        XCTAssertEqual(decision, .clear)
    }

    /// 原本が空文字列のときも同じく消す（残さない）
    func testClearsWhenOriginalIsEmpty() {
        let decision = ClipboardRestorePolicy.decide(currentGeneration: 1,
                                                     taskGeneration: 1,
                                                     clipboardIsStillOurs: true,
                                                     original: "")
        XCTAssertEqual(decision, .clear)
    }

    /// 原本が非テキスト（画像など）でも全型で退避してあるので、消さずにそのまま戻す
    /// （以前は文字列しか退避できず、画像をコピーしていると貼り付け後に消えていた）
    func testNonTextOriginalIsRestored() {
        let image = ClipboardSnapshot(items: [[.init(type: "public.png", data: Data([0x89, 0x50, 0x4E, 0x47]))]])
        let decision = ClipboardRestorePolicy.decide(currentGeneration: 7,
                                                     taskGeneration: 7,
                                                     clipboardIsStillOurs: true,
                                                     original: image)
        XCTAssertEqual(decision, .restore(image))
    }

    /// 世代不一致が最優先（他の条件がどうであれ触らない）
    func testGenerationMismatchWinsOverEverything() {
        XCTAssertEqual(ClipboardRestorePolicy.decide(currentGeneration: 2,
                                                     taskGeneration: 1,
                                                     clipboardIsStillOurs: false,
                                                     original: nil), .skip)
    }

    // MARK: - 退避の上限

    private let text = NSPasteboard.PasteboardType.string.rawValue
    private let rtf = NSPasteboard.PasteboardType.rtf.rawValue

    /// 上限内なら全項目・全型を元の並び順のまま退避する
    func testSelectKeepsEverythingWithinLimits() {
        let store: [String: Data] = [text: Data("a".utf8), "public.png": Data(count: 10), rtf: Data("r".utf8)]
        let (snapshot, dropped) = ClipboardSnapshot.select(
            itemTypes: [["public.png", text, rtf]], read: { _, type in store[type] },
            maxTotalBytes: 100, maxTypes: 10)
        XCTAssertEqual(dropped, 0)
        XCTAssertEqual(snapshot.items.map { $0.map(\.type) }, [["public.png", text, rtf]])
    }

    /// 合計が上限を超えたら重い型は 1 つも残さず、文字列・RTF だけ退避する
    func testSelectFallsBackToLightTypesWhenTooLarge() {
        let store: [String: Data] = [
            text: Data("a".utf8), rtf: Data("r".utf8),
            "public.png": Data(count: 60), "public.tiff": Data(count: 60),
        ]
        let (snapshot, dropped) = ClipboardSnapshot.select(
            itemTypes: [["public.tiff", "public.png", text, rtf]], read: { _, type in store[type] },
            maxTotalBytes: 100, maxTypes: 10)
        XCTAssertEqual(snapshot.items.map { $0.map(\.type) }, [[text, rtf]])
        XCTAssertEqual(dropped, 2)
    }

    /// 型の数が上限を超えたら重い型は読みすらしない（読むこと自体が待ちになるため）
    func testSelectSkipsHeavyReadsWhenTooManyTypes() {
        var readTypes: [String] = []
        let items = (0..<5).map { _ in [text, "com.example.heavy"] }
        let (snapshot, dropped) = ClipboardSnapshot.select(
            itemTypes: items,
            read: { _, type in readTypes.append(type); return Data("x".utf8) },
            maxTotalBytes: 1000, maxTypes: 8)
        XCTAssertFalse(readTypes.contains("com.example.heavy"))
        XCTAssertEqual(snapshot.items.count, 5)
        XCTAssertEqual(snapshot.items.flatMap { $0 }.map(\.type), Array(repeating: text, count: 5))
        XCTAssertEqual(dropped, 5)
    }

    /// 中身の無い型があるだけなら、上限で削ったことにはしない
    func testSelectDoesNotCountUnreadableTypes() {
        let (snapshot, dropped) = ClipboardSnapshot.select(
            itemTypes: [[text, "public.png"]], read: { _, type in type == text ? Data("a".utf8) : nil },
            maxTotalBytes: 100, maxTypes: 10)
        XCTAssertEqual(dropped, 0)
        XCTAssertEqual(snapshot, "a")
    }
}
