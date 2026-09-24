//
//  InputRestartPolicyTests.swift
//  録音中の作り直し判断（InputRestartPolicy）のテスト
//
//  Bluetooth ヘッドセットで自分の IO 開始が起こす形式通知を即作り直さないこと、
//  1 回の録音での作り直し累計に上限があることを、HAL に触れず純関数だけで確かめる。
//

import XCTest
@testable import voicekey

final class InputRestartPolicyTests: XCTestCase {

    /// IO 開始から 1 秒以内の形式通知は見送る（自分の開始操作による切替とみなす）
    func testDefersFormatChangeRightAfterIOStart() {
        XCTAssertTrue(InputRestartPolicy.defersFormatChange(notifiedAt: 100.0, ioStartedAt: 100.0))
        XCTAssertTrue(InputRestartPolicy.defersFormatChange(notifiedAt: 100.9, ioStartedAt: 100.0))
    }

    /// 1 秒を過ぎた通知は本物の構成変更として扱う（従来どおり即作り直す）
    func testDoesNotDeferAfterWindow() {
        XCTAssertFalse(InputRestartPolicy.defersFormatChange(notifiedAt: 101.0, ioStartedAt: 100.0))
        XCTAssertFalse(InputRestartPolicy.defersFormatChange(notifiedAt: 150.0, ioStartedAt: 100.0))
    }

    /// IO を一度も開始していない（0）なら見送らない
    func testDoesNotDeferWithoutIOStart() {
        XCTAssertFalse(InputRestartPolicy.defersFormatChange(notifiedAt: 0.5, ioStartedAt: 0))
    }

    /// 2 秒窓の 3 回、または録音ごとの累計 5 回で諦める
    func testGiveUpLimits() {
        XCTAssertFalse(InputRestartPolicy.shouldGiveUp(recentRestarts: 0, restartsThisRecording: 0))
        XCTAssertFalse(InputRestartPolicy.shouldGiveUp(recentRestarts: 2, restartsThisRecording: 4))
        XCTAssertTrue(InputRestartPolicy.shouldGiveUp(recentRestarts: 3, restartsThisRecording: 3))
        // 2 秒窓がリセットされ続けても（1 秒おきの作り直し）、累計で打ち切る
        XCTAssertTrue(InputRestartPolicy.shouldGiveUp(recentRestarts: 0, restartsThisRecording: 5))
    }
}
