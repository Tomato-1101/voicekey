/// グローバルホットキー（⌥⌘S で字幕の開始/停止）
///
/// Carbon の `RegisterEventHotKey` を使う。`NSEvent.addGlobalMonitorForEvents` の
/// キーボード監視はアクセシビリティ許可（＝ユーザー承認プロンプト）が要るのに対し、
/// Carbon のホットキー登録は許可不要で、承認プロンプトを出さないという恒久方針に合う。
import AppKit
import Carbon.HIToolbox
import Foundation
import OSLog

/// アプリ全体で 1 つだけ登録するホットキー
@available(macOS 26.0, *)
final class CaptionHotKey {

    /// ホットキーの識別子（4 文字コード 'sgls'）
    private static let signature: OSType = 0x73_67_6C_73

    /// Carbon のコールバックは C 関数ポインタなので、self を渡せない。
    /// 唯一のインスタンスをここから引く。
    private static var current: CaptionHotKey?

    private let logger = makeCaptionLogger("CaptionHotKey")
    private let handler: () -> Void
    private var hotKeyRef: EventHotKeyRef?
    private var eventHandlerRef: EventHandlerRef?

    /// - Parameter handler: ホットキーが押されたときに呼ぶ処理（メインスレッドで呼ばれる）
    init(handler: @escaping () -> Void) {
        self.handler = handler
    }

    deinit {
        unregister()
    }

    /// ⌥⌘S を登録する
    ///
    /// - Returns: 登録できたか（他アプリが同じ組み合わせを取っていると失敗する）
    @discardableResult
    func register() -> Bool {
        guard hotKeyRef == nil else { return true }
        Self.current = self
        installEventHandler()

        var reference: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: Self.signature, id: 1)
        let status = RegisterEventHotKey(
            UInt32(kVK_ANSI_S),
            UInt32(cmdKey | optionKey),
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &reference
        )
        guard status == noErr, let reference else {
            logger.error("ホットキー ⌥⌘S を登録できませんでした status=\(status, privacy: .public)")
            return false
        }
        hotKeyRef = reference
        logger.notice("ホットキー ⌥⌘S を登録しました（字幕の開始/停止）")
        return true
    }

    /// 登録を解除する
    func unregister() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
        }
        if let eventHandlerRef {
            RemoveEventHandler(eventHandlerRef)
            self.eventHandlerRef = nil
        }
        if Self.current === self { Self.current = nil }
    }

    /// ホットキー押下イベントのハンドラを登録する
    private func installEventHandler() {
        guard eventHandlerRef == nil else { return }
        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, _ -> OSStatus in
                var hotKeyID = EventHotKeyID()
                let status = GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    hotKeyIDSize,
                    nil,
                    &hotKeyID
                )
                guard status == noErr, hotKeyID.signature == CaptionHotKey.signature else { return noErr }
                // Carbon のコールバックはメインスレッドで来るが、UI を触るので明示的に載せ替える
                DispatchQueue.main.async { CaptionHotKey.current?.handler() }
                return noErr
            },
            1,
            &eventType,
            nil,
            &eventHandlerRef
        )
    }
}

/// `GetEventParameter` に渡す EventHotKeyID のサイズ
///
/// C 関数ポインタのクロージャからは型のメンバへ触れないため、ファイルスコープに置く。
@available(macOS 26.0, *)
private let hotKeyIDSize = MemoryLayout<EventHotKeyID>.size
