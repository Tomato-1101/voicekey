/// CoreAudio（HAL）のプロパティ読み出しヘルパ
///
/// `AudioObjectGetPropertyData` は「アドレス構造体を組み立てて、サイズを問い合わせて、
/// バッファを渡す」の定型が毎回必要で、呼び出し側が読みづらくなる。
/// subglass が実際に必要とする 4 種類だけを名前付き関数に閉じ込める（汎用化しない）。
import CoreAudio
import Foundation

/// CoreAudio 呼び出しの失敗を表すエラー
@available(macOS 26.0, *)
enum CoreAudioError: Error, CustomStringConvertible {
    /// OSStatus を伴う失敗（どの操作で失敗したかを一緒に持つ）
    case osStatus(operation: String, status: OSStatus)
    /// OSStatus を伴わない論理的な失敗
    case message(String)

    var description: String {
        switch self {
        case let .osStatus(operation, status):
            // OSStatus は 4 文字コードのことが多いので、可読な形に直して出す
            return "\(operation) が失敗しました (OSStatus=\(status) '\(fourCharCode(status))')"
        case let .message(text):
            return text
        }
    }

    /// OSStatus を 4 文字コード文字列に変換する（'!obj' 等。印字不能なら空文字）
    private func fourCharCode(_ status: OSStatus) -> String {
        let value = UInt32(bitPattern: status)
        let bytes = [
            UInt8((value >> 24) & 0xFF),
            UInt8((value >> 16) & 0xFF),
            UInt8((value >> 8) & 0xFF),
            UInt8(value & 0xFF)
        ]
        guard bytes.allSatisfy({ $0 >= 0x20 && $0 < 0x7F }) else { return "" }
        return String(bytes: bytes, encoding: .ascii) ?? ""
    }
}

/// グローバルスコープ・主要素のプロパティアドレスを作る
///
/// - Parameter selector: 読みたいプロパティのセレクタ
/// - Returns: `AudioObjectGetPropertyData` に渡すアドレス構造体
@available(macOS 26.0, *)
func globalPropertyAddress(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
}

/// 既定の「システム出力」デバイス ID を読む
///
/// 通知音などが鳴る側のデバイス。Aggregate Device の main sub-device に据える必要がある
/// （実デバイスを親に置かないとタップがゼロサンプルしか返さない、という HAL の仕様）。
///
/// - Returns: 既定システム出力デバイスの AudioObjectID
/// - Throws: 取得に失敗した場合 `CoreAudioError.osStatus`
@available(macOS 26.0, *)
func readDefaultSystemOutputDeviceID() throws -> AudioObjectID {
    var address = globalPropertyAddress(kAudioHardwarePropertyDefaultSystemOutputDevice)
    var deviceID = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &deviceID
    )
    guard status == noErr else {
        throw CoreAudioError.osStatus(operation: "既定システム出力デバイスの取得", status: status)
    }
    guard deviceID != AudioObjectID(kAudioObjectUnknown) else {
        throw CoreAudioError.message("既定システム出力デバイスが存在しません")
    }
    return deviceID
}

/// デバイスの UID 文字列を読む
///
/// Aggregate Device の記述辞書はデバイスを ID ではなく UID 文字列で指す。
///
/// - Parameter deviceID: 対象デバイスの AudioObjectID
/// - Returns: デバイス UID
/// - Throws: 取得に失敗した場合 `CoreAudioError.osStatus`
@available(macOS 26.0, *)
func readDeviceUID(_ deviceID: AudioObjectID) throws -> String {
    var address = globalPropertyAddress(kAudioDevicePropertyDeviceUID)
    var uid: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    let status = withUnsafeMutablePointer(to: &uid) { pointer in
        AudioObjectGetPropertyData(deviceID, &address, 0, nil, &size, pointer)
    }
    guard status == noErr else {
        throw CoreAudioError.osStatus(operation: "デバイス UID の取得", status: status)
    }
    return uid as String
}

/// デバイスの公称サンプルレートを読む
///
/// Aggregate Device の IO サイクルは main sub-device のクロックで回るため、
/// タップ側 ASBD が主張するレートと実際に配られるフレーム数が食い違うことがある
/// （実測: タップ ASBD=48kHz / 内蔵スピーカー=44.1kHz のとき、届くのは 44.1kHz 相当）。
///
/// - Parameter deviceID: 対象デバイスの AudioObjectID
/// - Returns: 公称サンプルレート（Hz）
/// - Throws: 取得に失敗した場合 `CoreAudioError.osStatus`
@available(macOS 26.0, *)
func readNominalSampleRate(_ deviceID: AudioObjectID) throws -> Double {
    var address = globalPropertyAddress(kAudioDevicePropertyNominalSampleRate)
    var sampleRate = 0.0
    var size = UInt32(MemoryLayout<Double>.size)
    let status = AudioObjectGetPropertyData(deviceID, &address, 0, nil, &size, &sampleRate)
    guard status == noErr else {
        throw CoreAudioError.osStatus(operation: "公称サンプルレートの取得", status: status)
    }
    return sampleRate
}

/// タップが出力する PCM フォーマット（ASBD）を読む
///
/// タップ生成後にしか確定しないため、Aggregate Device を作る前にここで取得して
/// AVAudioFormat に変換する。
///
/// - Parameter tapID: `AudioHardwareCreateProcessTap` が返したタップの AudioObjectID
/// - Returns: タップ出力のストリーム記述
/// - Throws: 取得に失敗した場合 `CoreAudioError.osStatus`
@available(macOS 26.0, *)
func readTapStreamDescription(_ tapID: AudioObjectID) throws -> AudioStreamBasicDescription {
    var address = globalPropertyAddress(kAudioTapPropertyFormat)
    var description = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    let status = AudioObjectGetPropertyData(tapID, &address, 0, nil, &size, &description)
    guard status == noErr else {
        throw CoreAudioError.osStatus(operation: "タップ出力フォーマットの取得", status: status)
    }
    return description
}

/// PID から HAL の「プロセスオブジェクト」ID を引く
///
/// タップから自プロセスを除外するのに使う。ただし **一度も音を出していないプロセスには
/// オブジェクトが存在しない**（`kAudioObjectUnknown` が返る）。そのため subglass では
/// これ単独に頼らず、`CATapDescription.bundleIDs`（macOS 26+）による除外も併用する。
///
/// - Parameter pid: 対象プロセス ID
/// - Returns: 見つかれば AudioObjectID、見つからなければ nil
@available(macOS 26.0, *)
func translatePIDToAudioProcessObject(_ pid: pid_t) -> AudioObjectID? {
    var address = globalPropertyAddress(kAudioHardwarePropertyTranslatePIDToProcessObject)
    var targetPID = pid
    var objectID = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject),
        &address,
        UInt32(MemoryLayout<pid_t>.size),
        &targetPID,
        &size,
        &objectID
    )
    guard status == noErr, objectID != AudioObjectID(kAudioObjectUnknown) else { return nil }
    return objectID
}

/// HAL が把握している音声プロセス 1 件
@available(macOS 26.0, *)
struct AudioOutputProcess: Sendable, Hashable, CustomStringConvertible {
    /// HAL のプロセスオブジェクト ID（タップの対象指定に使う）
    let objectID: AudioObjectID
    /// プロセス ID
    let pid: pid_t
    /// バンドル ID（取れない場合は空文字）
    let bundleID: String
    /// いま出力を鳴らしているか
    let isRunningOutput: Bool

    var description: String { bundleID.isEmpty ? "pid=\(pid)" : "pid=\(pid)(\(bundleID))" }
}

/// 別プロセスの「責任プロセス」PID を引く
///
/// Chrome の音は Google Chrome Helper、Safari の音は com.apple.WebKit.GPU が出しており、
/// 最前面アプリの PID とは一致しない。責任 PID（responsible pid）を辿ると、これらの
/// ヘルパは親アプリ本体を指すため、「最前面アプリが出している音」を特定できる。
///
/// `responsibility_get_pid_responsible_for_pid` は公開ヘッダの無い SPI なので、
/// リンクエラーにならないよう dlsym で解決する（自分専用アプリなので使用可）。
@available(macOS 26.0, *)
private let responsibleForPIDFunction: (@convention(c) (pid_t) -> pid_t)? = {
    guard let handle = dlopen(nil, RTLD_NOW),
          let symbol = dlsym(handle, "responsibility_get_pid_responsible_for_pid") else { return nil }
    return unsafeBitCast(symbol, to: (@convention(c) (pid_t) -> pid_t).self)
}()

/// 指定 PID の責任プロセス PID を返す（引けない場合は自分自身の PID を返す）
///
/// - Parameter pid: 対象プロセス ID
/// - Returns: 責任プロセスの PID
@available(macOS 26.0, *)
func responsiblePID(for pid: pid_t) -> pid_t {
    guard let function = responsibleForPIDFunction else { return pid }
    let responsible = function(pid)
    return responsible > 0 ? responsible : pid
}

/// HAL が把握している音声プロセスをすべて列挙する
///
/// タップの対象を「最前面アプリだけ」に絞るために、プロセスオブジェクト ID まで含めて返す。
///
/// - Returns: 音声プロセス一覧（取得に失敗した場合は空配列）
@available(macOS 26.0, *)
func readAudioProcesses() -> [AudioOutputProcess] {
    var listAddress = globalPropertyAddress(kAudioHardwarePropertyProcessObjectList)
    var listSize: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &listAddress, 0, nil, &listSize
    ) == noErr, listSize > 0 else { return [] }

    let count = Int(listSize) / MemoryLayout<AudioObjectID>.size
    var objectIDs = [AudioObjectID](repeating: AudioObjectID(kAudioObjectUnknown), count: count)
    guard AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &listAddress, 0, nil, &listSize, &objectIDs
    ) == noErr else { return [] }

    return objectIDs.compactMap { objectID -> AudioOutputProcess? in
        var runningAddress = globalPropertyAddress(kAudioProcessPropertyIsRunningOutput)
        var isRunning: UInt32 = 0
        var runningSize = UInt32(MemoryLayout<UInt32>.size)
        let runningStatus = AudioObjectGetPropertyData(
            objectID, &runningAddress, 0, nil, &runningSize, &isRunning
        )

        var pidAddress = globalPropertyAddress(kAudioProcessPropertyPID)
        var pid: pid_t = -1
        var pidSize = UInt32(MemoryLayout<pid_t>.size)
        guard AudioObjectGetPropertyData(
            objectID, &pidAddress, 0, nil, &pidSize, &pid
        ) == noErr else { return nil }

        var bundleAddress = globalPropertyAddress(kAudioProcessPropertyBundleID)
        var bundleID: CFString = "" as CFString
        var bundleSize = UInt32(MemoryLayout<CFString>.size)
        let bundleStatus = withUnsafeMutablePointer(to: &bundleID) { pointer in
            AudioObjectGetPropertyData(objectID, &bundleAddress, 0, nil, &bundleSize, pointer)
        }
        return AudioOutputProcess(
            objectID: objectID,
            pid: pid,
            bundleID: bundleStatus == noErr ? (bundleID as String) : "",
            isRunningOutput: runningStatus == noErr && isRunning != 0
        )
    }
}

/// いま出力を鳴らしているプロセスを列挙する
///
/// TTS 再キャプチャ検証の要。RMS だけでは「他アプリの再生音が混ざった環境」で
/// 自分の読み上げを拾ったのかどうか判別できないが、
/// 「読み上げ中に出力していたのはどのプロセスか」は環境音に影響されず確定する。
/// ここに自プロセスしか出てこなければ、読み上げは自プロセス内でレンダリングされており
/// タップの自プロセス除外がそのまま効く（＝再キャプチャループは起きない）。
///
/// - Returns: 出力中のプロセス一覧（取得に失敗した場合は空配列）
@available(macOS 26.0, *)
func readProcessesRunningOutput() -> [AudioOutputProcess] {
    readAudioProcesses().filter(\.isRunningOutput)
}
