//
//  HotkeyMonitor.swift
//  CGEventTap によるグローバルキー監視
//
//  - listen-only タップ（イベントを消費しない）
//  - 修飾キーの左右は CGEventFlags のデバイス依存ビットで厳密に判定
//    （Python/pynput 版は左修飾キーが汎用名で報告されホットキーが効かなかった）
//  - tapDisabledByTimeout を受けたら即座に再有効化する
//    （pynput はこれを行わず、ハンドラが一度ブロックするとホットキーが
//    永久に死んでいた。本実装はコールバックが軽量なうえ、万一無効化されても
//    自動復旧する）
//

import CoreGraphics
import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "hotkey")

final class HotkeyMonitor {

    /// キー押下（トークン）。タップのスレッドから呼ばれるため軽量処理のみにすること
    var onPress: ((String) -> Void)?
    /// キー解放（トークン）
    var onRelease: ((String) -> Void)?

    /// 現在押下中のトークン集合（タップスレッドからのみ更新）
    private(set) var pressedTokens: Set<String> = []

    private var tap: CFMachPort?
    private var runLoop: CFRunLoop?
    private var thread: Thread?

    /// 監視を開始する。
    /// - Returns: タップ作成に成功したか（失敗は入力監視権限の不足）
    @discardableResult
    func start() -> Bool {
        guard tap == nil else { return true }

        let mask: CGEventMask =
            (1 << CGEventType.keyDown.rawValue) |
            (1 << CGEventType.keyUp.rawValue) |
            (1 << CGEventType.flagsChanged.rawValue)

        let selfPtr = Unmanaged.passUnretained(self).toOpaque()
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: mask,
            callback: { _, type, event, userInfo in
                guard let userInfo else { return Unmanaged.passUnretained(event) }
                let monitor = Unmanaged<HotkeyMonitor>.fromOpaque(userInfo).takeUnretainedValue()
                monitor.handle(type: type, event: event)
                return Unmanaged.passUnretained(event)
            },
            userInfo: selfPtr
        ) else {
            log.error("イベントタップを作成できません（入力監視権限を確認してください）")
            return false
        }

        self.tap = tap

        // 専用スレッドの RunLoop でタップを駆動する（メインスレッドを汚さない）
        let thread = Thread { [weak self] in
            guard let self, let tap = self.tap else { return }
            let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
            self.runLoop = CFRunLoopGetCurrent()
            CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
            CGEvent.tapEnable(tap: tap, enable: true)

            // ウォッチドッグ: tapDisabledByTimeout の通知自体を取りこぼしても
            // 復旧できるよう、5 秒ごとにタップの有効状態を確認して再有効化する
            let timer = CFRunLoopTimerCreateWithHandler(
                kCFAllocatorDefault, CFAbsoluteTimeGetCurrent() + 5, 5, 0, 0
            ) { [weak self] _ in
                guard let self, let tap = self.tap else { return }
                if !CGEvent.tapIsEnabled(tap: tap) {
                    CGEvent.tapEnable(tap: tap, enable: true)
                    log.warning("ウォッチドッグ: 無効化されたイベントタップを再有効化しました")
                }
            }
            CFRunLoopAddTimer(CFRunLoopGetCurrent(), timer, .commonModes)
            CFRunLoopRun()
        }
        thread.name = "voicekey.hotkey-tap"
        // App Nap やスレッド調停で遅延するとタップが OS にタイムアウト無効化
        // されるため、最高優先度で動かす
        thread.qualityOfService = .userInteractive
        thread.start()
        self.thread = thread
        log.info("ホットキー監視を開始しました")
        return true
    }

    /// 監視を停止する
    func stop() {
        if let tap {
            CGEvent.tapEnable(tap: tap, enable: false)
        }
        if let runLoop {
            CFRunLoopStop(runLoop)
        }
        tap = nil
        runLoop = nil
        thread = nil
    }

    // MARK: - イベント処理（タップスレッド上）

    private func handle(type: CGEventType, event: CGEvent) {
        switch type {
        case .tapDisabledByTimeout, .tapDisabledByUserInput:
            // OS にタップを無効化されたら即座に再有効化（ホットキー永久無反応の防止）。
            // 原因の切り分けのため無効化理由も記録する
            // （timeout=コールバック遅延 / userInput=セキュア入力などによる強制無効化）
            if let tap {
                CGEvent.tapEnable(tap: tap, enable: true)
                let reason = type == .tapDisabledByTimeout ? "timeout" : "userInput"
                log.warning("イベントタップが無効化されたため再有効化しました (理由: \(reason, privacy: .public))")
            }

        case .flagsChanged:
            // 修飾キー: デバイス依存ビットから現在の押下集合を計算し、差分を通知
            let current = KeyToken.modifierTokens(from: event.flags)
            let previous = pressedTokens.filter { isModifierToken($0) }
            for token in current.subtracting(previous) {
                pressedTokens.insert(token)
                onPress?(token)
            }
            for token in previous.subtracting(current) {
                pressedTokens.remove(token)
                onRelease?(token)
            }

        case .keyDown:
            // OS のキーリピートは無視（エッジ検出）
            guard event.getIntegerValueField(.keyboardEventAutorepeat) == 0 else { return }
            let keyCode = event.getIntegerValueField(.keyboardEventKeycode)
            guard let token = KeyToken.token(forKeyCode: keyCode) else { return }
            if !pressedTokens.contains(token) {
                pressedTokens.insert(token)
                onPress?(token)
            }

        case .keyUp:
            let keyCode = event.getIntegerValueField(.keyboardEventKeycode)
            guard let token = KeyToken.token(forKeyCode: keyCode) else { return }
            if pressedTokens.contains(token) {
                pressedTokens.remove(token)
                onRelease?(token)
            }

        default:
            break
        }
    }

    private func isModifierToken(_ token: String) -> Bool {
        token == "fn" || KeyToken.modifierBits.contains { $0.token == token }
    }
}
