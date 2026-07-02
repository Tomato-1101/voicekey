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
    /// ハンズフリー(toggle 実効)録音で groq スロットの代わりに使う ElevenLabs(scribe_v1) トランスクライバ。
    /// 長時間録音の精度対策。言語変更時も rebuildTranscribers で作り直す（backend/model は固定）。
    private var handsfreeTranscriber: Transcriber?

    // --- 録音状態 ---
    private var recordingSlot: Int?
    /// 録音中の実効モード（ハンズフリー切替キー併用時は hold スロットでも toggle になる）
    private var recordingEffectiveMode: HotkeyMode = .hold
    private var autoEnter = false
    /// ストリーミング録音中の Deepgram セッション（非ストリーミング時は nil）
    private var streamer: StreamingTranscriber?
    /// 録音開始時に確定した不変の処理コンテキスト（finishRecording で処理タスクへ引き継ぐ）
    private var recordContext: RecordContext?
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

    /// /ephemeral 関数を一定間隔で温め続けるタイマー（Deepgram ストリーミング利用時の cold start 回避）
    private var warmTimer: Timer?
    /// App Nap 抑止トークン。放置中も暖機ループ（セッション/トークンの温存）を間引かせないための
    /// もの＝「放置後の初回録音が遅い」の根治。ログイン中のみ保持し、shutdown で解放する。
    private var antiNapActivity: NSObjectProtocol?

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

    /// アプリ起動時に呼ぶ（権限確認 → 監視開始）。多重呼び出しは無視する。
    /// - Parameter showPermissionAlert: 権限不足時の NSAlert を出すか。オンボーディング完了直後は
    ///   すでに権限を案内済みなので false を渡し、案内が二重に出ないようにする。
    func startup(showPermissionAlert: Bool = true) {
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
            // サーバー往復（serverless cold start を含む最大数秒）を前払いする。段階3では
            // 有料・無料とも再利用可トークンを先取りしてキャッシュへ載せる（消費ゼロ・往復ゼロで
            // 話し始められる）。トークン先取りが認証経路・serverless 関数もまとめて温める。
            // 失敗は無視（録音時に通常経路で再取得される）。継続的な温存は warmTimer が担う。
            if BackendClient.isLoggedIn {
                Task { await self.warmBackendsNow() }  // 有料・無料とも短命トークン先読み（消費ゼロ）
                startEphemeralWarmLoop()  // 以後も数分間隔でプロキシ関数を温存（cold start 回避）
                // 放置中も暖機ループ（セッション/トークン温存）が確実に回るよう App Nap を抑止する。
                // .background＝システムスリープは妨げず、アイドル時のタイマー間引き（nap）だけ止める。
                // これで長時間放置後の初回でもセッション/トークンが手元に温存され、更新往復を踏まない。
                if antiNapActivity == nil {
                    antiNapActivity = ProcessInfo.processInfo.beginActivity(
                        options: .background,
                        reason: "keep auth session and transcription token warm"
                    )
                }
                // スリープ復帰直後は serverless 関数が冷え・短命トークンも失効済み＝「放置後の
                // 初回録音が遅い」の主因。復帰イベントで即・暖機＋（有料は）トークン先読みして、
                // 復帰後の話し始めの待ちを消す（暖機タイマーは最大4分後までしか効かないため）。
                NSWorkspace.shared.notificationCenter.addObserver(
                    forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
                ) { [weak self] _ in
                    Task { @MainActor in await self?.warmBackendsNow() }
                }
            }

            // 入力監視・アクセシビリティ権限の確認と監視開始
            let tapOK = hotkeys.start()
            checkPermissions(micGranted: micOK, tapCreated: tapOK, showAlert: showPermissionAlert)
        }
    }

    func shutdown() {
        hotkeys.stop()
        warmTimer?.invalidate()
        warmTimer = nil
        if let activity = antiNapActivity {
            ProcessInfo.processInfo.endActivity(activity)
            antiNapActivity = nil
        }
    }

    /// 製品版で使用中のプロキシ関数（/ephemeral・/transcribe/elevenlabs）を一定間隔で温める。
    /// cold start（Vercel serverless の起動）が「話し始めの遅延」の主因なので、消費なしの
    /// 軽量 GET で関数を温存し、録音ごとの POST を warm path に乗せる。
    /// 重い常駐ループは張らず、約 4 分間隔で GET を打つだけ（Vercel の warm 保持は数分程度）。
    /// 使っていないプロバイダの関数は温めない（設定スロットを見て該当時だけ打つ）。
    private func startEphemeralWarmLoop() {
        warmTimer?.invalidate()
        warmTimer = Timer.scheduledTimer(withTimeInterval: 240, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.warmBackendsNow() }
        }
    }

    /// 使用中プロバイダのサーバー関数を温め、有料なら短命トークンを先読みする（消費ゼロ）。
    /// 起動時・録音後・暖機ループ・スリープ復帰直後に共通で呼ぶ。
    /// 有料: トークンを先読みしてキャッシュ＝アイドル中も常に手元に有効トークンがあり録音開始ゼロ待ち
    ///       （TTL(300s) > 暖機間隔(240s) なのでキャッシュは切れない）。
    /// 無料: 枠を守るためトークンは取らず、消費なしの GET で関数だけ温める（実トークンは録音時取得）。
    private func warmBackendsNow() async {
        guard BackendClient.isLoggedIn else { return }
        // 放置後初回の「セッション期限切れ→更新往復」を録音時に踏まないよう、暖機のたびに
        // 先回りでセッションを有効化しておく（Groq/EL プロキシは録音時に ensureValidSession を
        // 呼ぶが、それを事前に済ませておけば録音のクリティカルパスから更新往復が消える）。
        // App Nap でこのループが間引かれても、スリープ復帰ハンドラ経由で必ず通る。
        try? await AuthClient.ensureValidSession()
        let usesDeepgramStreaming = config.streamingEnabled
            && (config.slot(1).backend == .deepgram || config.slot(2).backend == .deepgram)
        let usesGroq = config.slot(1).backend == .groq || config.slot(2).backend == .groq
        // ハンズフリーで内部利用する EL を温める条件: groq スロットが toggle か、または
        // ハンズフリー切替キーが設定されている（hold スロットも切替キー併用で toggle 実効になる）。
        // EL は選択肢から外れたので、backend == .elevenlabs では永久に温まらない。
        let handsfreeConfigured = !config.handsfreeKey.isEmpty
        let usesElevenLabs =
            (config.slot(1).backend == .groq && (config.slot(1).mode == .toggle || handsfreeConfigured))
            || (config.slot(2).backend == .groq && (config.slot(2).mode == .toggle || handsfreeConfigured))
        if usesDeepgramStreaming {
            // 段階3: 有料も無料も再利用可トークンを先読みしてキャッシュを温める＝録音開始のサーバー往復ゼロ。
            // 先読み自体は無料枠を消費しない（消費は録音成立後の非同期 confirm で数える）。これで
            // 「無料の話し始めが遅い」の主因だった録音ごとのトークン取り直し（cold start 込み）を根絶する。
            _ = try? await BackendClient.fetchEphemeralToken()
        }
        if usesGroq {
            // 普通入力=Groq プロキシの関数・接続を温める（Edge なので cold start はほぼ無いが導線統一）。
            await BackendClient.warmGroqTranscribe()
        }
        if usesElevenLabs {
            await BackendClient.warmElevenLabs()
        }
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

    /// 録音開始時に確定する不変の処理コンテキスト（スナップショット）。
    ///
    /// 設定の hot-reload やスロット変更が「録音中〜処理開始」の間に挟まっても、
    /// 録音開始時点で選ばれた transcriber（＝プロバイダー）と処理フラグだけで
    /// 文字起こしを完遂させるために使う。これが無いと、滞留中に backend が変わると
    /// 録音開始時とは別プロバイダーへ音声を送ってしまう。transcriber は参照を固定する
    /// （backend 変更時は rebuildTranscribers が新インスタンスを作るため、開始時の
    /// プロバイダーが確実に保持される）。
    private struct RecordContext {
        let transcriber: Transcriber?
        let vadEnabled: Bool
        let splitEnabled: Bool
        let autoEnterDelayMs: Int
        let formatEnabled: Bool
        let formatPrompt: String
        let formatModel: String
        /// サーバー統合整形（Groq プロキシで STT と整形を 1 リクエストに）を使えるか。
        /// groq スロット × 整形 ON × ログイン済み × ハンズフリー EL 差し替えでない、が条件。
        /// 単発送信のときだけこれを見て serverFormat=true にし、後段のクライアント整形をスキップする。
        let serverFormatEligible: Bool
    }

    private func beginRecording(slotId: Int, autoEnter: Bool, effectiveMode: HotkeyMode = .hold) {
        guard recordingSlot == nil else { return }
        recordingSlot = slotId
        recordingEffectiveMode = effectiveMode
        self.autoEnter = autoEnter
        recordingStartedAt = ProcessInfo.processInfo.systemUptime
        emitState()

        let slot = config.slot(slotId)
        log.info("録音開始 (スロット\(slotId), \(slot.backend.rawValue, privacy: .public)\(autoEnter ? ", auto_enter" : "", privacy: .public))")

        // ハンズフリー(toggle 実効)で groq スロットのときは、この録音の処理エンジンだけ内部で
        // ElevenLabs(scribe_v1) に差し替える（長時間録音の精度対策。保存値 groq は変えない）。
        // それ以外（hold 実効・groq 以外）は選択中スロットの transcriber をそのまま使う。
        let usesHandsfreeEL = effectiveMode == .toggle && slot.backend == .groq
        let activeTranscriber = usesHandsfreeEL ? handsfreeTranscriber : transcribers[slotId]
        if usesHandsfreeEL {
            log.info("ハンズフリー: 内部エンジン切替 (groq→elevenlabs)")
        }

        // サーバー統合整形（Groq プロキシで STT と整形を 1 リクエストに）が使えるか。
        // 条件: activeTranscriber が groq（＝ハンズフリー EL 差し替えなら backend が .elevenlabs に
        // なるので自動的に除外される）× 整形 ON × ログイン済み。単発送信のときだけ発動する。
        let serverFormatEligible = activeTranscriber?.backend == .groq
            && slot.formatEnabled
            && BackendClient.isLoggedIn

        // 録音開始時点の transcriber（プロバイダー）と処理フラグを不変スナップショット化する。
        // 設定 hot-reload やスロット変更が録音中〜処理開始に挟まっても開始時の設定で完遂する
        recordContext = RecordContext(
            transcriber: activeTranscriber,
            vadEnabled: config.vadEnabled,
            splitEnabled: config.splitParallelEnabled,
            autoEnterDelayMs: config.autoEnterDelayMs,
            formatEnabled: slot.formatEnabled,
            formatPrompt: config.autoFormatPrompt,
            formatModel: config.formatModel,
            serverFormatEligible: serverFormatEligible
        )

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
                self.recordContext = nil
                self.recorder.chunkHandler = nil
                self.recordingSlot = nil
                self.emitState()
                self.hud.notice("録音を開始できませんでした（マイクを確認）")
            }
        }

        // 録音中に TLS 接続を事前確立して、停止後の初回 API 往復を短縮（切替時は EL 側を温める）
        activeTranscriber?.prewarm()
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
        guard recordingSlot != nil else { return }
        let useAutoEnter = autoEnter
        // ストリーミング送信を打ち切り、確定待ちはパイプライン側で行う
        let activeStreamer = streamer
        let context = recordContext  // 録音開始時に確定した処理コンテキスト
        streamer = nil
        recordContext = nil
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
                self?.processAudio(samples, context: context,
                                   autoEnter: useAutoEnter, streamer: activeStreamer,
                                   quietIfNoSpeech: quietIfNoSpeech)
            }
        }
    }

    // MARK: - 文字起こしパイプライン

    private func processAudio(
        _ samples: [Float], context: RecordContext?, autoEnter: Bool,
        streamer: StreamingTranscriber?, quietIfNoSpeech: Bool = false
    ) {
        // ライブ設定でなく録音開始時の snapshot だけを使う
        // （滞留中に設定が変わっても別プロバイダーへ送らないため）
        guard let context, let transcriber = context.transcriber else {
            streamer?.cancel()
            taskFinished()
            return
        }
        let vadEnabled = context.vadEnabled
        let splitEnabled = context.splitEnabled
        let delayMs = context.autoEnterDelayMs
        let formatEnabled = context.formatEnabled
        let formatPrompt = context.formatPrompt
        let formatModel = context.formatModel
        let serverFormatEligible = context.serverFormatEligible

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
            // [計測] 録音停止→貼付までを段階別に刻む。release と main の速度差の在り処を実測するため。
            // release は普通入力=Groq プロキシ・ハンズフリー=EL プロキシとも、この REST 経路を通る
            // （高速リアルタイム=Deepgram を選択肢から外したのでストリーミング経路は不使用）。
            let restT0 = ProcessInfo.processInfo.systemUptime
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
            let vadMs = Int((ProcessInfo.processInfo.systemUptime - restT0) * 1000)

            // 3. API 文字起こし（長文は無音区間で分割し並列送信して待ち時間を短縮）。
            // 単発送信かつサーバー統合整形が使えるときは、STT と整形を 1 リクエストで済ませる
            // （didServerFormat=true で返る）。分割送信のときは各セグメント整形なし→結合後にクライアント整形。
            let sttStart = ProcessInfo.processInfo.systemUptime
            let text: String
            let didServerFormat: Bool
            do {
                (text, didServerFormat) = try await Self.transcribeWithOptionalSplit(
                    audio, transcriber: transcriber, splitEnabled: splitEnabled,
                    serverFormat: serverFormatEligible
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
            let sttMs = Int((ProcessInfo.processInfo.systemUptime - sttStart) * 1000)
            guard !text.isEmpty else {
                log.info("文字起こし結果が空でした")
                return
            }

            // 4. テキスト貼り付け（+ ダブルタップ時は Enter 自動送信）
            // 整形: サーバー統合済み（didServerFormat）なら再整形しない。サーバーが整形失敗
            // （formatted:false）でも text に STT 原文が入って返るため、ここで再整形すると同じ
            // Groq 障害で二度失敗するだけ＋遅延増になる。よってクライアント側の再整形はしない。
            // 統合でなく整形 ON のときだけ貼り付け前に LLM 整形する（失敗時は原文が返る）。
            let fmtStart = ProcessInfo.processInfo.systemUptime
            let formatted: String
            if didServerFormat {
                formatted = text
            } else if formatEnabled {
                formatted = await formatter.format(text, prompt: formatPrompt, model: formatModel)
            } else {
                formatted = text
            }
            let fmtMs = Int((ProcessInfo.processInfo.systemUptime - fmtStart) * 1000)
            // ユーザー辞書の確定置換を適用（API を通さないので遅延ゼロ）
            let output = config.applyReplacements(formatted)
            // 貼り付けに失敗しても履歴から救出できるよう、貼り付け前に記録する
            history.add(output)
            // 実績を集計（貼り付け後のローカル処理なので遅延に影響しない）
            stats.recordSession(characters: output.count, recordingSeconds: duration)
            let pasteStart = ProcessInfo.processInfo.systemUptime
            await Paster.paste(output)
            let pasteMs = Int((ProcessInfo.processInfo.systemUptime - pasteStart) * 1000)
            let totalMs = Int((ProcessInfo.processInfo.systemUptime - restT0) * 1000)
            // 統合時は整形を STT と一緒に済ませたことが分かるよう「整形 サーバー統合」と表記する
            let fmtDesc = didServerFormat ? "整形 サーバー統合" : "整形 \(fmtMs)"
            log.info("[計測] \(transcriber.backend.label, privacy: .public) 録音停止→貼付 総計\(totalMs)ms（VAD \(vadMs) / 文字起こし \(sttMs) / \(fmtDesc, privacy: .public) / 貼付 \(pasteMs)）")
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

    /// 文字起こし（分割並列送信オプション付き）。戻り値は (テキスト, サーバー統合整形したか)。
    /// 分割有効かつ長文なら無音区間で分割して並列送信し、結合テキストを返す（各セグメントは
    /// 整形なしで送るため didServerFormat=false＝呼び出し側が結合後にクライアント整形する）。
    /// 分割送信が失敗（一部セグメントのエラー・429 等）したら全体 1 本送信に自動フォールバックする。
    /// 単発送信（分割しない/分割失敗フォールバック）では serverFormat をそのまま transcribe へ渡し、
    /// その値を didServerFormat として返す（serverFormat は groq プロキシ経路でのみ実効＝呼び出し側で
    /// ログイン済み groq に限定済みなので、true なら必ず整形まで済んだテキストが返る）。
    nonisolated private static func transcribeWithOptionalSplit(
        _ samples: [Float], transcriber: Transcriber, splitEnabled: Bool, serverFormat: Bool
    ) async throws -> (text: String, didServerFormat: Bool) {
        if splitEnabled {
            let segments = VoiceActivity.segment(samples)
            if segments.count >= 2 {
                log.info("長文を \(segments.count) 分割して並列送信します")
                do {
                    // 分割送信は各セグメント整形なし。結合後にクライアント整形するため didServerFormat=false
                    return (try await Self.transcribeParallel(segments, transcriber: transcriber), false)
                } catch {
                    // 部分欠落を防ぐため、分割送信に失敗したら全体 1 本送信に切り替える
                    log.warning("分割送信に失敗したため全体送信にフォールバックします: \(error.localizedDescription)")
                }
            }
        }
        return (try await transcriber.transcribe(samples: samples, serverFormat: serverFormat), serverFormat)
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
        // 録音 1 回ごとに残量表示を静かに更新する（消費はサーバーが原子的に行うので、
        // アプリは最新の残量を取り直して「使うたびに残り回数が即減って見える」を担保する）。
        // クリティカルパス外（貼り付け後）。有料・未ログインは内部で no-op になる。
        LoginCoordinator.shared.refreshEntitlementQuiet()

        // 次録音の開始遅延を減らすため、文字起こし完了直後に次回トークンを先読みする
        // （連続ディクテーション時の 2 回目以降のサーバー往復をクリティカルパスから外す）。
        // 段階3: 有料も無料も再利用可トークンを先取りしてキャッシュ（TTL 内の次録音は往復ゼロ）。
        // 先読みは無料枠を消費しない（消費は録音成立後の非同期 confirm で数える）。
        if BackendClient.isLoggedIn, config.streamingEnabled,
           config.slot(1).backend == .deepgram || config.slot(2).backend == .deepgram {
            Task { _ = try? await BackendClient.fetchEphemeralToken() }
        }
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
        // ハンズフリー(toggle 実効)で groq スロットが使われるときに差し替える EL(scribe_v1) を常設する。
        // 言語変更でも作り直されるよう、スロット transcriber と同じ再構築フローに乗せる（backend/model は固定）。
        if let existing = handsfreeTranscriber, existing.backend == .elevenlabs {
            existing.model = Backend.elevenlabs.defaultModel
            existing.language = config.language
            existing.prompt = ""
        } else {
            handsfreeTranscriber = Transcriber(
                backend: .elevenlabs,
                model: Backend.elevenlabs.defaultModel,
                language: config.language,
                prompt: ""
            )
        }
    }

    // MARK: - 権限確認

    private func checkPermissions(micGranted: Bool, tapCreated: Bool, showAlert: Bool = true) {
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
        // オンボーディング完了直後は権限案内を済ませているため、NSAlert を二重に出さない
        // （不足はログにだけ残す）
        guard showAlert else {
            log.warning("権限不足（オンボーディング後のためアラートは抑止）: \(problems.joined(separator: " / "), privacy: .public)")
            return
        }

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
