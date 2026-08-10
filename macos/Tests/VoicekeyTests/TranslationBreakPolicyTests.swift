//
//  TranslationBreakPolicyTests.swift
//  ライブ字幕の区切りポリシー（TranslationCoordinator）の単体テスト
//
//  2026-08-10 のユーザー指摘「一文が長すぎて翻訳されるまで時間がかかりすぎる。
//  もっと文を区切れ区切れにして」への対処を恒久の回帰テストにする。
//  音声認識は 40 語を超える 1 文をまるごと 1 件の確定として渡してくることがあり（実測 199 字）、
//  それが 1 行として出るまで待たされていたのが遅さの正体だった。
//

import XCTest
import os

@testable import voicekey

/// 何も待たずに訳を返すだけのスタブ（区切り方だけを見たいので翻訳は素通し）
@available(macOS 26.0, *)
private struct EchoTranslator: Translator {
    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        "訳:" + text
    }
}

final class TranslationBreakPolicyTests: XCTestCase {

    /// 実測で問題になった長文（44 語・接続詞つなぎ・199 字）
    private let longSentence =
        "Today I woke up really early and I made some coffee because I had a lot of work to do, "
        + "so I sat down at my desk and started writing the report, "
        + "but then my friend called me and we talked for a while."

    /// 長い 1 文が節ごとに刻まれて出ること（1 行にまとめて出さない）
    func testLongSentenceIsSplitIntoClauses() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let chunks = collectSourceChunks(feeding: [longSentence])

        XCTAssertGreaterThanOrEqual(chunks.count, 4, "長文が刻まれていない: \(chunks)")
        for chunk in chunks {
            XCTAssertLessThanOrEqual(chunk.count, 48, "1 行が長すぎる: \(chunk)")
        }
        // 全文保証（[COVERAGE] と同じ観点）。刻んでも 1 文字も落とさない
        XCTAssertEqual(normalize(chunks.joined(separator: " ")), normalize(longSentence))
    }

    /// 短い 1 文はそのまま 1 行で出ること（刻みすぎて細切れにしない）
    func testShortSentenceStaysSingleLine() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let text = "That was a good idea."
        XCTAssertEqual(collectSourceChunks(feeding: [text]), [text])
    }

    /// 文末記号が来ないまま喋り続けても、節の切れ目で先に出ること
    func testKeepsBreakingWithoutSentenceEnding() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let chunks = collectSourceChunks(
            feeding: [
                "I went to the store and I bought some bread",
                "and then I walked back home because it was raining",
            ]
        )
        XCTAssertGreaterThanOrEqual(chunks.count, 2, "文末を待って溜め込んでいる: \(chunks)")
    }

    // MARK: - 補助

    /// 確定テキストを流し込み、翻訳へ回された原文の並びを取る
    ///
    /// `onSourceReady` は「1 区切りぶんを送出した」タイミングそのものなので、
    /// これを並べると刻み方がそのまま見える。
    @available(macOS 26.0, *)
    private func collectSourceChunks(feeding finals: [String]) -> [String] {
        let collected = OSAllocatedUnfairLock(initialState: [String]())
        let sequence = OSAllocatedUnfairLock(initialState: 0)
        let coordinator = TranslationCoordinator(
            translatorProvider: { EchoTranslator() },
            sequenceProvider: { sequence.withLock { $0 += 1; return $0 } }
        )
        coordinator.onSourceReady = { text in collected.withLock { $0.append(text) } }

        for text in finals { coordinator.submit(text) }
        // 残りを出し切らせてから読む（送出は専用キュー上で非同期に走る）
        coordinator.flush()
        let done = expectation(description: "flush")
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) { done.fulfill() }
        wait(for: [done], timeout: 2)

        return collected.withLock { $0 }
    }

    /// 比較用に空白の揺れを潰す
    private func normalize(_ text: String) -> String {
        text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    }
}
