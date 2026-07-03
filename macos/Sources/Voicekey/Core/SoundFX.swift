//
//  SoundFX.swift
//  録音開始/停止の操作音（コード合成・音源ファイル不要）
//
//  なぜコード合成か: 音源ファイルをバンドルに増やさず、開始=上昇・停止=下降の
//  短いブリップを正弦波で生成する。マイク録音の AVAudioEngine とは完全に独立した
//  再生専用エンジンを持ち、再生は fire-and-forget（音声入力パイプラインに待ちを足さない）。
//  失敗（オーディオ経路の一時不整合等）は握りつぶす＝音が鳴らないだけで実害はない。
//

import AVFoundation
import Foundation

/// 操作音の再生。`SoundFX.shared.play(.start / .stop)` を呼ぶだけ（撃ちっぱなし）。
final class SoundFX {

    /// アプリ全体で 1 つの再生エンジンを使い回す（毎回作ると起動コストがかかる）
    static let shared = SoundFX()

    /// 鳴らす操作音の種類
    enum Kind {
        /// 録音開始（2 音上昇のブリップ）
        case start
        /// 録音停止（下降のブリップ）
        case stop
    }

    /// 再生用の合成音を作る/鳴らすシリアルキュー。呼び出しスレッドをブロックしないため、
    /// バッファ生成・スケジューリングをすべてこのキューへ逃がす（クリティカルパス外）。
    private let queue = DispatchQueue(label: "com.voicekey.soundfx", qos: .userInitiated)

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    /// 合成に使うフォーマット（モノラル 44.1kHz float）
    private let format = AVAudioFormat(standardFormatWithSampleRate: 44100, channels: 1)!
    private var isConfigured = false

    private init() {}

    /// 操作音を鳴らす（fire-and-forget）。呼び出し側は即座に戻る。
    func play(_ kind: Kind) {
        queue.async { [self] in
            guard let buffer = makeBuffer(for: kind) else { return }
            configureIfNeeded()
            // ルート変更等でエンジンが止まっていたら起動し直す（失敗は無視）
            if !engine.isRunning {
                try? engine.start()
            }
            guard engine.isRunning else { return }
            player.scheduleBuffer(buffer, at: nil, options: [], completionHandler: nil)
            if !player.isPlaying { player.play() }
        }
    }

    /// エンジンのノード接続を一度だけ行う
    private func configureIfNeeded() {
        guard !isConfigured else { return }
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: format)
        isConfigured = true
    }

    /// 種類に応じた短い正弦波ブリップを合成する。
    /// 開始=2 音上昇（低→高）、停止=下降（高→低）。両端をフェードしてクリック音を防ぐ。
    private func makeBuffer(for kind: Kind) -> AVAudioPCMBuffer? {
        let sampleRate = format.sampleRate
        // 各トーンの長さ（合計 100〜150ms に収める）
        let toneDuration = 0.065
        // 開始は 2 音（上昇）、停止は 2 音（下降）
        let freqs: [Double]
        switch kind {
        case .start: freqs = [660, 990]   // 上昇（E5→B5 相当）
        case .stop: freqs = [880, 587]    // 下降（A5→D5 相当）
        }

        let framesPerTone = Int(sampleRate * toneDuration)
        let totalFrames = framesPerTone * freqs.count
        guard let buffer = AVAudioPCMBuffer(
            pcmFormat: format, frameCapacity: AVAudioFrameCount(totalFrames)
        ) else { return nil }
        buffer.frameLength = AVAudioFrameCount(totalFrames)
        guard let samples = buffer.floatChannelData?[0] else { return nil }

        let amplitude: Float = 0.1   // 控えめな音量（程よく小さく）
        let twoPi = 2.0 * Double.pi
        // 前後 6ms を線形フェードしてプチッというクリックを消す
        let fadeFrames = Double(min(framesPerTone, Int(sampleRate * 0.006)))
        for (toneIndex, freq) in freqs.enumerated() {
            let base = toneIndex * framesPerTone
            for i in 0..<framesPerTone {
                let t = Double(i) / sampleRate
                let pos = Double(i)
                var env = 1.0
                if pos < fadeFrames { env = pos / fadeFrames }
                let tail = Double(framesPerTone) - pos
                if tail < fadeFrames { env = min(env, tail / fadeFrames) }
                samples[base + i] = Float(sin(twoPi * freq * t) * env) * amplitude
            }
        }
        return buffer
    }
}
