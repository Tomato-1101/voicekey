//
//  AudioRecorder.swift
//  AVAudioEngine によるマイク録音（16kHz モノラル Float32）
//
//  Python 版は PortAudio のハングに苦しめられたが、AVAudioEngine は
//  CoreAudio を直接使う公式 API で start/stop が高速かつ安定している。
//  すべてのエンジン操作は専用シリアルキューで直列化し、
//  呼び出し側（ホットキーイベント）は一切ブロックしない。
//

import AVFoundation
import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "audio")

final class AudioRecorder {

    /// 出力サンプリングレート（Whisper 系 API の標準）
    static let sampleRate: Double = 16000

    /// 録音の最大サンプル数（300 秒）。永久録音によるメモリ膨張の保険
    private static let maxSamples = Int(sampleRate) * 300

    /// 音声レベル通知（0.0-1.0、約 30fps、audio スレッドから呼ばれる）
    var levelHandler: ((Float) -> Void)?

    private let engine = AVAudioEngine()
    /// エンジン操作を直列化するキュー（ブロックしてもここだけ）
    private let queue = DispatchQueue(label: "com.voicekey.audio-control")

    private var samples: [Float] = []
    private let samplesLock = NSLock()
    private var recording = false
    private var lastLevelTime: TimeInterval = 0

    /// 録音を開始する（即座に返る。結果はコールバック）
    func start(completion: @escaping (Bool) -> Void) {
        queue.async { [self] in
            guard !recording else {
                completion(true)
                return
            }
            samplesLock.lock()
            samples.removeAll(keepingCapacity: true)
            samplesLock.unlock()

            let input = engine.inputNode
            let hwFormat = input.inputFormat(forBus: 0)
            guard hwFormat.sampleRate > 0, hwFormat.channelCount > 0 else {
                log.error("入力デバイスが見つかりません")
                completion(false)
                return
            }

            // 変換先: 16kHz モノラル Float32
            guard let outFormat = AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: Self.sampleRate,
                channels: 1,
                interleaved: false
            ), let converter = AVAudioConverter(from: hwFormat, to: outFormat) else {
                log.error("音声フォーマット変換の初期化に失敗")
                completion(false)
                return
            }

            input.removeTap(onBus: 0)
            input.installTap(onBus: 0, bufferSize: 2048, format: hwFormat) { [weak self] buffer, _ in
                self?.handleBuffer(buffer, converter: converter, outFormat: outFormat)
            }

            do {
                engine.prepare()
                try engine.start()
                recording = true
                log.info("録音開始 (HW: \(Int(hwFormat.sampleRate))Hz \(hwFormat.channelCount)ch)")
                completion(true)
            } catch {
                log.error("録音開始に失敗: \(error.localizedDescription)")
                input.removeTap(onBus: 0)
                completion(false)
            }
        }
    }

    /// 録音を停止し、確定した音声データを返す（即座に返る。結果はコールバック）
    func stop(completion: @escaping ([Float]) -> Void) {
        queue.async { [self] in
            guard recording else {
                completion([])
                return
            }
            recording = false
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()

            samplesLock.lock()
            let result = samples
            samples = []
            samplesLock.unlock()

            log.info("録音停止 (samples=\(result.count), duration=\(String(format: "%.2f", Double(result.count) / Self.sampleRate))s)")
            completion(result)
        }
    }

    /// マイク使用許可を要求する（初回はシステムダイアログが出る）
    static func requestPermission() async -> Bool {
        await withCheckedContinuation { continuation in
            switch AVCaptureDevice.authorizationStatus(for: .audio) {
            case .authorized:
                continuation.resume(returning: true)
            case .notDetermined:
                AVCaptureDevice.requestAccess(for: .audio) { granted in
                    continuation.resume(returning: granted)
                }
            default:
                continuation.resume(returning: false)
            }
        }
    }

    // MARK: - audio スレッド処理

    private func handleBuffer(
        _ buffer: AVAudioPCMBuffer,
        converter: AVAudioConverter,
        outFormat: AVAudioFormat
    ) {
        guard recording else { return }

        // 16kHz モノラルへ変換
        let ratio = outFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 16
        guard let outBuffer = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else {
            return
        }
        var fed = false
        var convError: NSError?
        converter.convert(to: outBuffer, error: &convError) { _, outStatus in
            if fed {
                outStatus.pointee = .noDataNow
                return nil
            }
            fed = true
            outStatus.pointee = .haveData
            return buffer
        }
        if let convError {
            log.warning("音声変換エラー: \(convError.localizedDescription)")
            return
        }
        guard let channel = outBuffer.floatChannelData?[0], outBuffer.frameLength > 0 else {
            return
        }
        let chunk = Array(UnsafeBufferPointer(start: channel, count: Int(outBuffer.frameLength)))

        samplesLock.lock()
        if samples.count < Self.maxSamples {
            samples.append(contentsOf: chunk)
        }
        samplesLock.unlock()

        // HUD 用レベル通知（約 30fps に間引き）
        if let handler = levelHandler {
            let now = ProcessInfo.processInfo.systemUptime
            if now - lastLevelTime >= 0.033 {
                lastLevelTime = now
                var sum: Float = 0
                for s in chunk { sum += s * s }
                let rms = sqrt(sum / Float(max(1, chunk.count)))
                // 経験的に 0.15 をフルスケールとみなして 0-1 に正規化
                handler(min(1.0, rms / 0.15))
            }
        }
    }
}
