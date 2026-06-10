//
//  AppController.swift
//  全コンポーネントを統合する中央コントローラー
//
//  設計方針（Python 版の刷新で得た知見をそのまま適用）:
//  - ホットキーコールバックは軽量処理のみ（タップのスレッドをブロックしない）
//  - UI 状態は内部状態から一意に計算する（単一の状態発信点）
//  - 文字起こしは 正規化 → VAD → API → 貼り付け を非同期タスクで直列処理
//

import AppKit
import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "app")

/// ダブルタップ（auto_enter）の判定ウィンドウ（秒）
private let kDoubleTapWindow: TimeInterval = 0.4
/// 録音がこの秒数を超えたら自動停止する保険
private let kMaxRecordingSec: TimeInterval = 300
/// これより短い録音は誤操作とみなして破棄（秒）
private let kMinAudioSec = 0.3

@MainActor
final class AppController: ObservableObject {

    /// 現在のアプリ状態（メニューバーアイコン・HUD が購読）
    @Published private(set) var state: AppState = .idle

    let config = ConfigStore()
    let hud = HudController()

    private let recorder = AudioRecorder()
    private let hotkeys = HotkeyMonitor()

    /// スロット ID → トランスクライバ（設定変更時に作り直す）
    private var transcribers: [Int: Transcriber] = [:]

    // --- 録音状態 ---
    private var recordingSlot: Int?
    private var autoEnter = false
    /// 未完了の文字起こしタスク数
    private var outstanding = 0
    /// 録音の最大時間の保険タイマー
    private var failsafeTask: Task<Void, Never>?

    // --- ダブルタップ検出 ---
    private var lastReleaseTime: TimeInterval = 0
    private var lastReleaseSlot: Int?

    /// 文字起こしパイプラインの直列化チェーン。
    /// 連続録音時も「録音順にテキストが挿入される」ことを保証する
    private var pipelineTail: Task<Void, Never>?

    private var configObservation: Any?

    init() {
        rebuildTranscribers()

        // 設定変更でトランスクライバを作り直し、HUD 表示設定を反映
        configObservation = config.objectWillChange.sink { [weak self] _ in
            // objectWillChange は変更「前」に発火するため次のループで反映する
            DispatchQueue.main.async {
                guard let self else { return }
                self.rebuildTranscribers()
                self.hud.enabled = self.config.hudEnabled
            }
        }
        hud.enabled = config.hudEnabled

        // 音声レベル → HUD（audio スレッドから来るためメインへホップ）
        recorder.levelHandler = { [weak self] level in
            DispatchQueue.main.async {
                self?.hud.pushLevel(level)
            }
        }

        // ホットキーイベント（タップスレッドから来るためメインへホップ）
        hotkeys.onPress = { [weak self] token in
            let pressed = self?.hotkeys.pressedTokens ?? []
            DispatchQueue.main.async {
                self?.handlePress(token: token, pressed: pressed)
            }
        }
        hotkeys.onRelease = { [weak self] token in
            let pressed = self?.hotkeys.pressedTokens ?? []
            DispatchQueue.main.async {
                self?.handleRelease(token: token, pressed: pressed)
            }
        }
    }

    private var started = false

    /// アプリ起動時に呼ぶ（権限確認 → 監視開始）。多重呼び出しは無視する
    func startup() {
        guard !started else { return }
        started = true
        Task {
            // マイク権限（初回はシステムダイアログ）
            let micOK = await AudioRecorder.requestPermission()
            if !micOK {
                log.error("マイクの使用が許可されていません")
            }

            // 入力監視・アクセシビリティ権限の確認と監視開始
            let tapOK = hotkeys.start()
            checkPermissions(micGranted: micOK, tapCreated: tapOK)
        }
    }

    func shutdown() {
        hotkeys.stop()
    }

    // MARK: - ホットキーイベント（メインスレッド）

    private func handlePress(token: String, pressed: Set<String>) {
        if let slotId = recordingSlot {
            // toggle モード: 録音中の再押下で停止
            let slot = config.slot(slotId)
            if slot.mode == .toggle, slotMatches(slot, pressed: pressed) {
                finishRecording()
            }
            return
        }

        for slotId in [1, 2] {
            let slot = config.slot(slotId)
            guard !slot.hotkey.isEmpty, slotMatches(slot, pressed: pressed) else { continue }
            // ダブルタップ: 同スロットを短時間内に再押下 → auto_enter
            let now = ProcessInfo.processInfo.systemUptime
            let isDoubleTap = lastReleaseSlot == slotId
                && now - lastReleaseTime < kDoubleTapWindow
            beginRecording(slotId: slotId, autoEnter: isDoubleTap)
            break
        }
    }

    private func handleRelease(token: String, pressed: Set<String>) {
        guard let slotId = recordingSlot else { return }
        let slot = config.slot(slotId)
        guard slot.mode == .hold else { return }

        // 離されたキーがホットキーの構成キーなら録音停止
        let isHotkeyKey = slot.hotkey.contains { required in
            KeyToken.acceptableNames(for: required).contains(token)
        }
        if isHotkeyKey {
            lastReleaseTime = ProcessInfo.processInfo.systemUptime
            lastReleaseSlot = slotId
            finishRecording()
        }
    }

    /// スロットの必要キーがすべて押されているか
    private func slotMatches(_ slot: SlotConfig, pressed: Set<String>) -> Bool {
        slot.hotkey.allSatisfy { required in
            !KeyToken.acceptableNames(for: required).isDisjoint(with: pressed)
        }
    }

    // MARK: - 録音制御

    private func beginRecording(slotId: Int, autoEnter: Bool) {
        guard recordingSlot == nil else { return }
        recordingSlot = slotId
        self.autoEnter = autoEnter
        emitState()

        let slot = config.slot(slotId)
        log.info("録音開始 (スロット\(slotId), \(slot.backend.rawValue, privacy: .public)\(autoEnter ? ", auto_enter" : "", privacy: .public))")

        // 録音中に TLS 接続を事前確立して、停止後の初回 API 往復を短縮
        transcribers[slotId]?.prewarm()

        recorder.start { [weak self] ok in
            guard !ok else { return }
            DispatchQueue.main.async {
                guard let self else { return }
                self.recordingSlot = nil
                self.emitState()
                self.hud.notice("録音を開始できませんでした（マイクを確認）")
            }
        }

        // 録音時間の上限（release 取りこぼし等での永久録音を防ぐ保険）
        failsafeTask?.cancel()
        failsafeTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(kMaxRecordingSec))
            guard let self, !Task.isCancelled, self.recordingSlot != nil else { return }
            log.warning("録音時間が上限に達したため自動停止します")
            self.hud.notice("録音時間の上限に達したため停止しました")
            self.finishRecording()
        }
    }

    private func finishRecording() {
        guard let slotId = recordingSlot else { return }
        let useAutoEnter = autoEnter
        recordingSlot = nil
        autoEnter = false
        failsafeTask?.cancel()
        outstanding += 1
        emitState()

        recorder.stop { [weak self] samples in
            // audio キューから呼ばれる。メインへホップしてタスク起動
            DispatchQueue.main.async {
                self?.processAudio(samples, slotId: slotId, autoEnter: useAutoEnter)
            }
        }
    }

    // MARK: - 文字起こしパイプライン

    private func processAudio(_ samples: [Float], slotId: Int, autoEnter: Bool) {
        let vadEnabled = config.vadEnabled
        let delayMs = config.autoEnterDelayMs
        guard let transcriber = transcribers[slotId] else {
            taskFinished()
            return
        }

        // 直前のタスク完了を待ってから処理する（録音順のテキスト挿入を保証）
        let previous = pipelineTail
        pipelineTail = Task {
            await previous?.value
            defer { taskFinished() }

            let duration = Double(samples.count) / AudioRecorder.sampleRate
            guard duration >= kMinAudioSec else {
                log.info("録音が短すぎるためスキップ (\(String(format: "%.2f", duration))s)")
                return
            }

            // 1+2. 正規化と VAD（CPU 処理はメインスレッド外で実行される）
            guard let audio = await Self.prepareAudio(samples, vadEnabled: vadEnabled) else {
                log.info("発話が検出されなかったためスキップ")
                hud.notice("音声が検出されませんでした")
                return
            }

            // 3. API 文字起こし
            let text: String
            do {
                text = try await transcriber.transcribe(samples: audio)
            } catch let error as TranscriptionError {
                log.error("文字起こし失敗: \(error.message, privacy: .public)")
                hud.notice(error.message)
                return
            } catch {
                log.error("文字起こしで予期しないエラー: \(error.localizedDescription)")
                hud.notice("文字起こしに失敗しました")
                return
            }
            guard !text.isEmpty else {
                log.info("文字起こし結果が空でした")
                return
            }

            // 4. テキスト貼り付け（+ ダブルタップ時は Enter 自動送信）
            await Paster.paste(text)
            if autoEnter {
                try? await Task.sleep(for: .milliseconds(max(0, delayMs)))
                Paster.pressEnter()
            }
        }
    }

    /// 音量正規化 → 発話判定 → 無音トリミング。
    /// 発話が検出されなければ nil を返す。
    /// nonisolated async のため CPU 処理はメインスレッド外で実行される
    nonisolated private static func prepareAudio(
        _ samples: [Float], vadEnabled: Bool
    ) async -> [Float]? {
        // 音量正規化（ゲイン上限 +20dB。ノイズ増幅による幻覚を防ぐ）
        var audio = VoiceActivity.normalize(samples)

        guard vadEnabled else { return audio }

        // 発話がなければ API に送らない（幻覚と無駄コストの防止）
        guard await VoiceActivity.hasSpeech(audio) else { return nil }

        // 前後の無音・ノイズ区間をトリミング
        if let bounds = VoiceActivity.speechBounds(audio) {
            audio = Array(audio[bounds])
        }
        return audio
    }

    private func taskFinished() {
        outstanding -= 1
        emitState()
    }

    // MARK: - 状態計算（単一の発信点）

    private func emitState() {
        let newState: AppState
        if recordingSlot != nil {
            newState = .recording(autoEnter: autoEnter)
        } else if outstanding > 0 {
            newState = .transcribing
        } else {
            newState = .idle
        }
        if newState != state {
            state = newState
            hud.update(for: newState)
        }
    }

    // MARK: - 設定反映

    private func rebuildTranscribers() {
        for slotId in [1, 2] {
            let slot = config.slot(slotId)
            let existing = transcribers[slotId]
            // 同一バックエンドなら設定だけ更新（URLSession の接続を維持）
            if let existing, existing.backend == slot.backend {
                existing.model = slot.model
                existing.language = config.language
                existing.prompt = slot.prompt
            } else {
                transcribers[slotId] = Transcriber(
                    backend: slot.backend,
                    model: slot.model,
                    language: config.language,
                    prompt: slot.prompt
                )
            }
        }
    }

    // MARK: - 権限確認

    private func checkPermissions(micGranted: Bool, tapCreated: Bool) {
        let axTrusted = AXIsProcessTrusted()

        var problems: [String] = []
        if !tapCreated {
            problems.append("「入力監視」が許可されていません（ホットキーが反応しません）")
        }
        if !axTrusted {
            problems.append("「アクセシビリティ」が許可されていません（テキスト貼り付けができません）")
        }
        if !micGranted {
            problems.append("「マイク」が許可されていません（録音できません）")
        }
        guard !problems.isEmpty else { return }

        let alert = NSAlert()
        alert.messageText = "voicekey に権限が必要です"
        alert.informativeText = problems.joined(separator: "\n")
            + "\n\nシステム設定で許可した後、voicekey を再起動してください。"
        alert.addButton(withTitle: "システム設定を開く")
        alert.addButton(withTitle: "後で")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            let pane = !tapCreated
                ? "Privacy_ListenEvent"
                : (!axTrusted ? "Privacy_Accessibility" : "Privacy_Microphone")
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") {
                NSWorkspace.shared.open(url)
            }
        }
    }
}
