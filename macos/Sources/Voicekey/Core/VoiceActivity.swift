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

    /// 発話区間の先頭・末尾を返す（前後の無音トリミング用）。
    /// 長い録音の無音・ノイズ区間を API に送らないことで幻覚とレイテンシを減らす
    static func speechBounds(_ samples: [Float], padMs: Int = 250) -> Range<Int>? {
        var firstFrame: Int? = nil
        var lastFrame: Int? = nil
        var frameIndex = 0
        for start in stride(from: 0, to: samples.count - frameLen, by: frameLen) {
            var sum: Float = 0
            for i in start..<(start + frameLen) {
                sum += samples[i] * samples[i]
            }
            if sqrt(sum / Float(frameLen)) >= energyThreshold {
                if firstFrame == nil { firstFrame = frameIndex }
                lastFrame = frameIndex
            }
            frameIndex += 1
        }
        guard let first = firstFrame, let last = lastFrame else { return nil }

        let pad = sampleRate * padMs / 1000
        let lower = max(0, first * frameLen - pad)
        let upper = min(samples.count, (last + 1) * frameLen + pad)
        return lower..<upper
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

        guard let buffer = AVAudioPCMBuffer(
            pcmFormat: format,
            frameCapacity: AVAudioFrameCount(samples.count)
        ), let channel = buffer.floatChannelData?[0] else { return nil }
        samples.withUnsafeBufferPointer { src in
            channel.update(from: src.baseAddress!, count: samples.count)
        }
        buffer.frameLength = AVAudioFrameCount(samples.count)

        do {
            let request = try SNClassifySoundRequest(classifierIdentifier: .version1)
            // 1 秒窓・50% オーバーラップで短い発話も拾う
            request.windowDuration = CMTime(seconds: 1.0, preferredTimescale: 16000)
            request.overlapFactor = 0.5

            let analyzer = SNAudioStreamAnalyzer(format: format)
            let observer = SpeechObserver()
            try analyzer.add(request, withObserver: observer)
            analyzer.analyze(buffer, atAudioFramePosition: 0)
            analyzer.completeAnalysis()
            return observer.speechDetected
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

        func request(_ request: SNRequest, didProduce result: SNResult) {
            guard let classification = result as? SNClassificationResult else { return }
            if let speech = classification.classification(forIdentifier: "speech"),
               speech.confidence >= confidenceThreshold {
                speechDetected = true
            }
        }
    }
}
