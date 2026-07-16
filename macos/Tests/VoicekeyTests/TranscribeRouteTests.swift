//
//  TranscribeRouteTests.swift
//  文字起こし経路選択（Transcriber.selectRoute）の純ロジックテスト
//
//  目的: personal（個人用最速版）が、isDist / ログイン状態に関わらず必ず Keychain 直叩き
//  （.directKeychain）を選び、サーバー経路（.server）やログイン要求（.needLogin）を一切
//  通らないことを保証する（＝サーバー往復ゼロ＝最速の核心を回帰から守る）。
//

import XCTest
@testable import voicekey

final class TranscribeRouteTests: XCTestCase {

    // MARK: - personal は常に Keychain 直叩き（サーバー・ログイン要求を通らない）

    /// personal のとき、isDist / ログインの全組合せで必ず .directKeychain を選ぶ。
    func testPersonalAlwaysDirectKeychain() {
        for isDist in [false, true] {
            for isLoggedIn in [false, true] {
                let route = Transcriber.selectRoute(
                    isPersonal: true, isDist: isDist, isLoggedIn: isLoggedIn
                )
                XCTAssertEqual(
                    route, .directKeychain,
                    "personal は isDist=\(isDist) / isLoggedIn=\(isLoggedIn) でも Keychain 直叩きであるべき"
                )
                XCTAssertNotEqual(route, .server, "personal はサーバー経路を通ってはいけない")
                XCTAssertNotEqual(route, .needLogin, "personal はログイン要求を出してはいけない")
            }
        }
    }

    // MARK: - 非 personal（従来の release/開発ビルド）の経路は不変

    /// 配布ビルド（isDist=true）: ログイン済みはサーバー、未ログインはログイン要求。
    func testDistRoutes() {
        XCTAssertEqual(
            Transcriber.selectRoute(isPersonal: false, isDist: true, isLoggedIn: true), .server
        )
        XCTAssertEqual(
            Transcriber.selectRoute(isPersonal: false, isDist: true, isLoggedIn: false), .needLogin
        )
    }

    /// 開発ビルド（isDist=false）: ログイン済みはサーバー、未ログインは Keychain 直叩き。
    func testDevRoutes() {
        XCTAssertEqual(
            Transcriber.selectRoute(isPersonal: false, isDist: false, isLoggedIn: true), .server
        )
        XCTAssertEqual(
            Transcriber.selectRoute(isPersonal: false, isDist: false, isLoggedIn: false), .directKeychain
        )
    }
}
