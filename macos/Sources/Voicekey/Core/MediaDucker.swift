//
//  MediaDucker.swift
//  録音中のメディア音量ダッキング（既定出力デバイスの音量を一時的に下げる）
//
//  録音開始時に既定出力デバイスの音量を保存して 12% へ下げ、停止時に元へ戻す。
//  現在音量が既にターゲット(12%)以下のときは何もしない（音量を「引き上げて」しまう逆転を防ぐ）。
//  音量制御を持たないデバイス（一部の USB DAC・仮想デバイス等）では何もしない。
//
//  クラッシュ耐性: 下げる前に「元音量・ダッキング中フラグ」を UserDefaults へ保存し、
//  復元後にクリアする。アプリが録音中に落ちても、次回起動時に restore() が残存フラグを
//  見つけて元へ戻す（音量が下がったまま戻らない事故を防ぐ）。
//
//  CoreAudio 呼び出しはブロッキングになりうるため、録音のクリティカルパスに乗せず
//  専用シリアルキューで撃ちっぱなしに行う。
//

import AudioToolbox
import CoreAudio
import Foundation
import os.log

private let duckLog = Logger(subsystem: "com.voicekey.app", category: "ducker")

/// メディア音量ダッキング。`MediaDucker.duck()` / `MediaDucker.restore()` を呼ぶだけ。
enum MediaDucker {

    /// ダッキング時に下げる音量（元音量に対する係数ではなく絶対値 0...1）
    private static let duckedVolume: Float = 0.12

    /// クラッシュ耐性のための永続フラグ・元音量の UserDefaults キー
    private static let activeKey = "mediaDuckActive"
    private static let savedVolumeKey = "mediaDuckSavedVolume"

    /// CoreAudio 呼び出しを逃がす専用キュー（録音の開始/停止を待たせない）
    private static let queue = DispatchQueue(label: "com.voicekey.ducker", qos: .userInitiated)

    /// 既定出力デバイスの音量を保存して下げる（撃ちっぱなし）。
    /// 既にダッキング中なら二重に下げない（元音量を上書きしないため）。
    /// 現在音量が既にターゲット以下なら何もしない（音量を引き上げてしまう逆転を防ぐ）。
    static func duck() {
        queue.async {
            // 二重ダッキング防止（元音量を duckedVolume で上書きしてしまうのを防ぐ）
            guard !UserDefaults.standard.bool(forKey: activeKey) else { return }
            guard let device = defaultOutputDevice(),
                  let current = volume(of: device) else { return }
            // 現在音量がターゲットより大きいときだけ下げる。ターゲット以下（既に十分小さい・
            // 消音中など）なら下げず、フラグも立てない＝停止時に音量を「上げて」しまわない。
            guard current > duckedVolume else { return }
            // 復元用に元音量とフラグを先に永続化してから下げる（この間に落ちても復元できる）
            UserDefaults.standard.set(Double(current), forKey: savedVolumeKey)
            UserDefaults.standard.set(true, forKey: activeKey)
            setVolume(duckedVolume, of: device)
            duckLog.debug("メディア音量をダッキング: \(current) → \(duckedVolume)")
        }
    }

    /// ダッキング中なら元音量へ戻す（撃ちっぱなし）。ダッキングしていなければ何もしない。
    /// 起動時の残存フラグ復元にもこのメソッドを使う（前回異常終了の巻き戻し）。
    static func restore() {
        queue.async {
            guard UserDefaults.standard.bool(forKey: activeKey) else { return }
            let saved = Float(UserDefaults.standard.double(forKey: savedVolumeKey))
            if let device = defaultOutputDevice() {
                setVolume(saved, of: device)
                duckLog.debug("メディア音量を復元: \(saved)")
            }
            UserDefaults.standard.removeObject(forKey: activeKey)
            UserDefaults.standard.removeObject(forKey: savedVolumeKey)
        }
    }

    // MARK: - CoreAudio ヘルパ

    /// 既定出力デバイスの ID を取得する（取得できなければ nil）
    private static func defaultOutputDevice() -> AudioDeviceID? {
        var deviceID = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &deviceID
        )
        guard status == noErr, deviceID != 0 else { return nil }
        return deviceID
    }

    /// 仮想メインボリューム（デバイス全体の音量）のプロパティアドレス
    private static func volumeAddress() -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(
            mSelector: kAudioHardwareServiceDeviceProperty_VirtualMainVolume,
            mScope: kAudioObjectPropertyScopeOutput,
            mElement: kAudioObjectPropertyElementMain
        )
    }

    /// デバイスの現在音量（0...1）。音量制御を持たないデバイスは nil
    private static func volume(of device: AudioDeviceID) -> Float? {
        var address = volumeAddress()
        guard AudioObjectHasProperty(device, &address) else { return nil }
        var value = Float32(0)
        var size = UInt32(MemoryLayout<Float32>.size)
        let status = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &value)
        guard status == noErr else { return nil }
        return value
    }

    /// デバイスの音量を設定する（設定不可なら何もしない）
    private static func setVolume(_ value: Float, of device: AudioDeviceID) {
        var address = volumeAddress()
        guard AudioObjectHasProperty(device, &address) else { return }
        var settable: DarwinBoolean = false
        guard AudioObjectIsPropertySettable(device, &address, &settable) == noErr,
              settable.boolValue else { return }
        var v = Float32(max(0, min(1, value)))
        let size = UInt32(MemoryLayout<Float32>.size)
        AudioObjectSetPropertyData(device, &address, 0, nil, size, &v)
    }
}
