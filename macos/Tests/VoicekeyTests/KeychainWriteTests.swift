//
//  KeychainWriteTests.swift
//  Keychain.write の書き込み手順テスト（#17）
//
//  実 Keychain（Security 関数）には触れず、Keychain.Ops を注入して手順だけを検証する。
//  これにより CI / ローカルのどちらでもパスワード承認ダイアログを出さずにテストできる
//  （2026-06-12 の教訓: テストで実資格情報ストアに触れない）。
//

import XCTest
@testable import voicekey

final class KeychainWriteTests: XCTestCase {

    /// add 成功時は true を返し、新値が格納される（旧値の復元は走らない）
    func testWriteSuccessStoresNewValue() {
        var store: [String: String] = ["svc": "old"]
        var addCalls = 0
        let ops = Keychain.Ops(
            read: { store[$0] },
            delete: { store[$0] = nil },
            add: { addCalls += 1; store[$0] = $1; return true }
        )

        XCTAssertTrue(Keychain.write(service: "svc", value: "new", ops: ops))
        XCTAssertEqual(store["svc"], "new")
        XCTAssertEqual(addCalls, 1)  // 成功時は復元 add を呼ばない
    }

    /// delete→add の add が失敗したとき、旧値を復元して false を返す（資格情報の消失防止）
    func testWriteFailureRestoresPreviousValue() {
        var store: [String: String] = ["svc": "old"]
        var addArgs: [String] = []
        let ops = Keychain.Ops(
            read: { store[$0] },
            delete: { store[$0] = nil },
            add: { svc, val in
                addArgs.append(val)
                // 新値の追加は失敗させ、旧値の復元（2 回目の add）だけ成功させる
                if val == "new" { return false }
                store[svc] = val
                return true
            }
        )

        XCTAssertFalse(Keychain.write(service: "svc", value: "new", ops: ops))
        XCTAssertEqual(store["svc"], "old")          // 旧値が復元されている
        XCTAssertEqual(addArgs, ["new", "old"])      // 新値追加(失敗)→旧値復元の順
    }

    /// 旧値が無い（新規保存）の add 失敗では復元 add を呼ばない
    func testWriteFailureNoPreviousDoesNotRestore() {
        var store: [String: String] = [:]
        var addCalls = 0
        let ops = Keychain.Ops(
            read: { store[$0] },
            delete: { store[$0] = nil },
            add: { _, _ in addCalls += 1; return false }
        )

        XCTAssertFalse(Keychain.write(service: "svc", value: "new", ops: ops))
        XCTAssertNil(store["svc"])
        XCTAssertEqual(addCalls, 1)  // 復元 add は呼ばれない
    }
}
