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

/// 録音 buffer の「未取得」状態を管理する小さな状態機械（#20）。
///
/// 録音開始で利用可能になり、stop の取り出しで「一度だけ」消費される。
/// デバイス切断（構成変更からの復帰失敗）で録音が確定し recording=false になっても、
/// 利用可能フラグは残すため、それまでに録音済みの音声を取りこぼさず文字起こしへ回せる。
/// AudioRecorder.queue 上からのみ操作するため内部ロックは持たない。
final class BufferAvailability {
    /// 取り出していない録音 buffer があるか
    private(set) var available = false

    /// 録音開始時に呼ぶ（buffer の取り出しを許可する）
    func markAvailable() {
        available = true
    }

    /// stop 時に buffer を取り出してよいか判定する。
    /// 録音中、または録音確定済み（recording=false）でも未取得 buffer が残っていれば true を
    /// 返し、内部状態を消費済みにする（同じ buffer を二重取得しない）。
    ///
    /// - Parameter recording: 現在録音中か
    /// - Returns: 取り出してよければ true（呼び出し側が drain する）
    func consume(recording: Bool) -> Bool {
        guard recording || available else { return false }
        available = false
        return true
    }
}

final class AudioRecorder {

    /// 録音開始に失敗した理由（HUD にそのまま出せる短文を持つ）
    enum StartFailure: Equatable {
        case deviceMissing        // 入力デバイス消失
        case outOfMemory          // coreaudiod がメモリを確保できず IO を開始できない
        case other(Int)           // その他（NSError.code の生値）

        /// HUD ピルに表示するユーザー向け文言
        var noticeText: String {
            switch self {
            case .deviceMissing: return "録音を開始できませんでした（マイクが見つかりません）"
            case .outOfMemory:   return "メモリ不足でマイクを開始できませんでした"
            case .other(let c):  return "録音を開始できませんでした（マイクを確認: \(c)）"
            }
        }

        /// engine.start() の失敗を理由に分類する（純関数・テスト対象）。
        /// 2003329396 = 'what' (kAudioHardwareUnspecifiedError)。実測では coreaudiod が
        /// メモリ枯渇で IO 用バッファを mlock できないときにこのコードで拒否される。
        static func classify(_ error: Error) -> StartFailure {
            let code = (error as NSError).code
            return code == 2003329396 ? .outOfMemory : .other(code)
        }
    }

    /// 出力サンプリングレート（Whisper 系 API の標準）
    static let sampleRate: Double = 16000

    /// 録音の最大サンプル数（300 秒）。永久録音によるメモリ膨張の保険
    private static let maxSamples = Int(sampleRate) * 300

    /// 音声レベル通知（0.0-1.0、約 30fps、audio スレッドから呼ばれる）
    var levelHandler: ((Float) -> Void)?

    /// 16kHz モノラルチャンクの逐次通知（ストリーミング送信用、audio スレッドから呼ばれる）。
    /// ストリーミング録音時のみ設定し、終了時に nil へ戻す。
    /// メインスレッドが書き、audio スレッドが読むため lock で同期する。
    ///
    /// 連続録音間の音声混入防止: 登録のたびに録音世代を進めて束縛し、handleBuffer は
    /// 「現在の物理録音が受理する世代（activeChunkGen、start で確定）」と一致するチャンク
    /// だけを送る。旧録音の stop ドレイン中に次録音が別 streamer を差し替えても、受理世代は
    /// start でしか進まないため、旧録音末尾のチャンクが次録音の streamer へ流れ込まない。
    var chunkHandler: (([Float]) -> Void)? {
        get { stateLock.lock(); defer { stateLock.unlock() }; return _chunkHandler }
        set {
            stateLock.lock()
            _chunkHandler = newValue
            if newValue != nil {
                _recordGen += 1
                _chunkGen = _recordGen
            }
            stateLock.unlock()
        }
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
    private var _recordGen = 0       // chunkHandler 登録の採番（main スレッドのみが進める）
    private var _chunkGen = 0        // _chunkHandler が属する録音世代
    private var _activeChunkGen = 0  // 現在の物理録音が受理する世代（start で確定）
    private var _inputDeviceUID = ""
    private var _recording = false
    private var recording: Bool {
        get { stateLock.lock(); defer { stateLock.unlock() }; return _recording }
        set { stateLock.lock(); _recording = newValue; stateLock.unlock() }
    }
    /// マイクテスト用モニタリング中か（録音はせずレベルだけを levelHandler へ流す）。
    /// 録音（サンプル蓄積・文字起こし）とは排他で、無料枠を一切消費しない。
    private var _monitoring = false
    private var monitoring: Bool {
        get { stateLock.lock(); defer { stateLock.unlock() }; return _monitoring }
        set { stateLock.lock(); _monitoring = newValue; stateLock.unlock() }
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
    /// 構成変更による再起動のループ防止用カウンタと窓の開始時刻（queue 上からのみ触る）
    private var recentRestarts = 0
    private var restartWindowStart: TimeInterval = 0
    /// 録音 buffer の「未取得」状態（#20）。queue 上からのみ触る。
    /// デバイス切断で recording=false になっても、確定済み音声を一度だけ取り出すために使う
    private let bufferAvailability = BufferAvailability()

    init() {
        // 録音中のマイク切断・サンプルレート変更等ではエンジンが静かに止まり、
        // ユーザーが喋り続けても音声が入らない。通知で検知して継続/再開する。
        // 注意: この通知は engine.start() 直後やフォーマット確定時にも頻繁に「誤発火」する
        // （デバイスは何も変わっていない）。そのまま録音を止めると「開始した瞬間に
        // 『マイク構成が変わった』で止まる」誤動作になるため、ハンドラ側で実際に
        // 復帰が必要かを判断する（handleConfigurationChange）。
        NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in
            // 通知は任意スレッドで届く。エンジン操作を直列化する queue 上で処理する
            self?.queue.async { self?.handleConfigurationChange() }
        }
    }

    /// AVAudioEngineConfigurationChange を処理する（queue 上）。
    /// この通知は誤発火が多いため、録音中でもデバイスが生きていれば中断しない。
    /// エンジンが本当に停止していたら同じデバイスで作り直して録音を継続し、
    /// 復帰不能なときだけ呼び出し側へ確定通知する。
    private func handleConfigurationChange() {
        // 構成が変わった可能性 → 次回 start() ではデバイスを解決し直す
        appliedDeviceUID = nil
        guard recording else { return }

        // エンジンがまだ動いている＝音声は途切れていない（最も多い誤発火パターン）。
        // ここで止めると「デバイス未変更なのに録音が止まる」になるため何もしない。
        if engine.isRunning { return }

        // エンジンが停止＝音声が途切れた。短時間の再起動回数を数えてループを防ぎつつ、
        // 現在のフォーマットでタップ・変換器を作り直して録音を継続する（samples は保持）。
        let now = ProcessInfo.processInfo.systemUptime
        if now - restartWindowStart > 2.0 {
            restartWindowStart = now
            recentRestarts = 0
        }
        if recentRestarts >= 3 {
            recording = false
            log.warning("オーディオ構成変更が頻発し録音を継続できません")
            deviceChangedHandler?()
            return
        }
        recentRestarts += 1

        if installTapAndStart() == nil {
            log.info("構成変更で停止したエンジンを再開しました（録音継続）")
            return
        }
        // 本当にデバイスが使えない（切断等）
        recording = false
        log.warning("録音中にオーディオ構成が変化し復帰できませんでした")
        deviceChangedHandler?()
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

    /// 録音を開始する（即座に返る。結果はコールバック。nil=成功、非 nil=失敗理由）
    func start(completion: @escaping (StartFailure?) -> Void) {
        queue.async { [self] in
            guard !recording else {
                completion(nil)
                return
            }
            samplesLock.lock()
            samples.removeAll(keepingCapacity: true)
            samplesLock.unlock()

            // 入力デバイス設定の反映（エンジン停止中に行う必要があるが、start は常に
            // stop 後に呼ばれる）。設定が前回から変わっていなければ何もしない
            applyInputDevice()

            if let failure = installTapAndStart() {
                completion(failure)
            } else {
                // この物理録音が受理するストリーミング世代を確定する（recording=true より前）。
                // 旧録音の stop ドレイン中はこの値が進まないため、ドレイン中に差し替えられた
                // 次録音の streamer（より新しい世代）には旧音声が渡らない。
                stateLock.lock(); _activeChunkGen = _chunkGen; stateLock.unlock()
                recording = true
                bufferAvailability.markAvailable()  // この録音の buffer を取り出し可能にする（#20）
                completion(nil)
            }
        }
    }

    /// 現在の入力フォーマットでタップと 16kHz 変換器を作り直し、エンジンを開始する。
    /// queue 上・エンジン停止中に呼ぶ。成功で nil（録音開始/継続が可能）、失敗なら理由を返す。
    /// samples はクリアしないため、構成変更からの再開でも既存の録音を継続できる。
    @discardableResult
    private func installTapAndStart() -> StartFailure? {
        let input = engine.inputNode
        let hwFormat = input.inputFormat(forBus: 0)
        guard hwFormat.sampleRate > 0, hwFormat.channelCount > 0 else {
            // デバイス消失（切断後の再接続で ID が変わる）の可能性があるため次回は再解決する
            appliedDeviceUID = nil
            log.error("入力デバイスが見つかりません")
            return .deviceMissing
        }

        // 変換先: 16kHz モノラル Float32
        guard let outFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: Self.sampleRate,
            channels: 1,
            interleaved: false
        ), let converter = AVAudioConverter(from: hwFormat, to: outFormat) else {
            log.error("音声フォーマット変換の初期化に失敗")
            return .other(0)
        }

        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 2048, format: hwFormat) { [weak self] buffer, _ in
            self?.handleBuffer(buffer, converter: converter, outFormat: outFormat)
        }

        do {
            engine.prepare()
            try engine.start()
            log.info("録音開始 (HW: \(Int(hwFormat.sampleRate))Hz \(hwFormat.channelCount)ch)")
            return nil
        } catch {
            // デバイス起因の失敗に備えて次回はデバイスを解決し直す
            appliedDeviceUID = nil
            input.removeTap(onBus: 0)
            log.error("録音開始に失敗: code=\((error as NSError).code, privacy: .public) \(error.localizedDescription)")
            return StartFailure.classify(error)
        }
    }

    /// 録音を停止し、確定した音声データを返す（即座に返る。結果はコールバック）
    func stop(completion: @escaping ([Float]) -> Void) {
        queue.async { [self] in
            // 録音中でなくても、デバイス切断で録音が確定済み（recording=false）の場合は
            // それまでに録音済みの buffer を一度だけ取り出す（#20）。二度目以降は空を返す。
            guard bufferAvailability.consume(recording: recording) else {
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

    // MARK: - マイクテスト用モニタリング（録音しない）

    /// 録音（サンプル蓄積・文字起こし）を行わず、入力レベルだけを levelHandler へ流すモニタリングを
    /// 開始する。マイクテスト（オンボーディング／ホーム）用。録音中は開始しない。冪等。
    /// - 権限プロンプトは出さない（呼び出し側がマイク許可済みのステップでのみ呼ぶ前提）。
    func startMonitoring(completion: @escaping (Bool) -> Void) {
        queue.async { [self] in
            guard !recording, !monitoring else { completion(true); return }
            applyInputDevice()
            if installTapAndStart() == nil {
                monitoring = true
                log.info("マイクモニタリングを開始しました（録音なし・レベルのみ）")
                completion(true)
            } else {
                completion(false)
            }
        }
    }

    /// マイクモニタリングを停止する（録音には影響しない。モニタ中でなければ何もしない）。
    func stopMonitoring(completion: (() -> Void)? = nil) {
        queue.async { [self] in
            guard monitoring else { completion?(); return }
            monitoring = false
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
            engine.prepare()
            log.info("マイクモニタリングを停止しました")
            completion?()
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
        // 録音中はサンプル蓄積＋チャンク送信＋レベル、モニタ中はレベルのみ。どちらでもないなら無視。
        let isRecording = recording
        let isMonitoring = monitoring
        guard isRecording || isMonitoring else { return }

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

        // 録音時のみ: サンプル蓄積とストリーミングチャンク送信を行う（モニタ時はレベルだけ）。
        if isRecording {
            samplesLock.lock()
            if samples.count < Self.maxSamples {
                samples.append(contentsOf: chunk)
            }
            samplesLock.unlock()

            // ストリーミング送信用に逐次チャンクを渡す（全バッファ蓄積とは独立）。
            // ストリーミングが失敗しても samples には残るため REST フォールバックが効く。
            // 世代一致のチャンクのみ送る（旧録音 stop ドレイン中に差し替えられた新 streamer へ
            // 混入させない）。handler/gen は原子的にまとめて読む
            stateLock.lock()
            let handler = _chunkHandler
            let handlerGen = _chunkGen
            let activeGen = _activeChunkGen
            stateLock.unlock()
            if let handler, handlerGen == activeGen {
                handler(chunk)
            }
        }

        // レベル通知（録音・モニタ両方で流す。約 30fps に間引き）
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
