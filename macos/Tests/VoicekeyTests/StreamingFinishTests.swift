//
//  StreamingFinishTests.swift
//  StreamingTranscriber.finish() の即解決テスト（コールド窓中の 3 秒スタール根治）
//
//  WS 未接続かつ音声ゼロ（1 バイトも送っていない）で finish() が呼ばれても、
//  来ない Metadata を 3 秒フルに待たず、即座に空文字で解決して REST フォールバックへ
//  回すことを保証する。ネットワークもマイクも使わない（未接続状態だけを確かめる）。
//

import XCTest
@testable import voicekey

final class StreamingFinishTests: XCTestCase {

    // start() を呼ばない＝未接続・音声ゼロ。finish() は 3 秒待たず即空解決する
    func testFinishBeforeConnectResolvesImmediately() async {
        let s = StreamingTranscriber(model: "nova-3", language: "ja")
        let t0 = Date()
        let text = await s.finish()  // 接続も送信もしていない
        let elapsed = Date().timeIntervalSince(t0)
        XCTAssertEqual(text, "", "未接続・音声ゼロは空を返す（呼び出し側が REST フォールバックへ回す）")
        XCTAssertLessThan(elapsed, 1.0, "3 秒の確定待ちタイムアウトをフルに待ってはいけない（即解決）")
    }
}
