//
//  OpenAILiveE2ETests.swift
//  OpenAILiveTranscriber の実接続ハーネス（マイク不要・既定はスキップ）
//
//  「ビルドは通るが実際には一文字も返ってこない」配線ミス（session.update の形・
//  リサンプル・base64 append・commit の終端）を、実音声 + 実 API で捕まえるための
//  恒久ハーネス。ベンチ（Python）で通っても Swift 実装が同じ会話をしている保証は
//  無いので、Swift 側からも一度は実際に喋らせて確かめる。
//
//  実行:
//    OPENAI_API_KEY=sk-... VOICEKEY_LIVE_E2E=1 swift test --package-path macos \
//      --filter OpenAILiveE2ETests
//
//  既定（環境変数なし）は XCTSkip する＝通常の `swift test` はオフラインのまま。
//  キーは環境変数からのみ読む（実 Keychain には触れない）。
//

import XCTest
@testable import voicekey

final class OpenAILiveE2ETests: XCTestCase {

    /// benchmark/audio/short_ja.wav（16kHz・16bit・モノラル）を Float32 に読み込む。
    /// WAV ヘッダは同ファイル固定の 44 バイトなので、そこだけ読み飛ばす。
    private func loadShortClip() throws -> (samples: [Float], truth: String) {
        let repoRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // VoicekeyTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // macos
            .deletingLastPathComponent()  // リポジトリルート
        let audio = repoRoot.appendingPathComponent("benchmark/audio")
        let wav = try Data(contentsOf: audio.appendingPathComponent("short_ja.wav"))
        let truth = try String(contentsOf: audio.appendingPathComponent("short_ja.txt"), encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)

        let body = wav.dropFirst(44)
        var samples = [Float]()
        samples.reserveCapacity(body.count / 2)
        body.withUnsafeBytes { raw in
            for i in stride(from: 0, to: (raw.count / 2) * 2, by: 2) {
                let lo = UInt16(raw[i])
                let hi = UInt16(raw[i + 1])
                let v = Int16(bitPattern: lo | (hi << 8))
                samples.append(Float(v) / 32768.0)
            }
        }
        return (samples, truth)
    }

    /// 実音声を 100ms ずつ流し込み、確定テキストが返ることを確かめる。
    /// 「返ってきた」だけでなく、正解と比べて明らかに壊れていないこと（CER < 20%）まで見る。
    func testLiveTranscriptionReturnsText() async throws {
        guard ProcessInfo.processInfo.environment["VOICEKEY_LIVE_E2E"] != nil else {
            throw XCTSkip("実 API を叩くため既定ではスキップ（VOICEKEY_LIVE_E2E=1 で実行）")
        }
        guard let key = ProcessInfo.processInfo.environment["OPENAI_API_KEY"], !key.isEmpty else {
            throw XCTSkip("OPENAI_API_KEY が未設定（実 Keychain は読まない）")
        }

        let (samples, truth) = try loadShortClip()
        let live = OpenAILiveTranscriber(model: "gpt-live-transcribe", language: "ja")

        var interimSeen = false
        live.onInterim = { _ in interimSeen = true }

        XCTAssertTrue(live.start(apiKey: key), "WebSocket を開始できること")

        // 実時間相当（100ms ぶんずつ）で送る。まとめて送ると実運用と条件が変わる
        let chunk = 1600  // 16kHz × 100ms
        for start in stride(from: 0, to: samples.count, by: chunk) {
            live.send(Array(samples[start..<min(start + chunk, samples.count)]))
            try await Task.sleep(for: .milliseconds(100))
        }

        let text = await live.finish()
        XCTAssertFalse(text.isEmpty, "確定テキストが返ること（空なら配線が切れている）")
        XCTAssertTrue(interimSeen, "delta（ライブ字幕）が 1 回以上届くこと")

        let rate = Self.characterErrorRate(text, truth: truth)
        XCTAssertLessThan(rate, 0.2, "認識結果が壊れていないこと（実測 CER 2.7% 前後）: \(text)")
        print("E2E 結果: CER=\(String(format: "%.1f", rate * 100))% text=\(text)")
    }

    /// 文字誤り率（レーベンシュタイン距離 / 正解文字数）。benchmark/run_benchmark.py の cer と同じ定義
    private static func characterErrorRate(_ text: String, truth: String) -> Double {
        let a = Array(text), b = Array(truth)
        guard !b.isEmpty else { return a.isEmpty ? 0 : 1 }
        var prev = Array(0...b.count)
        for i in 1...max(a.count, 1) where !a.isEmpty {
            var cur = [i] + [Int](repeating: 0, count: b.count)
            for j in 1...b.count {
                cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] == b[j - 1] ? 0 : 1))
            }
            prev = cur
        }
        return Double(a.isEmpty ? b.count : prev[b.count]) / Double(b.count)
    }
}
