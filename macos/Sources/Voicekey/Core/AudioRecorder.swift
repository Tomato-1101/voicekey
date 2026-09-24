//
//  AudioRecorder.swift
//  入力専用 AUHAL によるマイク録音（16kHz モノラル Float32）
//
//  Python 版は PortAudio のハングに苦しめられたため、Mac 版は当初から CoreAudio 系の公式 API を使う。
//  以前は AVAudioEngine を使っていたが、2026-09-25 に入力専用の AUHAL
//  （kAudioUnitSubType_HALOutput・入力 bus1 だけ有効・出力 bus0 は無効）へ置き換えた。理由（実測）:
//  - AVAudioEngine は生成のたびに AUHAL をまず既定「出力」デバイスで作り、既定入力≠既定出力
//    （Bluetooth イヤホン等）だと coreaudiod に `CADefaultDeviceAggregate-<pid>-<n>` という
//    集約デバイスを新規作成してから入力へ付け替える（9 分で 8 個、3.6 時間で 70 個）。
//    作成・破棄のたびに Mac 全体の HAL デバイス一覧が揺れ、coreaudiod が肥大化する。
//  - AVFAudio 内部の IOUnit プロパティリスナーが HAL へ同期問い合わせを撃ち続けて暴走し
//    （2026-09-22 実測 107GB）、`-[AVAudioEngine dealloc]` がそのキューへの dispatch_sync で
//    メインスレッドごと永久にブロックする（2026-09-23 03:00 の hang レポート）。
//    どちらも AVFAudio 内部の話でアプリ側からは止められない。
//  出力側に一切触れない AUHAL を直接使えば集約デバイスは作られず、AVFAudio のリスナーも存在しない。
//  デバイス変化の追従は自前の最小限のリスナーだけで行う。
//
//  スレッド構成:
//  - 制御キュー（com.voicekey.audio-control）: AU・HAL 操作とリスナー通知をすべて直列化する。
//    呼び出し側（ホットキーイベント）は一切ブロックしない。
//  - IO スレッド（HAL のリアルタイムスレッド）: AudioUnitRender → リングバッファへコピーするだけ。
//    メモリ確保はせず、ロックは memcpy の間だけ（処理は IO スレッドの外へ出す）。
//  - 処理キュー（com.voicekey.audio-process）: リングから約 43ms ずつ取り出し、16kHz 変換・蓄積・
//    ストリーミング送信・レベル通知を行う（旧タップと同じ粒度）。
//
//  恒久要件: HAL をループで叩かない。待機中の通知は「次の start で解決し直す」フラグを立てるだけ。
//  録音中の作り直しは上限付き（2 秒に 3 回・1 回の録音で累計 5 回）。
//

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation
import os
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

/// 録音中の作り直しの判断基準（純関数・テスト対象）。
enum InputRestartPolicy {

    /// 自分の IO 開始が引き起こした可能性のある形式通知を、即作り直さずに見送る時間（秒）。
    /// Bluetooth ヘッドセット（AirPods 等）は入力を開始すると A2DP→HFP へ切り替わり、その過程で
    /// サンプルレート・ストリーム構成の通知が飛ぶ（推測・実機未計測）。これを受けて即作り直すと、
    /// 作り直しの IO 開始がまた切替を起こして上限まで作り直し続け、話している途中で録音が確定する。
    static let selfInducedWindow: TimeInterval = 1.0

    /// 録音開始・作り直しの成功後、音声が届いているかを確かめるまでの猶予（秒）。
    /// 構成変更通知の猶予（1 秒）より長いのは、Bluetooth の切替中は最初のバッファが
    /// 届くまで時間がかかることがあり、そこで作り直すと上の自己ループと同じことになるため。
    static let startSilenceGrace: TimeInterval = 2.0

    /// 1 回の録音で許す作り直しの累計。2 秒窓の上限だけだと、1 秒おきに作り直し続ける
    /// ような状態で録音中ずっと HAL を叩き続け得るため、録音単位でも打ち切る。
    static let maxRestartsPerRecording = 5

    /// この形式通知を即作り直さず、音声の到着確認へ回すか。
    /// - Parameters:
    ///   - notifiedAt: 通知を受けた時刻（`systemUptime` 基準）
    ///   - ioStartedAt: 直近に IO を開始した時刻（同上。未開始は 0）
    static func defersFormatChange(notifiedAt: TimeInterval, ioStartedAt: TimeInterval) -> Bool {
        ioStartedAt > 0 && notifiedAt >= ioStartedAt && notifiedAt - ioStartedAt < selfInducedWindow
    }

    /// 作り直しを諦めて録音を確定すべきか。
    /// - Parameters:
    ///   - recentRestarts: 直近 2 秒窓の作り直し回数
    ///   - restartsThisRecording: この録音での作り直し累計
    static func shouldGiveUp(recentRestarts: Int, restartsThisRecording: Int) -> Bool {
        recentRestarts >= 3 || restartsThisRecording >= maxRestartsPerRecording
    }
}

/// AUHAL の入力コールバック（IO スレッド）。refCon は現在の構成の InputCapture。
/// C 関数ポインタとして渡すため何も捕捉しないクロージャにする。
private let inputRenderCallback: AURenderCallback = { refCon, flags, timeStamp, bus, frames, _ in
    Unmanaged<InputCapture>.fromOpaque(refCon).takeUnretainedValue()
        .render(flags: flags, timeStamp: timeStamp, bus: bus, frames: frames)
}

/// 1 つの入力構成（デバイス・フォーマット）ぶんの受け渡し器。
///
/// IO スレッドが AudioUnitRender の結果をリングへ書き、処理キューが約 43ms ずつ取り出して
/// `onBlock` へ渡す。バッファはすべて生成時に確保し、途中で差し替えない（構成が変わったら
/// 新しいインスタンスを作る）。IO スレッドが最後に触った可能性のある旧インスタンスは
/// AudioRecorder が 1 世代ぶん保持してから解放する。
private final class InputCapture {
    let unit: AudioUnit
    let format: AVAudioFormat
    let channels: Int
    /// 処理キューへ 1 回に渡すフレーム数（旧タップの 2048 フレーム@48kHz ≒ 43ms 相当）
    let blockFrames: Int

    /// AudioUnitRender の受け皿（事前確保・チャンネルごとに renderCapacity フレーム）
    private let renderCapacity: Int
    private let renderList: UnsafeMutableAudioBufferListPointer
    private let renderStorage: UnsafeMutablePointer<Float>
    /// IO スレッド → 処理キューのリング（チャンネルごとに ringCapacity フレームを連続配置）
    private let ringCapacity: Int
    private let ring: UnsafeMutablePointer<Float>
    private var ringWrite = 0
    private var ringCount = 0
    /// IO スレッドで捨てたコールバックの件数（lock で保護）。IO スレッドではログできないので
    /// 数えるだけにし、録音の終わりに処理キューから 1 回だけ出す（黙って音が欠ける事故の手掛かり）
    private var renderFailures = 0
    private var oversizeDrops = 0
    private let lock: UnsafeMutablePointer<os_unfair_lock>
    /// 処理キューで使い回す取り出し用バッファ
    private let block: AVAudioPCMBuffer
    /// IO スレッドから処理キューを起こす合図（dispatch_source_merge_data は確保もロックもしない）
    private let signal: DispatchSourceUserDataAdd
    private let onBlock: (AVAudioPCMBuffer) -> Void

    init?(
        unit: AudioUnit, format: AVAudioFormat, renderCapacity: Int,
        processQueue: DispatchQueue, onBlock: @escaping (AVAudioPCMBuffer) -> Void
    ) {
        let channels = Int(format.channelCount)
        let blockFrames = max(64, Int((format.sampleRate * 2048 / 48000).rounded()))
        guard channels > 0,
              let block = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(blockFrames))
        else { return nil }
        self.unit = unit
        self.format = format
        self.channels = channels
        self.blockFrames = blockFrames
        self.renderCapacity = renderCapacity
        self.block = block
        self.onBlock = onBlock

        renderStorage = .allocate(capacity: channels * renderCapacity)
        renderStorage.initialize(repeating: 0, count: channels * renderCapacity)
        renderList = AudioBufferList.allocate(maximumBuffers: channels)
        for c in 0..<channels {
            renderList[c] = AudioBuffer(
                mNumberChannels: 1,
                mDataByteSize: UInt32(renderCapacity * MemoryLayout<Float>.size),
                mData: UnsafeMutableRawPointer(renderStorage + c * renderCapacity)
            )
        }
        // 処理キューが一時的に遅れても落とさないよう 2 秒ぶん持つ
        ringCapacity = max(blockFrames * 4, Int(format.sampleRate * 2))
        ring = .allocate(capacity: channels * ringCapacity)
        ring.initialize(repeating: 0, count: channels * ringCapacity)
        lock = .allocate(capacity: 1)
        lock.initialize(to: os_unfair_lock())

        signal = DispatchSource.makeUserDataAddSource(queue: processQueue)
        signal.setEventHandler { [weak self] in self?.drain(flush: false) }
        signal.activate()
    }

    deinit {
        signal.cancel()
        renderStorage.deallocate()
        free(renderList.unsafeMutablePointer)
        ring.deallocate()
        lock.deinitialize(count: 1)
        lock.deallocate()
    }

    /// IO スレッドから呼ばれる。ここではメモリ確保をしない。
    func render(
        flags: UnsafeMutablePointer<AudioUnitRenderActionFlags>,
        timeStamp: UnsafePointer<AudioTimeStamp>,
        bus: UInt32, frames: UInt32
    ) -> OSStatus {
        let n = Int(frames)
        guard n <= renderCapacity else {
            os_unfair_lock_lock(lock)
            oversizeDrops += 1
            os_unfair_lock_unlock(lock)
            return kAudioUnitErr_TooManyFramesToProcess
        }
        // AudioUnitRender は mDataByteSize を書き換えるので毎回戻す
        for c in 0..<channels {
            renderList[c].mData = UnsafeMutableRawPointer(renderStorage + c * renderCapacity)
            renderList[c].mDataByteSize = UInt32(n * MemoryLayout<Float>.size)
        }
        let status = AudioUnitRender(unit, flags, timeStamp, bus, frames, renderList.unsafeMutablePointer)
        guard status == noErr else {
            os_unfair_lock_lock(lock)
            renderFailures += 1
            os_unfair_lock_unlock(lock)
            return status
        }

        os_unfair_lock_lock(lock)
        // 溢れた分は捨てる（処理キューが 2 秒止まるような異常時だけ起きる）
        let writable = min(n, ringCapacity - ringCount)
        if writable > 0 {
            let first = min(writable, ringCapacity - ringWrite)
            for c in 0..<channels {
                let src = renderStorage + c * renderCapacity
                let dst = ring + c * ringCapacity
                (dst + ringWrite).update(from: src, count: first)
                if writable > first {
                    dst.update(from: src + first, count: writable - first)
                }
            }
            ringWrite = (ringWrite + writable) % ringCapacity
            ringCount += writable
        }
        os_unfair_lock_unlock(lock)
        signal.add(data: 1)
        return noErr
    }

    /// 処理キュー上で呼ぶ。flush=false なら満杯のブロックだけ、true なら端数まで全部渡す。
    func drain(flush: Bool) {
        while true {
            os_unfair_lock_lock(lock)
            let n = ringCount >= blockFrames ? blockFrames : (flush ? ringCount : 0)
            guard n > 0, let dstChannels = block.floatChannelData else {
                os_unfair_lock_unlock(lock)
                return
            }
            let read = (ringWrite - ringCount + ringCapacity) % ringCapacity
            let first = min(n, ringCapacity - read)
            for c in 0..<channels {
                let src = ring + c * ringCapacity
                let dst = dstChannels[c]
                dst.update(from: src + read, count: first)
                if n > first {
                    (dst + first).update(from: src, count: n - first)
                }
            }
            ringCount -= n
            os_unfair_lock_unlock(lock)
            block.frameLength = AVAudioFrameCount(n)
            onBlock(block)
        }
    }

    /// 溜まっている音声を捨てる（IO 開始前に呼ぶ。前回の残りを次の録音へ持ち越さない）
    func reset() {
        os_unfair_lock_lock(lock)
        ringCount = 0
        ringWrite = 0
        os_unfair_lock_unlock(lock)
    }

    /// 捨てたコールバックの件数を取り出してゼロに戻す（どのスレッドからでも可）
    func takeDropCounts() -> (renderFailures: Int, oversizeDrops: Int) {
        os_unfair_lock_lock(lock)
        defer {
            renderFailures = 0
            oversizeDrops = 0
            os_unfair_lock_unlock(lock)
        }
        return (renderFailures, oversizeDrops)
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

        /// IO 開始の失敗を理由に分類する（純関数・テスト対象）。
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

    /// 音声レベル通知（0.0-1.0、約 30fps、処理キューから呼ばれる）
    var levelHandler: ((Float) -> Void)?

    /// 16kHz モノラルチャンクの逐次通知（ストリーミング送信用、処理キューから呼ばれる）。
    /// ストリーミング録音時のみ設定し、終了時に nil へ戻す。
    /// メインスレッドが書き、処理キューが読むため lock で同期する。
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

    /// 録音中にデバイス構成が変わり（マイク切断等）復帰できなかったときの通知。
    /// 呼び出し側はこれを受けて録音を確定する
    var deviceChangedHandler: (() -> Void)?

    /// AU・HAL 操作を直列化するキュー（ブロックしてもここだけ）
    private let queue = DispatchQueue(label: "com.voicekey.audio-control")
    /// 16kHz 変換・蓄積・通知を行うキュー（IO スレッドの外。HAL は叩かない）
    private let processQueue = DispatchQueue(label: "com.voicekey.audio-process", qos: .userInteractive)

    private var samples: [Float] = []
    private let samplesLock = NSLock()
    /// メイン・制御キュー・処理キューをまたぐ可変状態の保護
    private let stateLock = NSLock()
    private var _chunkHandler: (([Float]) -> Void)?
    private var _recordGen = 0       // chunkHandler 登録の採番（main スレッドのみが進める）
    private var _chunkGen = 0        // _chunkHandler が属する録音世代
    private var _activeChunkGen = 0  // 現在の物理録音が受理する世代（start で確定）
    private var _inputDeviceUID = ""
    /// 最後に処理キューがバッファを受け取った時刻（systemUptime 基準・未受信は 0）。
    /// 「IO は動いていると思っているのに音が来ない」を見分けるために使う
    private var _lastBufferUptime: TimeInterval = 0
    /// この録音で IO スレッドが捨てたコールバックの累計（作り直しで受け渡し器が替わっても足し込む）
    private var _renderFailures = 0
    private var _oversizeDrops = 0
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

    // --- 以下は制御キュー上からのみ触る ---

    /// 入力専用 AUHAL（nil = 未生成。失敗したら捨てて次の start で作り直す）
    private var unit: AudioUnit?
    private var unitInitialized = false
    /// AudioOutputUnitStart 済みか（マイクインジケータが点いている状態）
    private var ioRunning = false
    /// 現在の構成の受け渡し器と、1 世代前のもの（IO スレッドが最後に触った可能性があるため
    /// 次の構成変更まで解放しない）
    private var capture: InputCapture?
    private var retiredCapture: InputCapture?
    /// 現在の構成の 16kHz 変換器（処理キューが使う。録音をまたいで内部の残りを持ち越さないよう
    /// IO 開始のたびに処理キュー上でリセットする）
    private var converter: AVAudioConverter?
    /// AU に設定済みのデバイス ID（0 = 未設定）
    private var configuredDeviceID: AudioDeviceID = 0
    /// 適用済みの入力デバイス UID（nil = 未適用または要再解決、空 = システム既定）。
    /// 毎押下の HAL 全列挙と AUHAL 再構成（実測で録音開始遅延の主因だった）を避けるため、
    /// 設定か実デバイスが変わったときだけ適用し直す
    private var appliedDeviceUID: String?
    /// 自前リスナーが変化を受けた（次の start でデバイス・形式を解決し直す）。
    /// 待機中はこのフラグを立てるだけで HAL を叩かない
    private var deviceDirty = false
    /// 実 IO（AudioOutputUnitStart）の初回起動コストを前払い済みか
    private var ioWarmed = false
    /// 構成変更による再起動のループ防止用カウンタと窓の開始時刻
    private var recentRestarts = 0
    private var restartWindowStart: TimeInterval = 0
    /// この録音での作り直しの累計（start でリセット）
    private var restartsThisRecording = 0
    /// 直近に作り直した時刻（それより前の通知に対する確認は、作り直し側の確認に任せて捨てる）
    private var lastRestartAt: TimeInterval = 0
    /// 直近に IO を開始した時刻（自分の IO 開始が起こした形式通知を見分けるため）
    private var ioStartedAt: TimeInterval = 0
    /// 録音ごとの通し番号。前の録音で予約した確認が次の録音に作用しないようにする
    private var recordingEpoch = 0
    /// 録音 buffer の「未取得」状態（#20）。
    /// デバイス切断で recording=false になっても、確定済み音声を一度だけ取り出すために使う
    private let bufferAvailability = BufferAvailability()

    /// 既定入力デバイスの変更リスナー（システムオブジェクトに 1 つだけ張る）
    private var defaultInputListener: AudioObjectPropertyListenerBlock?
    /// 現在のデバイスに張ったリスナー（生死・サンプルレート・ストリーム構成）
    private var deviceWatch: (deviceID: AudioDeviceID, block: AudioObjectPropertyListenerBlock)?

    /// 現在のデバイスで見張るプロパティ。CurrentDevice の設定・Initialize ではどれも変わらないが、
    /// Bluetooth ヘッドセットでは IO 開始そのものが A2DP→HFP 切替を起こし、サンプルレート・
    /// ストリーム構成の通知が自分の操作で飛び得る（推測）。自己ループにしないため、IO 開始直後の
    /// 形式通知は即作り直さず音声の到着確認へ回す（`InputRestartPolicy.defersFormatChange`）。
    /// 離鍵後の HFP→A2DP 戻りで立つ deviceDirty は、次の start で実形式が同じなら構成し直さない
    private static let watchedDeviceProperties: [(AudioObjectPropertySelector, AudioObjectPropertyScope)] = [
        (kAudioDevicePropertyDeviceIsAlive, kAudioObjectPropertyScopeGlobal),
        (kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal),
        (kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeInput),
    ]

    init() {
        // リスナー登録も HAL との通信なので、呼び出し元（メイン）ではなく制御キューで行う
        queue.async { [weak self] in self?.installDefaultInputListener() }
    }

    deinit {
        // 後片付けは制御キューで行い、呼び出し元（多くはメイン）を HAL 呼び出しで塞がない
        // （2026-09-23 の hang は AVAudioEngine の dealloc がメインで永久ブロックしたもの）。
        // 受け渡し器は AU を破棄し終えるまで生かしておく（IO スレッドがまだ参照し得るため）。
        let unit = self.unit
        let captures = (capture, retiredCapture)
        let watch = deviceWatch
        let defaultListener = defaultInputListener
        let queue = self.queue
        queue.async {
            if let unit {
                AudioOutputUnitStop(unit)
                AudioUnitUninitialize(unit)
                AudioComponentInstanceDispose(unit)
            }
            if let watch {
                Self.removeDeviceListeners(deviceID: watch.deviceID, block: watch.block, queue: queue)
            }
            if let defaultListener {
                var address = Self.address(kAudioHardwarePropertyDefaultInputDevice)
                AudioObjectRemovePropertyListenerBlock(
                    AudioObjectID(kAudioObjectSystemObject), &address, queue, defaultListener)
            }
            withExtendedLifetime(captures) {}
        }
    }

    // MARK: - デバイス変化の追従（自前リスナー・制御キュー上）

    private static func address(
        _ selector: AudioObjectPropertySelector,
        _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
    ) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(
            mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }

    private func installDefaultInputListener() {
        guard defaultInputListener == nil else { return }
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            self?.handleDefaultInputChanged()
        }
        var address = Self.address(kAudioHardwarePropertyDefaultInputDevice)
        if AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject), &address, queue, block) == noErr {
            defaultInputListener = block
        }
    }

    /// 現在のデバイスにリスナーを張る（デバイスが変わったときだけ張り替える）
    private func watchDevice(_ deviceID: AudioDeviceID) {
        if deviceWatch?.deviceID == deviceID { return }
        unwatchDevice()
        let block: AudioObjectPropertyListenerBlock = { [weak self] count, addresses in
            let selector = count > 0 ? addresses[0].mSelector : 0
            self?.handleDeviceChanged(deviceID, selector: selector)
        }
        for (selector, scope) in Self.watchedDeviceProperties {
            var address = Self.address(selector, scope)
            AudioObjectAddPropertyListenerBlock(deviceID, &address, queue, block)
        }
        deviceWatch = (deviceID, block)
    }

    private func unwatchDevice() {
        guard let watch = deviceWatch else { return }
        Self.removeDeviceListeners(deviceID: watch.deviceID, block: watch.block, queue: queue)
        deviceWatch = nil
    }

    private static func removeDeviceListeners(
        deviceID: AudioDeviceID, block: @escaping AudioObjectPropertyListenerBlock, queue: DispatchQueue
    ) {
        for (selector, scope) in watchedDeviceProperties {
            var address = address(selector, scope)
            AudioObjectRemovePropertyListenerBlock(deviceID, &address, queue, block)
        }
    }

    /// 既定入力デバイスが変わった（制御キュー上）。
    /// 録音中でも途中で切り替えない（旧実装と同じ）。次の start で解決し直すだけ。
    private func handleDefaultInputChanged() {
        ActionLog.shared.write("audio", "オーディオ構成変更通知を受信 (既定入力)")
        deviceDirty = true
    }

    /// 現在のデバイスの生死・形式が変わった（制御キュー上）。
    /// 待機中はフラグを立てるだけで HAL を叩かない。録音中だけ、本当に使えなくなったかを
    /// 確かめて作り直す（上限付き）。
    private func handleDeviceChanged(_ deviceID: AudioDeviceID, selector: AudioObjectPropertySelector) {
        ActionLog.shared.write("audio", "オーディオ構成変更通知を受信 (\(Self.propertyLabel(selector)))")
        deviceDirty = true
        guard recording, deviceID == configuredDeviceID else { return }
        let notifiedAt = ProcessInfo.processInfo.systemUptime

        // デバイス消失は自分の操作では起きないので、IO 開始直後でも即対処する
        if !AudioDevices.isAlive(deviceID) {
            restartAfterConfigurationChange(reason: "入力デバイスの構成が変わったため再構成します")
            return
        }
        // IO 開始直後の形式通知は自分の開始操作（Bluetooth の切替）が起こした可能性が高いので、
        // 形式の比較もせず（切替中の HAL を叩かない）、落ち着いてから到着確認で判定する
        let settling = InputRestartPolicy.defersFormatChange(notifiedAt: notifiedAt, ioStartedAt: ioStartedAt)
        if !settling, formatDiffers(deviceID) {
            restartAfterConfigurationChange(reason: "入力デバイスの構成が変わったため再構成します")
            return
        }
        // 変化が見えない通知でも、IO が黙り込む実事故（2026-09-15 01:57 / 09-16 20:41、
        // 19.8 秒押して 6.9 秒しか録れていない）に備え、少し待ってからバッファの到着で判定する
        scheduleSilenceCheck(
            after: TapStallPolicy.bufferSilenceGrace, notifiedAt: notifiedAt, checkFormat: settling,
            reason: "構成変更後に音声が届かないため再構成します")
    }

    /// 音声が届いているかの確認を 1 回だけ予約する（予約自体は HAL を叩かない）
    private func scheduleSilenceCheck(
        after grace: TimeInterval, notifiedAt: TimeInterval, checkFormat: Bool, reason: String
    ) {
        let epoch = recordingEpoch
        queue.asyncAfter(deadline: .now() + grace) { [weak self] in
            self?.verifyTapAlive(notifiedAt: notifiedAt, epoch: epoch, checkFormat: checkFormat, reason: reason)
        }
    }

    private static func propertyLabel(_ selector: AudioObjectPropertySelector) -> String {
        switch selector {
        case kAudioDevicePropertyDeviceIsAlive: return "デバイスの生死"
        case kAudioDevicePropertyNominalSampleRate: return "サンプルレート"
        case kAudioDevicePropertyStreamConfiguration: return "ストリーム構成"
        default: return "不明"
        }
    }

    /// デバイスの現在の形式が、AU に設定済みの形式と違うか（読めなければ違う扱い）
    private func formatDiffers(_ deviceID: AudioDeviceID) -> Bool {
        guard let capture else { return true }
        guard let rate = AudioDevices.nominalSampleRate(deviceID) else { return true }
        return rate != capture.format.sampleRate
            || AudioDevices.inputChannelCount(deviceID) != capture.channels
    }

    /// 通知・開始の後で音声が黙っていないか確かめる（制御キュー上・予約した猶予の後）。
    /// 届いていれば何もしない。
    /// - Parameters:
    ///   - notifiedAt: 基準時刻（これ以降に 1 つもバッファが届いていなければ黙死とみなす）
    ///   - epoch: 予約した録音の通し番号（別の録音になっていれば何もしない）
    ///   - checkFormat: 見送った形式通知の確認を兼ねるか（落ち着いた後も形式が違えば構成し直す）
    ///   - reason: 黙死で作り直すときに行動ログへ残す理由
    private func verifyTapAlive(notifiedAt: TimeInterval, epoch: Int, checkFormat: Bool, reason: String) {
        // 再確認までに離鍵で録音が終わっていれば何もしない（誤発火防止）
        guard recording, epoch == recordingEpoch else { return }
        // 通知の後に作り直し済みなら、その作り直しが予約した確認に任せる（同じ通知の束で
        // 何度も作り直さない）
        guard lastRestartAt < notifiedAt else { return }
        if checkFormat, formatDiffers(configuredDeviceID) {
            restartAfterConfigurationChange(reason: "入力デバイスの構成が変わったため再構成します")
            return
        }
        stateLock.lock()
        let lastBuffer = _lastBufferUptime
        stateLock.unlock()
        guard TapStallPolicy.needsRestart(
            lastBufferUptime: lastBuffer, notifiedAt: notifiedAt
        ) else { return }
        restartAfterConfigurationChange(reason: reason, rebuildUnit: true)
    }

    /// 入力を作り直して録音を継続する（制御キュー上）。
    /// 短時間の再起動回数と録音ごとの累計を数えてループを防ぐ。samples は保持するので録音は途切れない。
    /// - Parameters:
    ///   - reason: 行動ログに残す理由（呼び出し経路ごとに違う）
    ///   - rebuildUnit: AU ごと作り直すか。黙死は同じ AU の Stop/Start では直らないことがあるため、
    ///     無音検知の経路では AU を捨てて新しく作る
    private func restartAfterConfigurationChange(reason: String, rebuildUnit: Bool = false) {
        let now = ProcessInfo.processInfo.systemUptime
        if now - restartWindowStart > 2.0 {
            restartWindowStart = now
            recentRestarts = 0
        }
        if InputRestartPolicy.shouldGiveUp(
            recentRestarts: recentRestarts, restartsThisRecording: restartsThisRecording
        ) {
            recording = false
            log.warning("オーディオ構成変更が頻発し録音を継続できません")
            if recentRestarts >= 3 {
                ActionLog.shared.write("audio", "構成変更が頻発したため録音を確定 (再構成 \(recentRestarts) 回)")
            } else {
                ActionLog.shared.write("audio", "録音中の再構成が上限に達したため録音を確定 (累計 \(restartsThisRecording) 回)")
            }
            deviceChangedHandler?()
            return
        }
        recentRestarts += 1
        restartsThisRecording += 1
        lastRestartAt = now
        ActionLog.shared.write("audio", "\(reason) (\(recentRestarts) 回目)")

        if rebuildUnit {
            // 端数を処理してから（stopIO）AU を捨てる。次の prepareInputUnit が新しく作る
            stopIO()
            discardUnit()
        }
        // デバイスも形式も解決し直す（切断なら既定デバイスへ移る）
        deviceDirty = true
        if prepareAndStartIO() == nil {
            log.info("構成変更で停止した入力を再開しました（録音継続）")
            ActionLog.shared.write("audio", "再構成に成功し録音を継続")
            // 作り直した入力が本当に音を運んでいるかを 1 回だけ確かめる（上限の内側で）
            scheduleSilenceCheck(
                after: InputRestartPolicy.startSilenceGrace,
                notifiedAt: ProcessInfo.processInfo.systemUptime, checkFormat: false,
                reason: "再構成後に音声が届かないため再構成します")
            return
        }
        // 本当にデバイスが使えない（切断等）
        recording = false
        log.warning("録音中にオーディオ構成が変化し復帰できませんでした")
        ActionLog.shared.write("audio", "再構成に失敗したため録音を確定")
        deviceChangedHandler?()
    }

    // MARK: - AUHAL の構成（制御キュー上）

    /// 入力専用の AUHAL を作る（入力 bus1 だけ有効・出力 bus0 は無効）。
    /// 出力を無効にするので既定出力デバイスには一切触れず、集約デバイスも作られない。
    private static func makeInputUnit() -> (AudioUnit?, OSStatus) {
        var description = AudioComponentDescription(
            componentType: kAudioUnitType_Output,
            componentSubType: kAudioUnitSubType_HALOutput,
            componentManufacturer: kAudioUnitManufacturer_Apple,
            componentFlags: 0, componentFlagsMask: 0
        )
        guard let component = AudioComponentFindNext(nil, &description) else {
            return (nil, kAudioUnitErr_FailedInitialization)
        }
        var created: AudioUnit?
        var status = AudioComponentInstanceNew(component, &created)
        guard status == noErr, let unit = created else { return (nil, status) }
        var enable: UInt32 = 1
        var disable: UInt32 = 0
        let size = UInt32(MemoryLayout<UInt32>.size)
        status = AudioUnitSetProperty(
            unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &enable, size)
        if status == noErr {
            status = AudioUnitSetProperty(
                unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &disable, size)
        }
        guard status == noErr else {
            AudioComponentInstanceDispose(unit)
            return (nil, status)
        }
        return (unit, noErr)
    }

    /// 受け取り側（bus1 の output scope）の形式: Float32 非インターリーブ・デバイスと同じレート/チャンネル数
    private static func clientFormat(sampleRate: Double, channels: UInt32) -> AVAudioFormat? {
        if channels <= 2 {
            return AVAudioFormat(
                commonFormat: .pcmFormatFloat32, sampleRate: sampleRate,
                channels: channels, interleaved: false)
        }
        // 3ch 以上は channelLayout が必須（無いと AVAudioFormat が作れない）
        guard let layout = AVAudioChannelLayout(
            layoutTag: kAudioChannelLayoutTag_DiscreteInOrder | channels) else { return nil }
        return AVAudioFormat(
            commonFormat: .pcmFormatFloat32, sampleRate: sampleRate,
            interleaved: false, channelLayout: layout)
    }

    private static func osStatusError(_ status: OSStatus) -> NSError {
        NSError(domain: NSOSStatusErrorDomain, code: Int(status))
    }

    /// AU を指定デバイスで構成し直す（IO 停止中に呼ぶ）。成功で nil。
    /// CurrentDevice は Initialize 前に明示設定する（「システム既定」も解決済みの ID で設定する）。
    private func configureUnit(deviceID: AudioDeviceID) -> StartFailure? {
        if unit == nil {
            let (created, status) = Self.makeInputUnit()
            guard let created else {
                log.error("入力ユニットの生成に失敗: \(status, privacy: .public)")
                return StartFailure.classify(Self.osStatusError(status))
            }
            unit = created
        }
        guard let unit else { return .other(0) }
        if unitInitialized {
            AudioUnitUninitialize(unit)
            unitInitialized = false
        }

        var device = deviceID
        var status = AudioUnitSetProperty(
            unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
            &device, UInt32(MemoryLayout<AudioDeviceID>.size))
        guard status == noErr else {
            log.error("入力デバイスの設定に失敗: \(status, privacy: .public)")
            return .deviceMissing
        }

        // デバイス側の形式（bus1 の input scope）
        var deviceFormat = AudioStreamBasicDescription()
        var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        status = AudioUnitGetProperty(
            unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 1, &deviceFormat, &formatSize)
        guard status == noErr, deviceFormat.mSampleRate > 0, deviceFormat.mChannelsPerFrame > 0 else {
            log.error("入力デバイスが見つかりません")
            return .deviceMissing
        }

        // 変換元（受け取り形式）と変換先（16kHz モノラル Float32）
        guard let format = Self.clientFormat(
                sampleRate: deviceFormat.mSampleRate, channels: deviceFormat.mChannelsPerFrame),
              let outFormat = AVAudioFormat(
                commonFormat: .pcmFormatFloat32, sampleRate: Self.sampleRate,
                channels: 1, interleaved: false),
              let converter = AVAudioConverter(from: format, to: outFormat)
        else {
            log.error("音声フォーマット変換の初期化に失敗")
            return .other(0)
        }
        var clientASBD = format.streamDescription.pointee
        status = AudioUnitSetProperty(
            unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1, &clientASBD, formatSize)
        guard status == noErr else {
            log.error("受け取り形式の設定に失敗: \(status, privacy: .public)")
            return StartFailure.classify(Self.osStatusError(status))
        }

        var maxFrames: UInt32 = 0
        var maxFramesSize = UInt32(MemoryLayout<UInt32>.size)
        AudioUnitGetProperty(
            unit, kAudioUnitProperty_MaximumFramesPerSlice, kAudioUnitScope_Global, 0,
            &maxFrames, &maxFramesSize)
        guard let newCapture = InputCapture(
            unit: unit, format: format, renderCapacity: max(Int(maxFrames), 8192),
            processQueue: processQueue,
            onBlock: { [weak self] buffer in
                self?.handleBuffer(buffer, converter: converter, outFormat: outFormat)
            }
        ) else { return .other(0) }

        var callback = AURenderCallbackStruct(
            inputProc: inputRenderCallback,
            inputProcRefCon: Unmanaged.passUnretained(newCapture).toOpaque())
        status = AudioUnitSetProperty(
            unit, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global, 0,
            &callback, UInt32(MemoryLayout<AURenderCallbackStruct>.size))
        guard status == noErr else { return StartFailure.classify(Self.osStatusError(status)) }

        status = AudioUnitInitialize(unit)
        guard status == noErr else {
            log.error("入力ユニットの初期化に失敗: \(status, privacy: .public)")
            return StartFailure.classify(Self.osStatusError(status))
        }
        unitInitialized = true
        retiredCapture = capture
        capture = newCapture
        self.converter = converter
        configuredDeviceID = deviceID
        watchDevice(deviceID)
        return nil
    }

    /// AU を丸ごと捨てる（失敗時）。次の start で作り直す（押下ごとの 1 回だけ＝ループしない）
    private func discardUnit() {
        if let unit {
            if ioRunning { AudioOutputUnitStop(unit) }
            if unitInitialized { AudioUnitUninitialize(unit) }
            AudioComponentInstanceDispose(unit)
        }
        unit = nil
        unitInitialized = false
        ioRunning = false
        retiredCapture = capture
        capture = nil
        configuredDeviceID = 0
        appliedDeviceUID = nil
        unwatchDevice()
    }

    /// inputDeviceUID を解決し、必要なときだけ AU を構成し直す（IO 停止中に呼ぶ）。成功で nil。
    /// 設定もデバイスも変わっていなければ HAL には「既定入力の ID」1 回しか問い合わせない。
    private func prepareInputUnit() -> StartFailure? {
        let uid = inputDeviceUID
        let dirty = deviceDirty
        deviceDirty = false

        let target: AudioDeviceID
        let applied: String?
        if uid.isEmpty {
            // システム既定: 既定デバイスの変更へ追従するため ID を毎回確認する（プロパティ 1 回の取得で軽い）
            guard let defaultID = AudioDevices.defaultInputDeviceID() else {
                log.error("入力デバイスが見つかりません")
                return .deviceMissing
            }
            target = defaultID
            applied = ""
        } else if uid == appliedDeviceUID, !dirty, capture != nil {
            target = configuredDeviceID
            applied = uid
        } else if let deviceID = AudioDevices.deviceID(forUID: uid) {
            target = deviceID
            applied = uid
        } else {
            // 指定デバイスが未接続: 今回は既定で録音し、接続され次第使えるよう再解決待ちにする
            log.warning("指定の入力デバイスが見つかりません（既定を使用）")
            guard let defaultID = AudioDevices.defaultInputDeviceID() else { return .deviceMissing }
            target = defaultID
            applied = nil
        }

        let deviceChanged = capture == nil || target != configuredDeviceID
        // 形式の確認は通知を受けたときだけ（通知が誤発火でも、変わっていなければ構成し直さない
        // ＝自分の操作で通知が出ても再構成ループにならない）
        let formatChanged = !deviceChanged && dirty && formatDiffers(target)
        if deviceChanged || formatChanged {
            if let failure = configureUnit(deviceID: target) {
                discardUnit()
                return failure
            }
            if !deviceChanged {
                ActionLog.shared.write("audio", "入力フォーマットの変更を反映 (id=\(target))")
            } else if applied == "" {
                ActionLog.shared.write("audio", "既定入力デバイスを適用 (id=\(target))")
            } else if let applied {
                ActionLog.shared.write("audio", "入力デバイスを切り替え (uid=\(applied))")
            }
        }
        appliedDeviceUID = applied
        return nil
    }

    /// AU の IO を開始する（構成済みであること）。成功で nil。
    private func startIO() -> StartFailure? {
        guard let unit, let capture else { return .other(0) }
        capture.reset()
        // 変換器の内部に残った前の録音の端数を捨てる。処理キューは直列なので、この後に始まる
        // IO のブロックより必ず先に実行される＝待たずに投げるだけでよい（押下の経路に待ちを足さない）
        if let converter {
            processQueue.async { converter.reset() }
        }
        let status = AudioOutputUnitStart(unit)
        guard status == noErr else {
            // デバイス起因の失敗に備えて次回は作り直す
            discardUnit()
            log.error("録音開始に失敗: code=\(status, privacy: .public)")
            return StartFailure.classify(Self.osStatusError(status))
        }
        ioRunning = true
        ioStartedAt = ProcessInfo.processInfo.systemUptime
        return nil
    }

    /// AU の IO を止め、リングに残った端数まで処理してから返す（末尾の声を落とさない）。
    private func stopIO() {
        guard ioRunning, let unit else { return }
        AudioOutputUnitStop(unit)
        ioRunning = false
        if let capture {
            processQueue.sync { capture.drain(flush: true) }
            absorbDropCounts(capture)
        }
    }

    /// 受け渡し器が捨てた件数をこの録音の累計へ足し込む（作り直しで受け渡し器が替わっても失わない）
    private func absorbDropCounts(_ capture: InputCapture) {
        let counts = capture.takeDropCounts()
        guard counts.renderFailures > 0 || counts.oversizeDrops > 0 else { return }
        stateLock.lock()
        _renderFailures += counts.renderFailures
        _oversizeDrops += counts.oversizeDrops
        stateLock.unlock()
    }

    /// デバイスを解決して IO を開始する（録音開始・モニタ開始・録音中の作り直しで共用）。
    /// samples はクリアしないため、構成変更からの再開でも既存の録音を継続できる。
    @discardableResult
    private func prepareAndStartIO() -> StartFailure? {
        // 構成し直す可能性があるので、動いていれば先に止める（動作中の AU は再構成できない）
        stopIO()
        if let failure = prepareInputUnit() { return failure }
        if let failure = startIO() { return failure }
        let rate = Int(capture?.format.sampleRate ?? 0)
        let channels = capture?.channels ?? 0
        log.info("録音開始 (HW: \(rate)Hz \(channels)ch)")
        // デバイスは名前ではなく UID で残す。名前の取得は HAL への追加問い合わせになり、
        // ここは「HAL が詰まると止まる」まさにその経路なので、記録のために叩かない
        ActionLog.shared.write(
            "audio",
            "タップ設置・エンジン開始 (device=\(appliedDeviceUID?.isEmpty == false ? appliedDeviceUID! : "既定"), "
                + "\(rate)Hz \(channels)ch)"
        )
        return nil
    }

    // MARK: - 公開 API

    /// 次回 start() を高速化するウォームアップ。設定デバイスの解決と AU の構成（Initialize）に加え、
    /// 初回のみ実 IO の起動・停止まで行う（AudioOutputUnitStart の初回コストは実測 1 秒超に
    /// なることがあり、録音時に払うと押し始めの声が欠けるため起動時に前払いする）。
    /// ウォームアップ中の音声は処理キューで捨てるのでどこにも残らない
    /// （アプリ起動直後にマイクインジケータが一瞬点灯するのはこのウォームアップ）。
    /// 待機中の AU は Initialize 済み・停止状態で保持する（停止中はマイクインジケータは点かない）。
    /// - Parameter warmIO: 実 IO の起動・停止まで行うか。false だとマイクインジケータが点かない
    func prewarm(warmIO: Bool = true) {
        queue.async { [self] in
            guard !recording, !monitoring, !ioRunning else { return }
            if let failure = prepareInputUnit() {
                log.info("プリウォームを省略: \(String(describing: failure), privacy: .public)")
                return
            }
            if warmIO, !ioWarmed {
                ioWarmed = true
                if startIO() == nil {
                    stopIO()
                } else {
                    log.info("IO ウォームアップを省略")
                }
            }
        }
    }

    /// 録音を開始する（即座に返る。結果はコールバック。nil=成功、非 nil=失敗理由）
    func start(completion: @escaping (StartFailure?) -> Void) {
        // 行動ログは **queue へ投げる前** に書く。audio-control キューが詰まると
        // 以下の async は一生実行されないので、ここで残さないと「要求したのに完了しない」
        // という形跡そのものが残らない（2026-09-02 のハング障害で実際に追えなかった）
        ActionLog.shared.write("audio", "録音開始要求 (device=\(inputDeviceUID.isEmpty ? "既定" : inputDeviceUID))")
        queue.async { [self] in
            guard !recording else {
                completion(nil)
                return
            }
            samplesLock.lock()
            samples.removeAll(keepingCapacity: true)
            samplesLock.unlock()
            recordingEpoch += 1
            restartsThisRecording = 0
            stateLock.lock(); _renderFailures = 0; _oversizeDrops = 0; stateLock.unlock()

            if let failure = prepareAndStartIO() {
                ActionLog.shared.write("audio", "録音開始失敗 (\(failure))")
                completion(failure)
            } else {
                // この物理録音が受理するストリーミング世代を確定する（recording=true より前）。
                // 旧録音の stop ドレイン中はこの値が進まないため、ドレイン中に差し替えられた
                // 次録音の streamer（より新しい世代）には旧音声が渡らない。
                stateLock.lock(); _activeChunkGen = _chunkGen; stateLock.unlock()
                recording = true
                bufferAvailability.markAvailable()  // この録音の buffer を取り出し可能にする（#20）
                ActionLog.shared.write("audio", "録音開始完了")
                completion(nil)
                // 構成変更の通知が来なくても IO が最初から黙っている場合に備え、1 回だけ到着を確かめる
                // （completion の後に予約するだけなので押下→開始の経路には何も足さない）
                scheduleSilenceCheck(
                    after: InputRestartPolicy.startSilenceGrace,
                    notifiedAt: ProcessInfo.processInfo.systemUptime, checkFormat: false,
                    reason: "録音開始後に音声が届かないため再構成します")
            }
        }
    }

    /// 録音を停止し、確定した音声データを返す（即座に返る。結果はコールバック）
    func stop(completion: @escaping ([Float]) -> Void) {
        // 開始と同じ理由で queue へ投げる前に書く（キューが詰まると停止も完了しない）
        ActionLog.shared.write("audio", "録音停止要求")
        queue.async { [self] in
            // 録音中でなくても、デバイス切断で録音が確定済み（recording=false）の場合は
            // それまでに録音済みの buffer を一度だけ取り出す（#20）。二度目以降は空を返す。
            guard bufferAvailability.consume(recording: recording) else {
                completion([])
                return
            }
            // 録音中のまま IO を止めて端数まで処理する（離鍵直前の声とストリーミング末尾を落とさない）。
            // マイクテストが同時に動いていれば IO は止めず、端数の処理だけ行う（テストのレベル表示を止めない）
            if monitoring, let capture {
                processQueue.sync { capture.drain(flush: true) }
                absorbDropCounts(capture)
            } else {
                stopIO()
            }
            recording = false
            stateLock.lock()
            let renderFailures = _renderFailures
            let oversizeDrops = _oversizeDrops
            stateLock.unlock()
            if renderFailures > 0 || oversizeDrops > 0 {
                // IO スレッドでは書けないので、録音ごとに 1 回だけ処理キューから出す
                processQueue.async {
                    log.warning("入力コールバックを捨てた: render失敗=\(renderFailures) 過大フレーム=\(oversizeDrops)")
                    ActionLog.shared.write(
                        "audio", "入力コールバックを破棄 (render失敗 \(renderFailures) 件, 過大フレーム \(oversizeDrops) 件)")
                }
            }

            samplesLock.lock()
            let result = samples
            samples = []
            samplesLock.unlock()

            log.info("録音停止 (samples=\(result.count), duration=\(String(format: "%.2f", Double(result.count) / Self.sampleRate))s)")
            ActionLog.shared.write(
                "audio",
                "録音停止 (\(String(format: "%.2f", Double(result.count) / Self.sampleRate))s)"
            )
            completion(result)
            // AU は Initialize 済みのまま残す（次の start は AudioOutputUnitStart だけで済む）
        }
    }

    // MARK: - マイクテスト用モニタリング（録音しない）

    /// 録音（サンプル蓄積・文字起こし）を行わず、入力レベルだけを levelHandler へ流すモニタリングを
    /// 開始する。マイクテスト（オンボーディング／ホーム）用。録音中は開始しない。冪等。
    /// - 権限プロンプトは出さない（呼び出し側がマイク許可済みのステップでのみ呼ぶ前提）。
    func startMonitoring(completion: @escaping (Bool) -> Void) {
        queue.async { [self] in
            guard !recording, !monitoring else { completion(true); return }
            if prepareAndStartIO() == nil {
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
            // 録音中なら IO は録音のもの。止めると録音が黙るので、録音の stop に任せる
            if !recording { stopIO() }
            log.info("マイクモニタリングを停止しました")
            completion?()
        }
    }

    /// 制御キュー（`com.voicekey.audio-control`）が動いているか確かめる ping。
    ///
    /// HAL 呼び出しが無期限ブロックするとこの直列キューごと詰まり、以後の録音が全部止まる。
    /// 詰まりを検知したあとに「復帰したか」を知るために使う。ブロックを 1 つ積むだけで
    /// **待たない**（呼び出し側は即座に返る）。詰まっている間 completion は呼ばれず、
    /// キューが動き出した瞬間に呼ばれる＝それが復帰の証拠になる。
    ///
    /// - Parameter completion: キューが動いたときに呼ばれる（制御キューのスレッド上）
    func ping(_ completion: @escaping () -> Void) {
        queue.async { completion() }
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

    // MARK: - 処理キュー

    private func handleBuffer(
        _ buffer: AVAudioPCMBuffer,
        converter: AVAudioConverter,
        outFormat: AVAudioFormat
    ) {
        // 入力が生きている証拠として到着時刻を残す（構成変更後の「黙死」判定に使う）。
        // 無音でも ~43ms ごとに届くので、これが更新されない＝入力が死んでいる
        stateLock.lock()
        _lastBufferUptime = ProcessInfo.processInfo.systemUptime
        stateLock.unlock()

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
