//
//  OpenAILiveResampleTests.swift
//  OpenAILiveTranscriber の 16kHz→24kHz リサンプルの単体テスト
//
//  OpenAI Realtime は 24kHz 以上の PCM しか受け付けないため、録音の 16kHz を
//  1.5 倍に伸ばして送っている。ここで検証したいのは「チャンク境界に段差が出ないこと」。
//  段差はクリック音になって認識精度を落とすが、実音声では気づきにくく退行しやすいので
//  純関数レベルで固定する。ネットワークもマイクも使わない。
//

import XCTest
@testable import voicekey

final class OpenAILiveResampleTests: XCTestCase {

    private func makeTranscriber() -> OpenAILiveTranscriber {
        OpenAILiveTranscriber(model: "gpt-live-transcribe", language: "ja")
    }

    /// 24000/16000 = 1.5 倍のサンプル数になる（1 チャンクあたり ±1 の丸め誤差は許容）
    func testUpsampleRaisesSampleCountByOneAndHalf() {
        let t = makeTranscriber()
        let input = [Float](repeating: 0.5, count: 1600)  // 16kHz で 100ms
        let out = t.upsample(input)
        XCTAssertEqual(Double(out.count), 2400, accuracy: 2, "100ms ぶんは 24kHz で 2400 サンプルになる")
    }

    /// 直流（一定値）を入れたら出力も同じ一定値。チャンクをまたいでも先頭が 0 に落ちない
    /// （＝前チャンク末尾の持ち越しが効いていること）。
    func testConstantSignalStaysConstantAcrossChunks() {
        let t = makeTranscriber()
        let chunk = [Float](repeating: 0.5, count: 160)
        _ = t.upsample(chunk)              // 1 チャンク目（先頭は prev=0 からの立ち上がりを含む）
        let second = t.upsample(chunk)     // 2 チャンク目は完全に定常のはず
        for v in second {
            XCTAssertEqual(v, 0.5, accuracy: 1e-5, "定常信号がチャンク境界で揺れてはいけない")
        }
    }

    /// 分割して渡しても、一括で渡したのと同じ波形になる（境界の連続性の本命）。
    /// 傾き一定のランプは線形補間で厳密に再現できるため、境界の段差がそのまま差分に出る。
    func testChunkedOutputMatchesSingleShot() {
        let n = 960
        let ramp = (0..<n).map { Float($0) / Float(n) }  // 0.0 → 1.0 の直線

        let whole = makeTranscriber().upsample(ramp)

        let chunked = makeTranscriber()
        var pieces: [Float] = []
        for start in stride(from: 0, to: n, by: 160) {
            pieces += chunked.upsample(Array(ramp[start..<min(start + 160, n)]))
        }

        XCTAssertEqual(pieces.count, whole.count, "分割しても総サンプル数は変わらない")
        for (i, (a, b)) in zip(pieces, whole).enumerated() {
            XCTAssertEqual(a, b, accuracy: 1e-5, "分割の境界（index \(i)）で波形がずれてはいけない")
        }
    }
}
