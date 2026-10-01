//
//  ApiKeyResolutionTests.swift
//  API キーの探索順（アプリ → 環境変数 → 中央 Keychain）と項目対応表の単体テスト
//
//  実 Keychain・環境変数・中央 Keychain には一切触れない（探索順は純関数 `Keychain.resolve` に
//  クロージャを注入して検証する）。鍵の値はダミー文字列だけを使う。
//

import XCTest
@testable import voicekey

final class ApiKeyResolutionTests: XCTestCase {

    // MARK: - 探索順

    /// 3 か所すべてにあれば、設定 › API キー で入れたアプリ項目が最優先
    func testAppValueWins() {
        let found = Keychain.resolve(app: { "app-value" }, env: { "env-value" }, central: { "central-value" })
        XCTAssertEqual(found?.value, "app-value")
        XCTAssertEqual(found?.source, .app)
    }

    /// アプリ項目が無ければ環境変数、それも無ければ中央 Keychain
    func testFallsBackToEnvironmentThenCentral() {
        let env = Keychain.resolve(app: { nil }, env: { "env-value" }, central: { "central-value" })
        XCTAssertEqual(env?.value, "env-value")
        XCTAssertEqual(env?.source, .environment)

        // アプリ項目が無い状態で従来どおり中央 Keychain から読める（作者の実運用の経路）
        let central = Keychain.resolve(app: { nil }, env: { nil }, central: { "central-value" })
        XCTAssertEqual(central?.value, "central-value")
        XCTAssertEqual(central?.source, .centralKeychain)
    }

    /// 空文字は未設定扱いで次へ進む。どこにも無ければ nil
    func testEmptyValuesAreSkipped() {
        let found = Keychain.resolve(app: { "" }, env: { "" }, central: { "central-value" })
        XCTAssertEqual(found?.source, .centralKeychain)
        XCTAssertNil(Keychain.resolve(app: { nil }, env: { "" }, central: { nil }))
    }

    /// 手前で見つかったら後段（中央 Keychain の子プロセス起動など）は評価しない
    func testLaterSourcesAreNotEvaluatedWhenFound() {
        var envCalled = false
        var centralCalled = false
        _ = Keychain.resolve(
            app: { "app-value" },
            env: { envCalled = true; return "env-value" },
            central: { centralCalled = true; return "central-value" }
        )
        XCTAssertFalse(envCalled)
        XCTAssertFalse(centralCalled)
    }

    // MARK: - 対応表

    /// 設定画面の項目と文字起こし側の Keychain 項目・変数名が一致する
    /// （画面で保存したキーを文字起こしがそのまま読めること）
    func testItemsMatchTranscriptionBackends() {
        let pairs: [(Backend, ApiKeyItem)] = [
            (.soniox, .soniox), (.openai, .openai), (.openaiLive, .openai),
            (.azureMAI, .azureKey), (.elevenlabs, .elevenlabs), (.groq, .groq),
        ]
        for (backend, item) in pairs {
            XCTAssertEqual(Keychain.service(for: backend), item.appService, "\(backend)")
            XCTAssertEqual(Keychain.keyVariableName(for: backend), item.variableName, "\(backend)")
        }
    }

    /// 字幕翻訳のプロバイダーも同じ項目を使う（Groq / OpenAI は文字起こしと共用）
    func testCaptionProvidersMatchItems() {
        for provider in APIProvider.allCases {
            XCTAssertEqual(provider.keyItem.variableName, provider.rawValue, "\(provider)")
        }
        XCTAssertEqual(APIProvider.groq.keyItem.appService, Keychain.service(for: .groq))
        XCTAssertEqual(APIProvider.openai.keyItem.appService, Keychain.service(for: .openai))
        XCTAssertEqual(APIProvider.gemini.keyItem.appService, "voicekey.Gemini")
    }

    /// アプリ項目名・変数名に重複が無い（別の項目のキーを上書きしない）
    func testItemNamesAreUnique() {
        let services = ApiKeyItem.allCases.map(\.appService)
        let names = ApiKeyItem.allCases.map(\.variableName)
        XCTAssertEqual(Set(services).count, services.count)
        XCTAssertEqual(Set(names).count, names.count)
    }

    /// 状態表示の文言（設定 › API キー の各行）
    func testStatusLabels() {
        XCTAssertEqual(APIKeySource.app.statusLabel, "アプリに保存済み")
        XCTAssertEqual(APIKeySource.centralKeychain.statusLabel, "中央 Keychain から読込")
        XCTAssertEqual(APIKeySource.environment.statusLabel, "環境変数から読込")
    }
}
