//
//  GeminiTranscribeTests.swift
//  Gemini 3.5 Transcribe（Interactions API）まわりの純関数テスト
//
//  ネットワークは叩かない（課金 API のため）。実機の 1 往復は
//  `--rest-stt-test <音声> --backend gemini` ハーネスで手動確認する。
//  ここで固定するのは「実際の応答 JSON を解析できること」と、言語コード・
//  固有名詞ヒントの写し方。
//

import XCTest
@testable import voicekey

final class GeminiTranscribeTests: XCTestCase {

    // MARK: - 応答解析

    /// 実際に返ってきた応答（2026-08-27 実測）からテキストを取り出せる
    func testParsesRealResponse() {
        let json = """
        {
          "id": "v1_abc",
          "status": "completed",
          "usage": {"total_tokens": 171},
          "steps": [
            {"content": [{"text": "明日の午後3時に渋谷駅で打ち合わせをしましょう。", "type": "text"}],
             "type": "model_output"}
          ],
          "object": "interaction",
          "model": "gemini-3.5-transcribe"
        }
        """
        let text = Transcriber.parseGeminiText(Data(json.utf8))
        XCTAssertEqual(text, "明日の午後3時に渋谷駅で打ち合わせをしましょう。")
    }

    /// step が複数に割れて返っても全文を落とさずつなぐ
    func testJoinsMultipleSteps() {
        let json = """
        {"steps": [
          {"content": [{"type": "text", "text": "前半です。"}]},
          {"content": [{"type": "text", "text": "後半です。"}]}
        ]}
        """
        XCTAssertEqual(Transcriber.parseGeminiText(Data(json.utf8)), "前半です。後半です。")
    }

    /// text 以外の要素（word_info 等の注釈）は本文に混ぜない
    func testIgnoresNonTextContent() {
        let json = """
        {"steps": [{"content": [
          {"type": "word_info", "text": "捨てる"},
          {"type": "text", "text": "残す"}
        ]}]}
        """
        XCTAssertEqual(Transcriber.parseGeminiText(Data(json.utf8)), "残す")
    }

    /// 解析できない応答は nil（呼び出し側が日本語のエラーに変換する）
    func testReturnsNilForUnparsableResponse() {
        XCTAssertNil(Transcriber.parseGeminiText(Data("{\"error\": {\"code\": 400}}".utf8)))
        XCTAssertNil(Transcriber.parseGeminiText(Data("not json".utf8)))
        XCTAssertNil(Transcriber.parseGeminiText(Data("{\"steps\": []}".utf8)))
    }

    // MARK: - 言語コード

    /// 設定の言語コードは BCP-47（地域付き）へ寄せる
    func testLanguageCodeGetsRegion() {
        XCTAssertEqual(Transcriber.geminiLanguageCode("ja"), "ja-JP")
        XCTAssertEqual(Transcriber.geminiLanguageCode("en"), "en-US")
        XCTAssertEqual(Transcriber.geminiLanguageCode("ko"), "ko-KR")
    }

    /// 既に地域付き・スクリプト付きならそのまま使う
    func testLanguageCodeKeepsExplicitRegion() {
        XCTAssertEqual(Transcriber.geminiLanguageCode("en-GB"), "en-GB")
        XCTAssertEqual(Transcriber.geminiLanguageCode("zh-Hans"), "zh-Hans")
    }

    // MARK: - 固有名詞ヒント

    /// ユーザー辞書のプロンプトは語のリストへ分解して渡す
    func testCustomVocabularySplitsWords() {
        XCTAssertEqual(
            Transcriber.customVocabulary(from: "voicekey、Deepgram, 渋谷"),
            ["voicekey", "Deepgram", "渋谷"]
        )
    }

    /// 空白だけの要素は落とす（空リストなら送らない側の判定に使う）
    func testCustomVocabularyDropsEmpty() {
        XCTAssertEqual(Transcriber.customVocabulary(from: "  "), [])
        XCTAssertEqual(Transcriber.customVocabulary(from: "語, ,,語2"), ["語", "語2"])
    }
}
