//
//  AudioStartFailureTests.swift
//  録音開始失敗の理由分類（AudioRecorder.StartFailure）のテスト
//
//  ローカル LLM がメモリを食い尽くして coreaudiod が IO を開始できず、
//  AVAudioEngine.start() が OSStatus 2003329396（'what'）で失敗し続けた実事故がある。
//  一律「マイクを確認」ではなく「メモリ不足」と分かる文言を出せることを検証する。
//  AVAudioEngine には触れず、純関数の分類と表示文言だけを単体で確かめる。
//

import XCTest
@testable import voicekey

final class AudioStartFailureTests: XCTestCase {

    /// CoreAudio の 'what'（kAudioHardwareUnspecifiedError）はメモリ不足として扱う
    func testWhatErrorClassifiedAsOutOfMemory() {
        let error = NSError(domain: NSOSStatusErrorDomain, code: 2003329396)

        let failure = AudioRecorder.StartFailure.classify(error)
        XCTAssertEqual(failure, .outOfMemory)
        XCTAssertTrue(failure.noticeText.contains("メモリ不足"))
    }

    /// 未知のコードは .other としてそのままコードを表示する（ログとの突合用）
    func testUnknownCodeKeepsRawCodeInNotice() {
        let error = NSError(domain: NSOSStatusErrorDomain, code: 560030580)

        let failure = AudioRecorder.StartFailure.classify(error)
        XCTAssertEqual(failure, .other(560030580))
        XCTAssertTrue(failure.noticeText.contains("560030580"))
    }

    /// デバイス消失は「マイクが見つかりません」と出す
    func testDeviceMissingNotice() {
        XCTAssertTrue(AudioRecorder.StartFailure.deviceMissing.noticeText.contains("マイクが見つかりません"))
    }
}
