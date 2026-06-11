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
import AudioToolbox
import CoreAudio
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

    /// 16kHz モノラルチャンクの逐次通知（ストリーミング送信用、audio スレッドから呼ばれる）。
    /// ストリーミング録音時のみ設定し、終了時に nil へ戻す
    var chunkHandler: (([Float]) -> Void)?

    /// 使用する入力デバイスの UID（空ならシステム既定）。録音開始のたびに参照する
    var inputDeviceUID: String = ""

    private let engine = AVAudioEngine()
    /// エンジン操作を直列化するキュー（ブロックしてもここだけ）
    private let queue = DispatchQueue(label: "com.voicekey.audio-control")

    private var samples: [Float] = []
    private let samplesLock = NSLock()
    private var recording = false
    private var lastLevelTime: TimeInterval = 0

    /// 次回 start() を高速化するウォームアップ（マイクは起動しない）。
    /// inputNode へのアクセスで CoreAudio の入力ユニットを初期化し、prepare で
    /// エンジンのリソースを事前確保する。アプリ起動時・録音停止後に呼ぶことで、
    /// 押した瞬間から声を取りこぼさないよう録音開始までの遅延を最小化する
    func prewarm() {
        queue.async { [self] in
            guard !recording else { return }
            _ = engine.inputNode.inputFormat(forBus: 0)
            engine.prepare()
        }
    }

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

            // 指定があれば入力デバイスを切り替える（UID が解決できなければ既定のまま）。
            // エンジン停止中に設定する必要があるが、start は常に stop 後に呼ばれる
            if !inputDeviceUID.isEmpty {
                if let deviceID = AudioDevices.deviceID(forUID: inputDeviceUID) {
                    do {
                        try input.auAudioUnit.setDeviceID(deviceID)
                    } catch {
                        log.warning("入力デバイスの切り替えに失敗（既定を使用）: \(error.localizedDescription)")
                    }
                } else {
                    log.warning("指定の入力デバイスが見つかりません（既定を使用）")
                }
            }

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

            // 次回の録音開始を速くするため、停止直後にリソースを確保し直しておく
            // （completion 後に行うので文字起こしの開始は一切遅らせない）
            engine.prepare()
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

        // ストリーミング送信用に逐次チャンクを渡す（全バッファ蓄積とは独立）。
        // ストリーミングが失敗しても samples には残るため REST フォールバックが効く
        chunkHandler?(chunk)

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
