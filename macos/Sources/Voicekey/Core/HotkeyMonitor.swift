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

    /// 直近に処理したキーイベントの CGEvent タイムスタンプ（ログ専用・タップスレッドからのみ更新）。
    /// onPress/onRelease は同じスレッドで同期に呼ばれるので、コールバック内で読めば当該イベントの値になる
    private(set) var lastEventTimestamp: CGEventTimestamp = 0

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
                    ActionLog.shared.write("hotkey", "ウォッチドッグ発火: イベントタップを再有効化")
                    // 無効化中の離鍵は届いていないので、ここでも押下状態を実キーボードに合わせる
                    // （タイマーはタップと同じ RunLoop＝同じスレッドで動くので、イベント処理と競合しない）
                    self.resyncPressedTokens()
                }
            }
            // 5 秒ごとの健全性チェックはホットキー入力の判定には関与しない
            // （判定は keyDown/keyUp イベント駆動）ため、tolerance を許容して起こされ方を緩める
            CFRunLoopTimerSetTolerance(timer, 0.5)
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
        ActionLog.shared.write("hotkey", "ホットキー監視を開始")
        return true
    }

    /// 入力監視権限の実効チェック用に、listen-only タップを試作して即破棄する（オンボーディング用）。
    /// 権限（TCC）が付いていても、プロセス起動時にタップが作れなかった環境では再起動するまで
    /// 有効化されないことが実測である。作成できたかどうかだけを返し、監視は開始しない。
    static func canCreateEventTap() -> Bool {
        let mask: CGEventMask = 1 << CGEventType.keyDown.rawValue
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: mask,
            callback: { _, _, event, _ in Unmanaged.passUnretained(event) },
            userInfo: nil
        ) else {
            return false
        }
        // 実際には駆動しないので、作った直後に無効化して破棄する
        CGEvent.tapEnable(tap: tap, enable: false)
        CFMachPortInvalidate(tap)
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
                ActionLog.shared.write("hotkey", "イベントタップ無効化を検知し再有効化 (理由=\(reason))")
                resyncPressedTokens()
            }

        case .flagsChanged:
            lastEventTimestamp = event.timestamp
            // 修飾キー: デバイス依存ビットから現在の押下集合を計算し、差分を通知
            let current = KeyToken.modifierTokens(from: event.flags)
            let previous = pressedTokens.filter { Self.isModifierToken($0) }
            for token in current.subtracting(previous) {
                pressedTokens.insert(token)
                onPress?(token)
            }
            for token in previous.subtracting(current) {
                pressedTokens.remove(token)
                onRelease?(token)
            }

        case .keyDown:
            lastEventTimestamp = event.timestamp
            // OS のキーリピートは無視（エッジ検出）
            guard event.getIntegerValueField(.keyboardEventAutorepeat) == 0 else { return }
            let keyCode = event.getIntegerValueField(.keyboardEventKeycode)
            guard let token = KeyToken.token(forKeyCode: keyCode) else { return }
            if !pressedTokens.contains(token) {
                pressedTokens.insert(token)
                onPress?(token)
            }

        case .keyUp:
            lastEventTimestamp = event.timestamp
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

    /// タップ再有効化時に、押下中集合を実際のキーボード状態に合わせる（タップスレッド上）。
    ///
    /// 無効化されている間の離鍵はタップに届かないため、放置すると「離したのに押下中」のまま残り、
    /// hold モードの録音が上限まで止まらない。消えたキーは通常の離鍵と同じ onRelease で知らせる。
    /// 逆に無効化中に新しく押されたキーは押下通知しない（意図しない録音開始を避ける。
    /// 押し続けていれば次の flagsChanged / keyDown で普通に拾われる）。
    private func resyncPressedTokens() {
        guard !pressedTokens.isEmpty else { return }
        // 左右の区別はデバイス依存ビット頼み。そのビットが状態取得で欠けていても誤って離鍵扱いに
        // しない（握っている最中の録音を切らない）よう、修飾キーのキーコード単位の状態も合わせて見る
        var currentModifiers = KeyToken.modifierTokens(
            from: CGEventSource.flagsState(.combinedSessionState))
        for (code, token) in Self.modifierKeyCodes
        where CGEventSource.keyState(.combinedSessionState, key: code) {
            currentModifiers.insert(token)
        }
        let released = Self.tokensToRelease(
            pressed: pressedTokens,
            currentModifiers: currentModifiers,
            isKeyDown: { token in
                // 同じトークンに複数のキーコードがある（enter=Return/テンキー Enter）ので、どれか押されていれば押下中
                KeyToken.keyCodeTokens.contains { code, name in
                    name == token && CGEventSource.keyState(.combinedSessionState, key: CGKeyCode(code))
                }
            })
        guard !released.isEmpty else { return }
        let list = released.sorted().joined(separator: ",")
        log.warning("タップ再有効化で押下状態を補正: \(list, privacy: .public)")
        ActionLog.shared.write("hotkey", "[ホットキー] タップ再有効化で押下状態を補正 離鍵扱い=\(list)")
        for token in released.sorted() {
            pressedTokens.remove(token)
            onRelease?(token)
        }
    }

    /// 押下中と記録しているトークンのうち、実際にはもう離されているもの（離鍵扱いにするもの）を返す。
    /// 副作用のない純ロジック（テスト対象）。
    ///
    /// - Parameters:
    ///   - pressed: 押下中として記録しているトークン集合
    ///   - currentModifiers: 実キーボードで今押されている修飾キートークン集合
    ///   - isKeyDown: 修飾キー以外のトークンが今押されているか。確かめられないキーは false を返す
    ///     （押しっぱなし扱いで録音が止まらないより、離鍵扱いで止める方が安全なため）
    static func tokensToRelease(pressed: Set<String>,
                                currentModifiers: Set<String>,
                                isKeyDown: (String) -> Bool) -> Set<String> {
        pressed.filter { token in
            isModifierToken(token) ? !currentModifiers.contains(token) : !isKeyDown(token)
        }
    }

    /// 修飾キーのキーコード（kVK_*）→ トークン。押下状態の補正にだけ使う
    private static let modifierKeyCodes: [(code: CGKeyCode, token: String)] = [
        (59, "ctrl_l"), (62, "ctrl_r"), (56, "shift_l"), (60, "shift_r"),
        (55, "cmd_l"), (54, "cmd_r"), (58, "alt_l"), (61, "alt_r"), (63, "fn"),
    ]

    private static func isModifierToken(_ token: String) -> Bool {
        token == "fn" || KeyToken.modifierBits.contains { $0.token == token }
    }
}
