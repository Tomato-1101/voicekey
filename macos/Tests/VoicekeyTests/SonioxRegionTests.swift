//
//  SonioxRegionTests.swift
//  Soniox のリージョン判定（純関数）とキー指紋のテスト
//
//  実際の REST 問い合わせ（detect）は外部通信になるのでここでは呼ばない。
//  UserDefaults のキャッシュにも触れない（テストが利用者の設定を汚さないため）。
//

import XCTest
@testable import voicekey

final class SonioxRegionTests: XCTestCase {

    func testJapanKeyPicksJapan() {
        XCTAssertEqual(SonioxRegion.decide(jpStatus: 200, usStatus: 401), .jp)
    }

    func testUSKeyPicksUS() {
        XCTAssertEqual(SonioxRegion.decide(jpStatus: 401, usStatus: 200), .us)
    }

    /// 両方失敗（401・通信失敗）は判定不能＝nil（呼び出し側はキャッシュせず米国へつなぐ）
    func testBothFailedIsUndecided() {
        XCTAssertNil(SonioxRegion.decide(jpStatus: 401, usStatus: 401))
        XCTAssertNil(SonioxRegion.decide(jpStatus: nil, usStatus: nil))
        XCTAssertNil(SonioxRegion.decide(jpStatus: nil, usStatus: 500))
    }

    /// 片方だけ通信失敗でも、通った方を採る
    func testNetworkFailureOnOneSide() {
        XCTAssertEqual(SonioxRegion.decide(jpStatus: nil, usStatus: 200), .us)
        XCTAssertEqual(SonioxRegion.decide(jpStatus: 200, usStatus: nil), .jp)
    }

    /// 両方通るキーは日本を優先する
    func testBothOKPrefersJapan() {
        XCTAssertEqual(SonioxRegion.decide(jpStatus: 200, usStatus: 200), .jp)
    }

    func testEndpoints() {
        XCTAssertEqual(SonioxRegion.us.websocketURL.absoluteString, "wss://stt-rt.soniox.com/transcribe-websocket")
        XCTAssertEqual(SonioxRegion.jp.websocketURL.absoluteString, "wss://stt-rt.jp.soniox.com/transcribe-websocket")
        XCTAssertEqual(SonioxRegion.us.modelsURL.absoluteString, "https://api.soniox.com/v1/models")
        XCTAssertEqual(SonioxRegion.jp.modelsURL.absoluteString, "https://api.jp.soniox.com/v1/models")
    }

    /// 指紋は決定的で、16 桁の hex。キー本体を含まず、キーが違えば変わる
    func testFingerprint() {
        // hex に出てこない文字（k, -, z 等）を含むダミーキーにして「キーが含まれない」を確実に判定する
        let key = "sk-dummy-key-zzzz"
        let fp = SonioxRegion.fingerprint(of: key)
        XCTAssertEqual(fp, SonioxRegion.fingerprint(of: key))
        XCTAssertEqual(fp.count, 16)
        XCTAssertTrue(fp.allSatisfy { "0123456789abcdef".contains($0) })
        XCTAssertFalse(fp.contains(key))
        XCTAssertFalse(fp.contains("dummy"))
        XCTAssertNotEqual(fp, SonioxRegion.fingerprint(of: key + "x"))
    }
}
