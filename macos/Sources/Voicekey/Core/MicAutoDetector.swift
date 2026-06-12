//
//  MicAutoDetector.swift
//  マイク自動検出（全入力デバイスを同時監視し、喋った声が入ったマイクを選ぶ）
//
//  判定アルゴリズム:
//      各デバイスで約 4 秒間、30ms フレームごとの RMS を収集し、
//      score = RMS の 90 パーセンタイル − 10 パーセンタイル とする。
//      喋り声は音量変動が大きく（スコア大）、定常ノイズやループバック系の
//      仮想デバイスは変動が小さい（スコア小）ため、単純な最大 RMS より誤選択が少ない。
//
//  設計:
//      デバイスごとに独立した AVAudioEngine を同時に起動する（使い捨て）。
//      AppController が持つ録音用エンジンには一切触れない。
//      開けないデバイスは起動失敗としてスキップし、起動がハングしたデバイスは
//      計測対象に加わらないだけで全体は締め切り時刻に必ず完了する。
//

import AVFoundation
import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "mic-autodetect")

enum MicAutoDetector {

    /// 検出結果（スコア最大のデバイス）
    struct Detection {
        let device: AudioInputDevice
        let score: Float
    }

    /// 監視時間（秒）。ボタンを押してから喋り終わるまでの猶予
    static let duration: TimeInterval = 4.0

    /// RMS を集計するフレーム長（秒）
    static let frameSeconds: Double = 0.03

    /// 発話ありとみなすスコア（p90 − p10）の下限。
    /// 定常ノイズのみのデバイスは概ね 0.001 未満、マイクへの発話は 0.01 以上になる
    static let scoreThreshold: Float = 0.005

    /// 全入力デバイスを duration 秒監視し、発話が検出されたデバイスを返す。
    /// 完了時に completion がメインスレッドで呼ばれる（検出なしは nil）。
    static func detect(completion: @escaping (Detection?) -> Void) {
        let devices = AudioDevices.inputDevices()
        guard !devices.isEmpty else {
            DispatchQueue.main.async { completion(nil) }
            return
        }

        let probesLock = NSLock()
        var probes: [Probe] = []
        var closed = false

        // 全デバイスのエンジンを並行起動する。起動に成功したものだけが計測対象
        for device in devices {
            DispatchQueue.global(qos: .userInitiated).async {
                let probe = Probe(device: device)
                guard probe.start() else { return }
                probesLock.lock()
                if closed {
                    // 締め切り後にようやく起動できた遅いデバイス（Bluetooth 等）。
                    // 集計対象外なので、マイクを掴んだまま放置しないよう即座に止める
                    probesLock.unlock()
                    probe.stop()
                    return
                }
                probes.append(probe)
                probesLock.unlock()
            }
        }

        // 起動のばらつき分 0.5 秒の余裕を持たせた締め切りで集計する
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + duration + 0.5) {
            probesLock.lock()
            closed = true
            let started = probes
            probesLock.unlock()

            var best: Detection?
            for probe in started {
                probe.stop()
                let score = Self.score(of: probe.rmsFrames())
                log.info("自動検出スコア \(probe.device.name, privacy: .public): \(score)")
                if score >= scoreThreshold, score > (best?.score ?? -1) {
                    best = Detection(device: probe.device, score: score)
                }
            }
            DispatchQueue.main.async { completion(best) }
        }
    }

    /// RMS フレーム列から発話らしさスコア（p90 − p10）を計算する
    static func score(of rmsFrames: [Float]) -> Float {
        // フレームが少なすぎる場合は判定不能（起動直後に終わったデバイス等）
        guard rmsFrames.count >= 10 else { return 0 }
        let sorted = rmsFrames.sorted()
        let p90 = sorted[Int(Double(sorted.count - 1) * 0.9)]
        let p10 = sorted[Int(Double(sorted.count - 1) * 0.1)]
        return p90 - p10
    }

    // MARK: - デバイス 1 台分の監視

    /// 1 デバイス専用の使い捨て AVAudioEngine。タップで 30ms フレームの RMS を貯める
    private final class Probe {
        let device: AudioInputDevice
        private let engine = AVAudioEngine()
        private let lock = NSLock()
        private var rms: [Float] = []
        private var pending: [Float] = []
        private var frameLength = 0

        init(device: AudioInputDevice) {
            self.device = device
        }

        /// エンジンを起動してタップを仕掛ける（失敗したら false）
        func start() -> Bool {
            let input = engine.inputNode
            do {
                try input.auAudioUnit.setDeviceID(device.id)
            } catch {
                log.info("スキップ（デバイス切替失敗）: \(self.device.name, privacy: .public)")
                return false
            }
            let format = input.inputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0 else { return false }
            frameLength = max(1, Int(format.sampleRate * MicAutoDetector.frameSeconds))

            input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buffer, _ in
                self?.append(buffer)
            }
            do {
                engine.prepare()
                try engine.start()
                return true
            } catch {
                input.removeTap(onBus: 0)
                log.info("スキップ（起動失敗）: \(self.device.name, privacy: .public)")
                return false
            }
        }

        func stop() {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }

        func rmsFrames() -> [Float] {
            lock.lock()
            defer { lock.unlock() }
            return rms
        }

        /// タップ（audio スレッド）からの追記。ch0 を 30ms フレームに切って RMS 化する
        private func append(_ buffer: AVAudioPCMBuffer) {
            guard let channel = buffer.floatChannelData?[0] else { return }
            let samples = Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
            lock.lock()
            defer { lock.unlock() }
            pending.append(contentsOf: samples)
            while pending.count >= frameLength {
                let frame = pending.prefix(frameLength)
                pending.removeFirst(frameLength)
                let sum = frame.reduce(Float(0)) { $0 + $1 * $1 }
                rms.append(sqrt(sum / Float(frameLength)))
            }
        }
    }
}
