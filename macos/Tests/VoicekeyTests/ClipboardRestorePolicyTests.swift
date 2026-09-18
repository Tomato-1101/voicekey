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

    /// 原本が非テキスト（画像など）で退避できなかった場合も nil 扱いで消える。
    /// 「残す」より「空にする」を選ぶのは、文字起こし結果をクリップボードに
    /// 保存しないことがユーザーの前提だから
    func testNonTextOriginalIsTreatedAsClear() {
        let decision = ClipboardRestorePolicy.decide(currentGeneration: 7,
                                                     taskGeneration: 7,
                                                     clipboardIsStillOurs: true,
                                                     original: nil)
        XCTAssertEqual(decision, .clear)
    }

    /// 世代不一致が最優先（他の条件がどうであれ触らない）
    func testGenerationMismatchWinsOverEverything() {
        XCTAssertEqual(ClipboardRestorePolicy.decide(currentGeneration: 2,
                                                     taskGeneration: 1,
                                                     clipboardIsStillOurs: false,
                                                     original: nil), .skip)
    }
}
