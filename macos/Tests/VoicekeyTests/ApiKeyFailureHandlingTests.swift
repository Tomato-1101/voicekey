//
//  ApiKeyFailureHandlingTests.swift
//  キー未設定・無効まわりの純ロジックの単体テスト
//  （401/403 の判定・設定画面を自動で開くのは 1 回だけ・貼り付けたキーの前置き除去）
//
//  実 Keychain・ネットワークには一切触れない。鍵の値はダミー文字列だけを使う。
//

import XCTest
@testable import voicekey

final class ApiKeyFailureHandlingTests: XCTestCase {

    // MARK: - 401 / 403 で「設定 › API キー」へ案内する

    /// サーバーが 401 / 403 を返したらキーの無効扱い（needsApiKey が立つ）
    func testUnauthorizedCodesNeedApiKey() {
        XCTAssertTrue(Transcriber.isInvalidKeyFailure(.error(code: "401", type: nil)))
        XCTAssertTrue(Transcriber.isInvalidKeyFailure(.error(code: "403", type: "unauthorized")))
    }

    /// キーと関係ない失敗（レート制限・サーバー障害・切断・時間切れ）では案内しない
    func testOtherFailuresDoNotNeedApiKey() {
        XCTAssertFalse(Transcriber.isInvalidKeyFailure(.error(code: "429", type: nil)))
        XCTAssertFalse(Transcriber.isInvalidKeyFailure(.error(code: "500", type: nil)))
        XCTAssertFalse(Transcriber.isInvalidKeyFailure(.error(code: nil, type: nil)))
        XCTAssertFalse(Transcriber.isInvalidKeyFailure(.disconnect))
        XCTAssertFalse(Transcriber.isInvalidKeyFailure(.timeout))
    }

    // MARK: - 設定画面を自動で開くのは起動中 1 回だけ

    /// 1 回目だけ開き、2 回目以降は開かない（HUD 通知だけ）
    func testAutoOpenOnlyOnce() {
        var policy = ApiKeyPromptPolicy()
        XCTAssertFalse(policy.hasAutoOpened)
        XCTAssertTrue(policy.shouldAutoOpenSettings())
        XCTAssertTrue(policy.hasAutoOpened)
        XCTAssertFalse(policy.shouldAutoOpenSettings())
        XCTAssertFalse(policy.shouldAutoOpenSettings())
    }

    // MARK: - 貼り付けたキーの前置き除去

    /// 設定 › API キー の全項目について `変数名=値` の前置きを外す（字幕の 3 社だけでなく）
    func testSanitizeStripsPrefixForEveryItem() {
        for item in ApiKeyItem.allCases {
            XCTAssertEqual(
                APIKeyStore.sanitize("\(item.variableName)=dummy-value"), "dummy-value",
                "\(item.variableName) の前置きが外れていない"
            )
        }
    }

    /// export・引用符・前後の空白改行がついていても値だけになる
    func testSanitizeStripsExportQuotesAndWhitespace() {
        XCTAssertEqual(APIKeyStore.sanitize("  export SONIOX_API_KEY=\"dummy-value\"\n"), "dummy-value")
        XCTAssertEqual(APIKeyStore.sanitize("ELEVENLABS_API_KEY='dummy-value'"), "dummy-value")
        XCTAssertEqual(
            APIKeyStore.sanitize("AZURE_SPEECH_ENDPOINT=https://example.cognitiveservices.azure.com"),
            "https://example.cognitiveservices.azure.com"
        )
    }

    /// 前置きの無い値はそのまま（値の途中の = などは壊さない）
    func testSanitizeKeepsPlainValue() {
        XCTAssertEqual(APIKeyStore.sanitize("dummy=value"), "dummy=value")
        XCTAssertEqual(APIKeyStore.sanitize("dummy-value"), "dummy-value")
        XCTAssertEqual(APIKeyStore.sanitize("   "), "")
    }
}
