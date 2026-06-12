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
    /// ストリーミング録音時のみ設定し、終了時に nil へ戻す。
    /// メインスレッドが書き、audio スレッドが読むため lock で同期する
    var chunkHandler: (([Float]) -> Void)? {
        get { stateLock.lock(); defer { stateLock.unlock() }; return _chunkHandler }
        set { stateLock.lock(); _chunkHandler = newValue; stateLock.unlock() }
    }

    /// 使用する入力デバイスの UID（空ならシステム既定）。録音開始のたびに参照する
    var inputDeviceUID: String {
        get { stateLock.lock(); defer { stateLock.unlock() }; return _inputDeviceUID }
        set { stateLock.lock(); _inputDeviceUID = newValue; stateLock.unlock() }
    }

    /// 録音中にデバイス構成が変わった（マイク切断等）ときの通知。
    /// エンジンは静かに止まるため、呼び出し側はこれを受けて録音を確定する
    var deviceChangedHandler: (() -> Void)?

    private let engine = AVAudioEngine()
    /// エンジン操作を直列化するキュー（ブロックしてもここだけ）
    private let queue = DispatchQueue(label: "com.voicekey.audio-control")

    private var samples: [Float] = []
    private let samplesLock = NSLock()
    /// メイン・制御キュー・audio スレッドをまたぐ可変状態の保護
    private let stateLock = NSLock()
    private var _chunkHandler: (([Float]) -> Void)?
    private var _inputDeviceUID = ""
    private var _recording = false
    private var recording: Bool {
        get { stateLock.lock(); defer { stateLock.unlock() }; return _recording }
        set { stateLock.lock(); _recording = newValue; stateLock.unlock() }
    }
    private var lastLevelTime: TimeInterval = 0

    /// engine に適用済みの入力デバイス UID（nil = 未適用または要再適用、空 = システム既定）。
    /// 毎押下の HAL 全列挙と AUHAL 再構成（実測で録音開始遅延の主因だった）を避けるため、
    /// 設定が変わったときだけ適用する。queue 上からのみ触る
    private var appliedDeviceUID: String?
    /// 「システム既定」を適用したときのデバイス ID（既定の変更に追従するための比較用）
    private var appliedDefaultID: AudioDeviceID = 0
    /// 実 IO（AudioOutputUnitStart）の初回起動コストを前払い済みか
    private var ioWarmed = false

    init() {
        // 録音中のマイク切断・サンプルレート変更等ではエンジンが静かに止まり、
        // ユーザーが喋り続けても音声が入らない。通知で検知して呼び出し側へ伝える
        NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in
            guard let self else { return }
            // 構成が変わったので次回 start() でデバイスを解決し直す
            self.queue.async { self.appliedDeviceUID = nil }
            if self.recording {
                self.deviceChangedHandler?()
            }
        }
    }

    /// 次回 start() を高速化するウォームアップ。設定デバイスの適用とリソース確保に加え、
    /// 初回のみ実 IO の起動・停止まで行う（AudioOutputUnitStart の初回コストは実測 1 秒超に
    /// なることがあり、録音時に払うと押し始めの声が欠けるため起動時に前払いする）。
    /// タップは何も記録しないダミーのため音声はどこにも残らない
    /// （アプリ起動直後にマイクインジケータが一瞬点灯するのはこのウォームアップ）
    func prewarm() {
        queue.async { [self] in
            guard !recording else { return }
            applyInputDevice()
            let input = engine.inputNode
            let hwFormat = input.inputFormat(forBus: 0)
            if !ioWarmed, hwFormat.sampleRate > 0, hwFormat.channelCount > 0 {
                ioWarmed = true
                input.installTap(onBus: 0, bufferSize: 2048, format: hwFormat) { _, _ in }
                do {
                    engine.prepare()
                    try engine.start()
                } catch {
                    log.info("IO ウォームアップを省略: \(error.localizedDescription)")
                }
                engine.stop()
                input.removeTap(onBus: 0)
            }
            engine.prepare()
        }
    }

    /// inputDeviceUID を engine に反映する（queue 上・エンジン停止中に呼ぶ）。
    /// 前回適用時から変わっていなければ何もしない。
    /// 一度 setDeviceID した AUHAL は、設定をスキップしただけでは前のデバイスに
    /// 固定されたままになるため、「既定に戻す」も既定デバイス ID の明示設定で行う
    private func applyInputDevice() {
        let uid = inputDeviceUID
        let input = engine.inputNode

        if uid.isEmpty {
            // システム既定: 既定デバイスの変更へ追従するため ID を毎回確認する（軽量）
            guard let defaultID = AudioDevices.defaultInputDeviceID() else { return }
            if appliedDeviceUID == "", appliedDefaultID == defaultID { return }
            do {
                try input.auAudioUnit.setDeviceID(defaultID)
                appliedDeviceUID = ""
                appliedDefaultID = defaultID
            } catch {
                appliedDeviceUID = nil
                log.warning("既定デバイスへの切り替えに失敗: \(error.localizedDescription)")
            }
            return
        }

        guard uid != appliedDeviceUID else { return }
        if let deviceID = AudioDevices.deviceID(forUID: uid) {
            do {
                try input.auAudioUnit.setDeviceID(deviceID)
                appliedDeviceUID = uid
            } catch {
                appliedDeviceUID = nil
                log.warning("入力デバイスの切り替えに失敗（既定を使用）: \(error.localizedDescription)")
            }
        } else {
            // 指定デバイスが未接続: 今回は既定で録音し、接続され次第使えるよう再適用待ちにする
            log.warning("指定の入力デバイスが見つかりません（既定を使用）")
            if let defaultID = AudioDevices.defaultInputDeviceID() {
                try? input.auAudioUnit.setDeviceID(defaultID)
                appliedDefaultID = defaultID
            }
            appliedDeviceUID = nil
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

            // 入力デバイス設定の反映（エンジン停止中に行う必要があるが、start は常に
            // stop 後に呼ばれる）。設定が前回から変わっていなければ何もしない
            applyInputDevice()

            let hwFormat = input.inputFormat(forBus: 0)
            guard hwFormat.sampleRate > 0, hwFormat.channelCount > 0 else {
                // デバイス消失（切断後の再接続で ID が変わる）の可能性があるため次回は再解決する
                appliedDeviceUID = nil
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
                // デバイス起因の失敗に備えて次回はデバイスを解決し直す
                appliedDeviceUID = nil
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

            // 停止直後にエンジンのリソースを確保し直しておく（completion 後に行うので
            // 文字起こしの開始は一切遅らせない）。なお実 IO の再起動コスト（数十 ms）は
            // prepare では前払いできず、次回 start() で支払う
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
