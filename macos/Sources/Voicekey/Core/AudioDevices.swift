//
//  AudioDevices.swift
//  Core Audio による入力デバイスの列挙と UID 解決
//
//  AVAudioEngine の inputNode は既定でシステム標準デバイスを使うため、
//  ユーザーが任意のマイクを選べるよう Core Audio (HAL) でデバイスを列挙し、
//  UID から AudioDeviceID を解決して AudioRecorder で切り替える。
//  AudioDeviceID は再接続のたびに変わり得るので、永続化には安定した UID を使う。
//

import CoreAudio
import Foundation

/// 入力（マイク）として使えるデバイス 1 つ分の情報
struct AudioInputDevice: Identifiable, Hashable {
    /// Core Audio のデバイス ID（再接続で変わり得るため永続化には使わない）
    let id: AudioDeviceID
    /// 安定した一意識別子（設定保存・永続化に使う）
    let uid: String
    /// 表示名
    let name: String
}

/// Core Audio による入力デバイス操作のユーティリティ
enum AudioDevices {

    /// 入力チャンネルを持つデバイス一覧を返す（マイクとして選択可能なもの）
    static func inputDevices() -> [AudioInputDevice] {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var dataSize: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &dataSize
        ) == noErr, dataSize > 0 else {
            return []
        }

        let count = Int(dataSize) / MemoryLayout<AudioDeviceID>.size
        var ids = [AudioDeviceID](repeating: 0, count: count)
        guard AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &dataSize, &ids
        ) == noErr else {
            return []
        }

        var result: [AudioInputDevice] = []
        for id in ids {
            // 入力チャンネルが無いデバイス（出力専用）は除外する
            guard inputChannelCount(id) > 0 else { continue }
            guard let uid = stringProperty(id, kAudioDevicePropertyDeviceUID),
                  let name = stringProperty(id, kAudioObjectPropertyName) else { continue }
            result.append(AudioInputDevice(id: id, uid: uid, name: name))
        }
        return result
    }

    /// UID から現在の AudioDeviceID を解決する（見つからなければ nil）
    static func deviceID(forUID uid: String) -> AudioDeviceID? {
        inputDevices().first { $0.uid == uid }?.id
    }

    /// システム既定の入力デバイスの ID を返す（取得できなければ nil）。
    /// 全デバイス列挙と違いプロパティ 1 回の取得なので毎回呼んでも軽い
    static func defaultInputDeviceID() -> AudioDeviceID? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var deviceID = AudioDeviceID(kAudioObjectUnknown)
        var dataSize = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &dataSize, &deviceID
        ) == noErr, deviceID != kAudioObjectUnknown else {
            return nil
        }
        return deviceID
    }

    // MARK: - 内部ヘルパー

    /// 指定デバイスの入力チャンネル総数（0 なら出力専用デバイス）
    private static func inputChannelCount(_ id: AudioDeviceID) -> Int {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var dataSize: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &address, 0, nil, &dataSize) == noErr,
              dataSize > 0 else {
            return 0
        }
        // AudioBufferList は可変長配列を含むため生メモリで確保する
        let bufferList = UnsafeMutableRawPointer.allocate(
            byteCount: Int(dataSize),
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { bufferList.deallocate() }
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &dataSize, bufferList) == noErr else {
            return 0
        }
        let abl = UnsafeMutableAudioBufferListPointer(
            bufferList.assumingMemoryBound(to: AudioBufferList.self)
        )
        var channels = 0
        for buffer in abl {
            channels += Int(buffer.mNumberChannels)
        }
        return channels
    }

    /// CFString 型のプロパティを取得して Swift String で返す
    private static func stringProperty(
        _ id: AudioDeviceID, _ selector: AudioObjectPropertySelector
    ) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var value: CFString?
        var dataSize = UInt32(MemoryLayout<CFString?>.size)
        let status = withUnsafeMutablePointer(to: &value) {
            AudioObjectGetPropertyData(id, &address, 0, nil, &dataSize, $0)
        }
        guard status == noErr, let value else { return nil }
        return value as String
    }
}
