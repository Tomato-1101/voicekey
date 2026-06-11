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
    static func condense(_ samples: [Float], padMs: Int = 250) -> [Float]? {
        // 1. フレームごとの発話判定（hasSpeechEnergy と同じエネルギー基準）
        var active: [Bool] = []
        for start in stride(from: 0, to: samples.count - frameLen, by: frameLen) {
            var sum: Float = 0
            for i in start..<(start + frameLen) {
                sum += samples[i] * samples[i]
            }
            active.append(sqrt(sum / Float(frameLen)) >= energyThreshold)
        }

        // 2. 発話フレームの連続区間を前後 pad 付きのサンプル範囲に変換
        let pad = sampleRate * padMs / 1000
        var regions: [(lower: Int, upper: Int)] = []
        var runStart: Int? = nil
        for (i, isActive) in active.enumerated() {
            if isActive {
                if runStart == nil { runStart = i }
            } else if let start = runStart {
                regions.append((max(0, start * frameLen - pad),
                                min(samples.count, i * frameLen + pad)))
                runStart = nil
            }
        }
        if let start = runStart {
            regions.append((max(0, start * frameLen - pad),
                            min(samples.count, active.count * frameLen + pad)))
        }
        guard !regions.isEmpty else { return nil }

        // 3. 近接区間をマージ（区間の間に残る無音が keptGapSec 以下なら切らずに繋げたまま）
        let keptGap = Int(Double(sampleRate) * keptGapSec)
        var merged: [(lower: Int, upper: Int)] = [regions[0]]
        for region in regions.dropFirst() {
            if region.lower - merged[merged.count - 1].upper <= keptGap {
                merged[merged.count - 1].upper = max(merged[merged.count - 1].upper, region.upper)
            } else {
                merged.append(region)
            }
        }

        // 4. 区間を連結（各接合部には前後 pad ぶん＝計 keptGapSec の実無音が残る）
        var result: [Float] = []
        result.reserveCapacity(merged.reduce(0) { $0 + ($1.upper - $1.lower) })
        for region in merged {
            result.append(contentsOf: samples[region.lower..<region.upper])
        }
        return result
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
