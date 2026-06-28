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
    /// 音声入力履歴（貼り付けたテキストを最大 10 件保持。設定の「履歴」タブで再コピー可）
    let history = HistoryStore()
    /// 使用実績（節約時間・レベル・連続日数。設定の「実績」タブで表示。貼り付け後に集計する）
    let stats = StatsStore()

    private let recorder = AudioRecorder()
    private let hotkeys = HotkeyMonitor()
    /// 貼り付け前の LLM テキスト整形（失敗時は原文を返すため全スロットで共用できる）
    private let formatter = TextFormatter()

    /// スロット ID → トランスクライバ（設定変更時に作り直す）
    private var transcribers: [Int: Transcriber] = [:]

    // --- 録音状態 ---
    private var recordingSlot: Int?
    /// 録音中の実効モード（ハンズフリー切替キー併用時は hold スロットでも toggle になる）
    private var recordingEffectiveMode: HotkeyMode = .hold
    private var autoEnter = false
    /// ストリーミング録音中の Deepgram セッション（非ストリーミング時は nil）
    private var streamer: StreamingTranscriber?
    /// 未完了の文字起こしタスク数
    private var outstanding = 0
    /// 録音の最大時間の保険タイマー
    private var failsafeTask: Task<Void, Never>?

    // --- ダブルタップ検出 ---
    private var lastReleaseTime: TimeInterval = 0
    private var lastReleaseSlot: Int?
    /// 録音開始時刻（短いタップ＝ダブルタップ 1 打目の判定用）
    private var recordingStartedAt: TimeInterval = 0
    /// 短いタップの離鍵後、ダブルタップ 2 打目を待つ間の遅延停止タスク。
    /// 1 打目で録音を止めない（タップと同時に話し始めた声の冒頭を失わない）ための仕組み
    private var pendingTapFinish: Task<Void, Never>?

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

        // 録音中のマイク切断・構成変更 → 録音を確定して知らせる
        // （エンジンは静かに止まるため、放置するとユーザーは喋り続けるが何も入らない）
        recorder.deviceChangedHandler = { [weak self] in
            DispatchQueue.main.async {
                guard let self, self.recordingSlot != nil else { return }
                log.warning("録音中にオーディオ構成が変化したため録音を確定します")
                self.finishRecording()
                self.hud.notice("マイクの構成が変わったため録音を停止しました")
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
            } else {
                // 初回録音の開始遅延を減らすため、起動時に入力ユニットを温めておく
                recorder.prewarm()
            }

            // 製品版（ログイン済み）なら、起動時にサーバー接続を 1 回だけ暖機して初回の
            // サーバー往復（serverless cold start を含む最大数秒）を前払いする。暖機は
            // GET /me（無料枠を消費しない read）で TLS・接続・認証経路を温める。さらに
            // 有料ユーザーかつ Deepgram ストリーミング設定のときだけ、短命トークンも先取りする
            // （有料は consume 前に return＝消費ゼロ・トークンキャッシュも温まる）。無料体験
            // ユーザーはトークンを取得しない＝/ephemeral は無料枠を 1 消費するため、録音前の
            // 暖機で枠を減らさない。失敗は無視（録音時に通常経路で再取得される）。背景ループは張らない。
            if BackendClient.isLoggedIn {
                let warmTokenToo = config.streamingEnabled
                    && (config.slot(0).backend == .deepgram || config.slot(1).backend == .deepgram)
                Task {
                    guard let status = try? await BackendClient.fetchAccountStatus() else { return }
                    if status.active, warmTokenToo {
                        _ = try? await BackendClient.fetchEphemeralToken()
                    }
                }
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
            let slot = config.slot(slotId)
            // toggle 実効モード: 録音中の再押下で停止（ハンズフリー切替キー併用での起動も含む）
            if recordingEffectiveMode == .toggle, slotMatches(slot, pressed: pressed) {
                finishRecording()
                return
            }
            // hold 実効モード: 短いタップ後の待機中に同スロットが再押下されたらダブルタップ確定。
            // 録音は 1 打目から止めていないため、タップ中・タップ間の音声もすべて残っている
            if recordingEffectiveMode == .hold, pendingTapFinish != nil, slotMatches(slot, pressed: pressed) {
                pendingTapFinish?.cancel()
                pendingTapFinish = nil
                autoEnter = true
                emitState()
                log.info("ダブルタップ検出: 録音を継続して auto_enter を有効化 (スロット\(slotId))")
            }
            return
        }

        for slotId in [1, 2] {
            let slot = config.slot(slotId)
            guard !slot.hotkey.isEmpty, slotMatches(slot, pressed: pressed) else { continue }
            // ハンズフリー切替キーが併用されていれば、このスロットを toggle 実効で起動する
            // （切替キーは空でなく全キーが押されていること。スロット設定が hold でも toggle になる）
            let handsfree = !config.handsfreeKey.isEmpty && handsfreeKeyPressed(pressed)
            let effectiveMode: HotkeyMode = handsfree ? .toggle : slot.mode
            // ダブルタップ: 同スロットを短時間内に再押下 → auto_enter
            // （ハンズフリー toggle 起動時はダブルタップ判定を使わない）
            let now = ProcessInfo.processInfo.systemUptime
            let isDoubleTap = !handsfree && lastReleaseSlot == slotId
                && now - lastReleaseTime < kDoubleTapWindow
            beginRecording(slotId: slotId, autoEnter: isDoubleTap, effectiveMode: effectiveMode)
            break
        }
    }

    private func handleRelease(token: String, pressed: Set<String>) {
        guard let slotId = recordingSlot else { return }
        let slot = config.slot(slotId)
        // toggle 実効モード（ハンズフリー起動含む）は離鍵で止めない（再押下で停止）
        guard recordingEffectiveMode == .hold else { return }

        // 離されたキーがホットキーの構成キーなら録音停止
        let isHotkeyKey = slot.hotkey.contains { required in
            KeyToken.acceptableNames(for: required).contains(token)
        }
        if isHotkeyKey {
            // ダブルタップ確定後（autoEnter）の離鍵は 2 打目の待ち窓に入れず即確定する。
            // ここで再び kDoubleTapWindow 待つと、短い録音でも Enter 自動送信が 0.4 秒遅れていた
            if autoEnter {
                finishRecording()
                return
            }
            let now = ProcessInfo.processInfo.systemUptime
            if now - recordingStartedAt < kDoubleTapWindow {
                // 短いタップだけをダブルタップの 1 打目として記録する
                // （長い口述の直後に素早く次の録音を始めただけで
                //  auto_enter（Enter 自動送信）になってしまうのを防ぐ）
                lastReleaseTime = now
                lastReleaseSlot = slotId
                // 短いタップ＝ダブルタップの 1 打目の可能性。録音を止めずに 2 打目を待つ
                // （タップと同時に話し始めた声を失わない。2 打目が来なければ通常どおり確定。
                //  誤タップで発話が無い場合は通知を出さず静かに捨てる）
                pendingTapFinish?.cancel()
                pendingTapFinish = Task { [weak self] in
                    try? await Task.sleep(for: .seconds(kDoubleTapWindow))
                    guard let self, !Task.isCancelled else { return }
                    self.pendingTapFinish = nil
                    self.finishRecording(quietIfNoSpeech: true)
                }
            } else {
                finishRecording()
            }
        }
    }

    /// スロットの必要キーがすべて押されているか
    private func slotMatches(_ slot: SlotConfig, pressed: Set<String>) -> Bool {
        slot.hotkey.allSatisfy { required in
            !KeyToken.acceptableNames(for: required).isDisjoint(with: pressed)
        }
    }

    /// ハンズフリー切替キーがすべて押されているか（汎用修飾キーは左右どちらでも可）
    private func handsfreeKeyPressed(_ pressed: Set<String>) -> Bool {
        config.handsfreeKey.allSatisfy { required in
            !KeyToken.acceptableNames(for: required).isDisjoint(with: pressed)
        }
    }

    // MARK: - 録音制御

    private func beginRecording(slotId: Int, autoEnter: Bool, effectiveMode: HotkeyMode = .hold) {
        guard recordingSlot == nil else { return }
        recordingSlot = slotId
        recordingEffectiveMode = effectiveMode
        self.autoEnter = autoEnter
        recordingStartedAt = ProcessInfo.processInfo.systemUptime
        emitState()

        let slot = config.slot(slotId)
        log.info("録音開始 (スロット\(slotId), \(slot.backend.rawValue, privacy: .public)\(autoEnter ? ", auto_enter" : "", privacy: .public))")

        // 設定された入力デバイスを反映（変更がなければ recorder 側では何もしない）
        recorder.inputDeviceUID = config.inputDeviceUID

        // Deepgram かつストリーミング有効なら WebSocket を開いてライブ字幕を出す。
        // 開始できなければ（キー無し等）chunkHandler を張らないため REST 経路に自動フォールバック。
        // chunkHandler は録音開始前に張る必要がある（最初のチャンクを取りこぼさない）
        if config.streamingEnabled, slot.backend == .deepgram {
            let stream = StreamingTranscriber(model: slot.model, language: config.language)
            stream.onInterim = { [weak self] text in
                DispatchQueue.main.async { self?.hud.setCaption(text) }
            }
            if stream.start() {
                streamer = stream
                recorder.chunkHandler = { [weak stream] chunk in stream?.send(chunk) }
            }
        }

        // マイク起動を最優先で仕掛ける（プリウォーム類は後ろに置き、
        // メインスレッドの Keychain 読みなどで録音開始を遅らせない）
        recorder.start { [weak self] ok in
            guard !ok else { return }
            DispatchQueue.main.async {
                guard let self, self.recordingSlot == slotId else { return }
                // ストリーミングセッションも後始末する（残すと次の録音のチャンクが
                // 旧 WS に流れ、別バックエンドの録音に Deepgram の結果が混ざる）
                self.streamer?.cancel()
                self.streamer = nil
                self.recorder.chunkHandler = nil
                self.recordingSlot = nil
                self.emitState()
                self.hud.notice("録音を開始できませんでした（マイクを確認）")
            }
        }

        // 録音中に TLS 接続を事前確立して、停止後の初回 API 往復を短縮
        transcribers[slotId]?.prewarm()
        // 整形が有効なら整形 LLM への接続も録音中に温めておく
        if slot.formatEnabled {
            formatter.prewarm()
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

    private func finishRecording(quietIfNoSpeech: Bool = false) {
        guard let slotId = recordingSlot else { return }
        let useAutoEnter = autoEnter
        // ストリーミング送信を打ち切り、確定待ちはパイプライン側で行う
        let activeStreamer = streamer
        streamer = nil
        recorder.chunkHandler = nil
        recordingSlot = nil
        autoEnter = false
        pendingTapFinish?.cancel()
        pendingTapFinish = nil
        failsafeTask?.cancel()
        outstanding += 1
        emitState()

        recorder.stop { [weak self] samples in
            // audio キューから呼ばれる。メインへホップしてタスク起動
            DispatchQueue.main.async {
                self?.processAudio(samples, slotId: slotId,
                                   autoEnter: useAutoEnter, streamer: activeStreamer,
                                   quietIfNoSpeech: quietIfNoSpeech)
            }
        }
    }

    // MARK: - 文字起こしパイプライン

    private func processAudio(
        _ samples: [Float], slotId: Int, autoEnter: Bool, streamer: StreamingTranscriber?,
        quietIfNoSpeech: Bool = false
    ) {
        let vadEnabled = config.vadEnabled
        let splitEnabled = config.splitParallelEnabled
        let delayMs = config.autoEnterDelayMs
        // 整形設定もタスク実行中の設定変更に影響されないよう Task の外で捕捉する
        let slot = config.slot(slotId)
        let formatEnabled = slot.formatEnabled
        let formatPrompt = config.autoFormatPrompt
        let formatModel = config.formatModel
        guard let transcriber = transcribers[slotId] else {
            streamer?.cancel()
            taskFinished()
            return
        }

        // 直前のタスク完了を待ってから処理する（録音順のテキスト挿入を保証）
        let previous = pipelineTail
        pipelineTail = Task {
            await previous?.value
            defer { taskFinished() }

            // --- ストリーミング経路: Deepgram の確定テキストを受け取って貼り付け ---
            if let streamer {
                let streamed = await streamer.finish()
                hud.clearCaption()
                if !streamed.isEmpty {
                    // 整形が有効なら貼り付け前に LLM で整形（失敗時は原文が返る）
                    let formatted = formatEnabled
                        ? await formatter.format(streamed, prompt: formatPrompt, model: formatModel)
                        : streamed
                    // ユーザー辞書の確定置換を適用（API を通さないので遅延ゼロ）
                    let output = config.applyReplacements(formatted)
                    // 貼り付けに失敗しても履歴から救出できるよう、貼り付け前に記録する
                    history.add(output)
                    // 実績を集計（貼り付け後のローカル処理なので遅延に影響しない）
                    stats.recordSession(
                        characters: output.count,
                        recordingSeconds: Double(samples.count) / AudioRecorder.sampleRate
                    )
                    await Paster.paste(output)
                    if autoEnter {
                        try? await Task.sleep(for: .milliseconds(max(0, delayMs)))
                        Paster.pressEnter()
                    }
                    return
                }
                // 確定が空（接続失敗・無音など）→ 取得済みバッファで REST にフォールバック
                log.info("ストリーミング結果が空のため REST にフォールバックします")
            }

            // --- REST 経路（従来の 正規化 → VAD → API → 貼り付け）---
            let duration = Double(samples.count) / AudioRecorder.sampleRate
            guard duration >= kMinAudioSec else {
                log.info("録音が短すぎるためスキップ (\(String(format: "%.2f", duration))s)")
                return
            }

            // 1+2. 正規化と VAD（CPU 処理はメインスレッド外で実行される）
            guard let audio = await Self.prepareAudio(samples, vadEnabled: vadEnabled) else {
                log.info("発話が検出されなかったためスキップ")
                // 誤タップ由来（ダブルタップ待ちの期限切れ）の無音は通知しない
                if !quietIfNoSpeech {
                    hud.notice("音声が検出されませんでした")
                }
                return
            }

            // 3. API 文字起こし（長文は無音区間で分割し並列送信して待ち時間を短縮）
            let text: String
            do {
                text = try await Self.transcribeWithOptionalSplit(
                    audio, transcriber: transcriber, splitEnabled: splitEnabled
                )
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
            // 整形が有効なら貼り付け前に LLM で整形（失敗時は原文が返る）
            let formatted = formatEnabled
                ? await formatter.format(text, prompt: formatPrompt, model: formatModel)
                : text
            // ユーザー辞書の確定置換を適用（API を通さないので遅延ゼロ）
            let output = config.applyReplacements(formatted)
            // 貼り付けに失敗しても履歴から救出できるよう、貼り付け前に記録する
            history.add(output)
            // 実績を集計（貼り付け後のローカル処理なので遅延に影響しない）
            stats.recordSession(characters: output.count, recordingSeconds: duration)
            await Paster.paste(output)
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

        // 前後の無音トリミング + 発話間の長い無音の圧縮。
        // 音声が短くなるほどアップロードと API 処理が速くなる（語頭・語尾の余白と
        // 0.5 秒のポーズは保持するため精度には影響しない）
        if let condensed = VoiceActivity.condense(audio) {
            let cutSec = Double(audio.count - condensed.count) / AudioRecorder.sampleRate
            if cutSec > 0.1 {
                log.info("無音圧縮: \(String(format: "%.1f", cutSec))s 削減")
            }
            audio = condensed
        }
        return audio
    }

    /// 文字起こし（分割並列送信オプション付き）。
    /// 分割有効かつ長文なら無音区間で分割して並列送信し、結合テキストを返す。
    /// 分割送信が失敗（一部セグメントのエラー・429 等）したら全体 1 本送信に自動フォールバックする。
    nonisolated private static func transcribeWithOptionalSplit(
        _ samples: [Float], transcriber: Transcriber, splitEnabled: Bool
    ) async throws -> String {
        if splitEnabled {
            let segments = VoiceActivity.segment(samples)
            if segments.count >= 2 {
                log.info("長文を \(segments.count) 分割して並列送信します")
                do {
                    return try await Self.transcribeParallel(segments, transcriber: transcriber)
                } catch {
                    // 部分欠落を防ぐため、分割送信に失敗したら全体 1 本送信に切り替える
                    log.warning("分割送信に失敗したため全体送信にフォールバックします: \(error.localizedDescription)")
                }
            }
        }
        return try await transcriber.transcribe(samples: samples)
    }

    /// 各セグメントを並列に文字起こしし、index 昇順に結合する（同時数は URLSession が制限）
    nonisolated private static func transcribeParallel(
        _ segments: [[Float]], transcriber: Transcriber
    ) async throws -> String {
        var results = [String](repeating: "", count: segments.count)
        try await withThrowingTaskGroup(of: (Int, String).self) { group in
            for (i, seg) in segments.enumerated() {
                group.addTask { (i, try await transcriber.transcribe(samples: seg)) }
            }
            for try await (i, text) in group {
                results[i] = text
            }
        }
        return VoiceActivity.joinSegments(results)
    }

    private func taskFinished() {
        outstanding -= 1
        emitState()
    }

    // MARK: - 状態計算（単一の発信点）

    private func emitState() {
        let newState: AppState
        if recordingSlot != nil {
            newState = .recording(autoEnter: autoEnter, handsFree: recordingEffectiveMode == .toggle)
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
