//
//  BackendLabelTests.swift
//  Backend の表示名（製品版の特徴名 / personal の実モデル名）の単体テスト
//
//  personal（自分用）の設定 UI は特徴名で包まず「実プロバイダー名 + 実際に動くモデル名」を
//  そのまま出す（2026-08-02 ユーザー指示）。表示だけの話に見えるが、モデルを切り替えたのに
//  古い名前が出る／既定モデルと表示がずれる、といった退行は気づきにくいので固定する。
//

import XCTest
@testable import voicekey

final class BackendLabelTests: XCTestCase {

    /// personal 表示は「プロバイダー名 + 既定モデル名」
    func testDeveloperLabelShowsProviderAndModel() {
        XCTAssertEqual(Backend.deepgram.developerLabel, "Deepgram nova-3")
        XCTAssertEqual(Backend.openaiLive.developerLabel, "OpenAI gpt-live-transcribe")
        XCTAssertEqual(Backend.groq.developerLabel, "Groq whisper-large-v3-turbo")
    }

    /// 表示したモデル名と、実際に選択される（＝録音時に使われる）モデルが一致すること。
    /// バックエンド切替時に slot.model は defaultModel へ揃うので、表示もそれに一致する必要がある。
    func testDeveloperLabelMatchesModelActuallyUsed() {
        for backend in Backend.selectableCases {
            XCTAssertTrue(
                backend.developerLabel.hasSuffix(backend.defaultModel),
                "\(backend.rawValue): 表示名の末尾が実際に使われるモデル名と一致すること"
            )
        }
    }

    /// 製品版（release）の特徴名は従来どおり（personal 対応で壊していないこと）
    func testProductLabelsUnchanged() {
        XCTAssertEqual(Backend.deepgram.label, "即時入力")
        XCTAssertEqual(Backend.groq.label, "スタンダード")
    }
}
