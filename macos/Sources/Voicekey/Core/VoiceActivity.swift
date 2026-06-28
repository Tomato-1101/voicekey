//
//  VoiceActivity.swift
//  音量正規化と発話検出（VAD）
//
//  - 正規化: RMS を -20dBFS に合わせる。ゲイン上限 +20dB
//    （上限がないと「ほぼ無音＋ノイズフロア」の録音でノイズだけが増幅され、
//    API が架空のテキストを生成する＝幻覚の主要因になる）
//  - 発話検出: Apple SoundAnalysis の組み込み分類器（オンデバイス ML）で
//    speech クラスを判定。利用できない場合はエネルギーベースにフォールバック
//  - トリミング: エネルギーベースで前後の無音区間を切り落とす
//

import Foundation
import SoundAnalysis
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "vad")

enum VoiceActivity {

    private static let sampleRate = 16000
    /// 目標 RMS（-20 dBFS）
    private static let targetRms: Float = 0.1
    /// ピーク上限（-3 dBFS）
    private static let peakCeiling: Float = 0.708
    /// ゲイン上限（+20 dB = 10 倍）
    private static let maxGain: Float = 10.0
    /// エネルギー VAD のフレーム長（30ms）
    private static let frameLen = 480
    /// 発話とみなす RMS しきい値（正規化後の音声に対して）
    private static let energyThreshold: Float = 0.02
    /// 発話とみなす連続フレームの最小長。離れた単発クリックノイズ（1 フレームだけ
    /// しきい値超え）を発話としないため、各 run の連続長で判定する（Python _MIN_SPEECH_FRAMES と一致）。
    private static let minSpeechFrames = 2

    // MARK: - 音量正規化

    /// Peak+RMS ハイブリッド方式で音量を一定化する（ゲイン上限 +20dB）
    static func normalize(_ samples: [Float]) -> [Float] {
        guard !samples.isEmpty else { return samples }
        var sum: Double = 0
        for s in samples { sum += Double(s) * Double(s) }
        let rms = Float(sqrt(sum / Double(samples.count)))
        guard rms > 1e-6 else { return samples }  // 完全無音はそのまま（ゲイン発散防止）

        // ノイズフロアだけの録音を増幅し尽くさないよう上限を設ける
        var gain = min(targetRms / rms, maxGain)

        // ピーク制限（音割れ防止のヘッドルーム）
        var peak: Float = 0
        for s in samples { peak = max(peak, abs(s)) }
        if peak * gain > peakCeiling {
            gain = peakCeiling / peak
        }
        return samples.map { $0 * gain }
    }

    // MARK: - 発話検出

    /// 発話が含まれるかを判定する。
    /// SoundAnalysis（オンデバイス ML 分類器）を優先し、失敗時はエネルギー判定
    static func hasSpeech(_ samples: [Float]) async -> Bool {
        guard samples.count >= frameLen * 2 else { return false }
        // 分類器の解析窓（1 秒）に満たない短い発話は分類結果が出ない／不安定なため、
        // 最初からエネルギー判定を使う（短い一言が「声なし」と誤判定されるのを防ぐ）
        if samples.count < Int(Double(sampleRate) * 1.2) {
            return hasSpeechEnergy(samples)
        }
        if let result = await classifySpeech(samples) {
            return result
        }
        return hasSpeechEnergy(samples)
    }

    /// エネルギーベースの発話判定（フォールバック用）。
    /// しきい値超えフレームが 3 つ以上（≒90ms 以上）で発話とみなす
    static func hasSpeechEnergy(_ samples: [Float]) -> Bool {
        var active = 0
        for start in stride(from: 0, to: samples.count - frameLen, by: frameLen) {
            var sum: Float = 0
            for i in start..<(start + frameLen) {
                sum += samples[i] * samples[i]
            }
            if sqrt(sum / Float(frameLen)) >= energyThreshold {
                active += 1
                if active >= 3 { return true }
            }
        }
        return false
    }

    /// 発話間に保持する無音の最大長（0.5 秒）。
    /// ポーズは句読点・文区切りの推定材料になるため完全には消さない
    private static let keptGapSec = 0.5

    /// 前後の無音トリミングに加えて、発話間の長い無音を圧縮した音声を返す。
    ///
    /// 各発話区間の前後に padMs の余白を残し、区間間の無音は最大 keptGapSec まで保持する
    /// （元の無音が約 1 秒以下ならそのまま。それより長い分だけを切り落とす）。
    /// 音声が短くなる分だけアップロードと API の処理時間が縮む。語頭・語尾は余白で守られ、
    /// ポーズの手がかりも残るため精度には影響しない。発話が無ければ nil
    /// 分割並列送信用: この長さを超える無音をセグメント境界にする（秒）
    private static let splitGapSec = 0.7
    /// 分割並列送信用: 全体がこれ未満なら分割しない（秒）
    private static let minSplitSec = 12.0
    /// 分割並列送信用: これ未満のセグメントは直前に結合して細切れを防ぐ（秒）
    private static let minSegmentSec = 2.0

    /// 発話フレームの連続区間を、前後 padMs の余白付きサンプル範囲にして返す（マージ前）。
    /// condense と segment の共通前処理（エネルギー基準は hasSpeechEnergy と同一）。
    private static func speechRegions(_ samples: [Float], padMs: Int) -> [(lower: Int, upper: Int)] {
        var active: [Bool] = []
        for start in stride(from: 0, to: samples.count - frameLen, by: frameLen) {
            var sum: Float = 0
            for i in start..<(start + frameLen) {
                sum += samples[i] * samples[i]
            }
            active.append(sqrt(sum / Float(frameLen)) >= energyThreshold)
        }
        let pad = sampleRate * padMs / 1000
        var regions: [(lower: Int, upper: Int)] = []
        var runStart: Int? = nil
        for (i, isActive) in active.enumerated() {
            if isActive {
                if runStart == nil { runStart = i }
            } else if let start = runStart {
                // 連続長が minSpeechFrames 未満の run（≒単発クリックノイズ）は採らない（#22）
                if i - start >= minSpeechFrames {
                    regions.append((max(0, start * frameLen - pad),
                                    min(samples.count, i * frameLen + pad)))
                }
                runStart = nil
            }
        }
        if let start = runStart, active.count - start >= minSpeechFrames {
            regions.append((max(0, start * frameLen - pad),
                            min(samples.count, active.count * frameLen + pad)))
        }
        return regions
    }

    /// 近接する区間を gapSec 以下の無音でマージする（gap を超える無音で区切られる）
    private static func mergeRegions(
        _ regions: [(lower: Int, upper: Int)], gapSec: Double
    ) -> [(lower: Int, upper: Int)] {
        guard let first = regions.first else { return [] }
        let gap = Int(Double(sampleRate) * gapSec)
        var merged: [(lower: Int, upper: Int)] = [first]
        for region in regions.dropFirst() {
            if region.lower - merged[merged.count - 1].upper <= gap {
                merged[merged.count - 1].upper = max(merged[merged.count - 1].upper, region.upper)
            } else {
                merged.append(region)
            }
        }
        return merged
    }

    static func condense(_ samples: [Float], padMs: Int = 250) -> [Float]? {
        let regions = speechRegions(samples, padMs: padMs)
        guard !regions.isEmpty else { return nil }
        // 近接区間をマージ（区間の間に残る無音が keptGapSec 以下なら切らずに繋げたまま）
        let merged = mergeRegions(regions, gapSec: keptGapSec)
        // 区間を連結（各接合部には前後 pad ぶん＝計 keptGapSec の実無音が残る）
        var result: [Float] = []
        result.reserveCapacity(merged.reduce(0) { $0 + ($1.upper - $1.lower) })
        for region in merged {
            result.append(contentsOf: samples[region.lower..<region.upper])
        }
        return result
    }

    /// 長い音声を無音区間で分割したセグメント配列を返す。
    ///
    /// 分割点は splitGapSec を超える無音の中だけなので、語の途中では切れない
    /// （＝精度に実用上影響しない）。各セグメントは前後 pad 付きで独立に文字起こしできる。
    /// 全体が短い（minSplitSec 未満）／区間が 1 つのときは空配列を返し、
    /// 呼び出し側は従来どおり 1 本送信にフォールバックする。
    static func segment(_ samples: [Float], padMs: Int = 250) -> [[Float]] {
        let total = Double(samples.count) / Double(sampleRate)
        guard total >= minSplitSec else { return [] }
        let regions = speechRegions(samples, padMs: padMs)
        let merged = mergeRegions(regions, gapSec: splitGapSec)
        guard merged.count >= 2 else { return [] }
        // 短すぎるセグメントは直前に結合して細切れ・送信オーバーヘッドを抑える
        let minLen = Int(Double(sampleRate) * minSegmentSec)
        let slices = merged.map { Array(samples[$0.lower..<$0.upper]) }
        let segments = mergeShortSegments(slices, minLen: minLen)
        return segments.count >= 2 ? segments : []
    }

    /// 短すぎるセグメント（minLen 未満）を直前へ結合する（#23）。
    ///
    /// 「現在の slice が短い」場合（末尾・中間の短区間）と「直前の slice が短い」場合
    /// （先頭の短区間）の両方を直前へ結合する。現在 slice 長を見ないと、長区間の後ろに
    /// 続く minSegmentSec 未満の短区間が独立したまま残ってしまう。Python vad.segment と同条件。
    static func mergeShortSegments(_ slices: [[Float]], minLen: Int) -> [[Float]] {
        var segments: [[Float]] = []
        for slice in slices {
            if let last = segments.last, last.count < minLen || slice.count < minLen {
                segments[segments.count - 1] = last + slice
            } else {
                segments.append(slice)
            }
        }
        return segments
    }

    /// セグメントごとの文字起こし結果を、境界の文字種を見て結合する。
    /// 両端のどちらかが CJK ならスペース無し、英数字同士ならスペース 1 個で繋ぐ。
    static func joinSegments(_ texts: [String]) -> String {
        var result = ""
        for piece in texts {
            let p = piece.trimmingCharacters(in: .whitespacesAndNewlines)
            if p.isEmpty { continue }
            if result.isEmpty {
                result = p
                continue
            }
            if isCJK(result.last!) || isCJK(p.first!) {
                result += p
            } else {
                result += " " + p
            }
        }
        return result
    }

    /// 1 文字がひらがな・カタカナ・漢字（および全角/半角形）かを判定する
    private static func isCJK(_ ch: Character) -> Bool {
        guard let scalar = ch.unicodeScalars.first else { return false }
        let v = scalar.value
        return (0x3040...0x30FF).contains(v)
            || (0x3400...0x4DBF).contains(v)
            || (0x4E00...0x9FFF).contains(v)
            || (0xF900...0xFAFF).contains(v)
            || (0xFF00...0xFFEF).contains(v)
    }

    // MARK: - SoundAnalysis（オンデバイス ML）

    /// Apple の組み込みサウンド分類器で speech クラスの有無を判定する。
    /// 分類器が使えない場合は nil（呼び出し側でエネルギー判定にフォールバック）
    private static func classifySpeech(_ samples: [Float]) async -> Bool? {
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: Double(sampleRate),
            channels: 1,
            interleaved: false
        ) else { return nil }

        do {
            let request = try SNClassifySoundRequest(classifierIdentifier: .version1)
            // 1 秒窓・50% オーバーラップで短い発話も拾う
            request.windowDuration = CMTime(seconds: 1.0, preferredTimescale: 16000)
            request.overlapFactor = 0.5

            let analyzer = SNAudioStreamAnalyzer(format: format)
            let observer = SpeechObserver()
            try analyzer.add(request, withObserver: observer)

            // 0.5 秒ずつ流し、speech を検出した時点で打ち切る（早期終了）。
            // 判定は「どこかに speech があるか」の OR なので結果は全量解析と同一。
            // 発話は冒頭にあることが多く、長い録音ほど ML 推論時間を大きく削れる
            let chunkLen = sampleRate / 2
            var pos = 0
            while pos < samples.count {
                let len = min(chunkLen, samples.count - pos)
                guard let chunk = AVAudioPCMBuffer(
                    pcmFormat: format, frameCapacity: AVAudioFrameCount(len)
                ), let chunkChannel = chunk.floatChannelData?[0] else { return nil }
                samples.withUnsafeBufferPointer { src in
                    chunkChannel.update(from: src.baseAddress! + pos, count: len)
                }
                chunk.frameLength = AVAudioFrameCount(len)
                analyzer.analyze(chunk, atAudioFramePosition: AVAudioFramePosition(pos))
                if observer.speechDetected { return true }
                pos += len
            }
            analyzer.completeAnalysis()
            if observer.speechDetected { return true }
            // 解析窓が一度も評価されなかった（短すぎる等で結果ゼロ）場合は
            // 「声なし」ではなく「判定不能」としてエネルギー判定に委ねる
            guard observer.resultCount > 0 else { return nil }
            return false
        } catch {
            log.warning("SoundAnalysis が利用できません（エネルギー判定へ）: \(error.localizedDescription)")
            return nil
        }
    }

    /// SoundAnalysis の結果から speech クラスの検出を記録するオブザーバ
    private final class SpeechObserver: NSObject, SNResultsObserving {
        /// speech とみなす信頼度しきい値
        private let confidenceThreshold = 0.6
        private(set) var speechDetected = false
        /// 産出された分類結果の数（0 なら判定不能としてフォールバックさせる）
        private(set) var resultCount = 0

        func request(_ request: SNRequest, didProduce result: SNResult) {
            guard let classification = result as? SNClassificationResult else { return }
            resultCount += 1
            if let speech = classification.classification(forIdentifier: "speech"),
               speech.confidence >= confidenceThreshold {
                speechDetected = true
            }
        }
    }
}
