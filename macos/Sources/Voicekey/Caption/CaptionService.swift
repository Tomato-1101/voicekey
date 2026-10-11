/// 字幕機能の統括
///
/// キャプチャ → 認識 → 翻訳 → HUD 表示 / 読み上げ を1か所で結線し、
/// 現在の状態（停止中・準備中・キャプチャ中・エラー）をメニューバーへ配る。
import AppKit
import Foundation
import OSLog
import os

/// 字幕機能の状態
@available(macOS 26.0, *)
enum CaptionRunState {
    /// 停止中
    case stopped
    /// 準備中（音声認識モデルの確認・ダウンロードなど）
    case preparing(String)
    /// 動作中
    case running
    /// エラーで止まった
    case failed(String)

    /// メニュー先頭に出す 1 行
    var menuTitle: String {
        switch self {
        case .stopped: return "停止中"
        case let .preparing(detail): return "準備中 — \(detail)"
        case .running: return "キャプチャ中"
        case let .failed(message): return "エラー — \(message)"
        }
    }

    /// 動作中扱いか（メニューの「開始/停止」の出し分けに使う）
    var isActive: Bool {
        switch self {
        case .stopped, .failed: return false
        case .preparing, .running: return true
        }
    }
}

/// 字幕機能の起動・停止と各コンポーネントの結線
@available(macOS 26.0, *)
final class CaptionService {

    private let logger = makeCaptionLogger("CaptionService")
    private let hud = CaptionHUDController()
    private let narrator = SpeechNarrator()
    /// 翻訳器は使い回す（Apple 側はセッションとモデルのロードを保持するため作り直さない）
    private let appleTranslator = AppleTranslator()
    private let geminiTranslator = GeminiTranslator()
    private let groqTranslator = GroqTranslator()
    private let mockTranslator = MockTranslator()

    private var pipeline: CapturePipeline?
    private var coordinator: TranslationCoordinator?
    private var partialDriver: PartialTranslationDriver?
    /// Soniox エンジンのときの接続（それ以外は nil）
    private var sonioxStream: SonioxCaptionStream?
    /// 確定文をローカルへ保存する記録係（議事録）
    private let recorder = TranscriptRecorder()

    /// セッションの世代。停止・張り直しの後に遅れて届いた結果を HUD に出さないため
    private let generation = CaptionGenerationGate()
    /// 設定変更による張り直しの予約（停止されたら取り消す）
    private var pendingRestart: DispatchWorkItem?

    /// 動作中セッションを組んだときの設定
    ///
    /// 設定画面の変更は ConfigStore の購読が先に CaptionSettings を書き換えてから setter が呼ばれる。
    /// setter が「保存値と同じなら何もしない」だと、張り直しが要る変更を素通しして
    /// 稼働中の処理に反映されなかった。保存値ではなく、いま動いている構成と比べる。
    private struct SessionConfig: Equatable {
        var mode: CaptionMode
        var localeID: String
        var scope: CaptureScopeMode
        var usesSoniox: Bool
    }
    private var sessionConfig: SessionConfig?

    /// このセッションだけ使うエンジン（Soniox が使えないときの Apple 代替）
    ///
    /// 保存済みの設定は書き換えない（エンジンの切替はユーザー操作だけの恒久要件）。
    /// 翻訳器の解決は翻訳キューから呼ばれるので lock で守る。
    private let sessionEngineOverride = OSAllocatedUnfairLock<TranslationEngine?>(initialState: nil)

    /// 記録に残す対象アプリ名（認識コールバックから読むので lock 付きで持つ）
    ///
    /// `captureTargetName` はメインスレッド専用の表示用。こちらは音声スレッドから読む写し。
    private let recordingTarget = OSAllocatedUnfairLock<String?>(initialState: nil)

    /// クラウドエンジン選択時に、Apple の訳を先に出せるか
    ///
    /// 言語モデルが**既に入っている場合だけ** true にする（ダウンロード承認 UI は絶対に出さない）。
    /// 部分訳ドライバ・翻訳キューから読むので unfair lock で守る。
    private let appleLiveLineReady = OSAllocatedUnfairLock(initialState: false)
    /// 遅延計測（実運用でも .notice で残す）
    private let latency = CaptionLatencyLog()
    /// 字幕動作中の App Nap 抑止トークン
    ///
    /// 常駐アプリが nap に入ると Timer / asyncAfter が沈黙し、字幕行の
    /// 「最低表示時間で自主退場」「無音 8 秒で全体フェード」が動かなくなる実事故がある
    /// （voicekey HANDOFF の App Nap 知見）。字幕を出している間だけ明示的に活動を宣言する。
    private var antiNapActivity: NSObjectProtocol?

    /// 表示順の採番。部分訳と確定訳で共有し、古い結果を捨てるのに使う。
    private let sequenceCounter = OSAllocatedUnfairLock(initialState: 0)
    /// 直近に表示した採番。これより古い結果は表示しない。
    private let displayedSequence = OSAllocatedUnfairLock(initialState: 0)

    /// 状態が変わったときに呼ばれる（メニューバー更新用・メインスレッドで呼ぶ）
    var onStateChange: ((CaptionRunState) -> Void)?

    /// 字幕 HUD の表示/非表示が変わったときに呼ばれる（メインスレッドで呼ぶ）
    ///
    /// 字幕は voicekey の待機ピルと同じ場所に出るので、出ている間はピルを引っ込めてもらう。
    /// ここは HUD からの通知を上位（AppController）へ素通しするだけ。
    var onHUDVisibilityChanged: ((Bool) -> Void)?

    /// 現在の状態
    private(set) var state: CaptionRunState = .stopped {
        didSet { onStateChange?(state) }
    }

    /// 訳文を読み上げるか
    var speaksTranslation: Bool {
        get { CaptionSettings.speakTranslation }
        set {
            CaptionSettings.speakTranslation = newValue
            if !newValue { narrator.stop() }
            logger.notice("読み上げ: \(newValue ? "ON" : "OFF", privacy: .public)")
        }
    }

    /// 最前面のアプリだけを翻訳するか
    ///
    /// 動作中に変えたら張り直す（タップの対象の決め方がセッション単位のため）。
    var capturesFrontmostOnly: Bool {
        get { CaptionSettings.captureScopeMode == .frontmost }
        set {
            CaptionSettings.captureScopeMode = newValue ? .frontmost : .all
            logger.notice("キャプチャ対象: \(newValue ? "最前面のアプリだけ" : "すべてのアプリ", privacy: .public)")
            restartIfSessionOutdated()
        }
    }

    /// いま対象にしているアプリ名（すべてモード・未確定なら nil）
    private(set) var captureTargetName: String?

    /// 原文（英語）を併記するか
    var showsSourceText: Bool {
        get { CaptionSettings.showSourceText }
        set {
            CaptionSettings.showSourceText = newValue
            hud.showsSourceText = newValue
        }
    }

    /// 字幕の大きさ倍率（0.7〜2.0。位置はピル固定なので大きさだけ変えられる）
    var hudScale: CGFloat {
        get { hud.currentScale }
        set { hud.setScale(newValue) }
    }

    /// 字幕の動作モード（翻訳 / 文字起こし）
    ///
    /// モードで認識ロケールが変わる（翻訳は英語固定・文字起こしは選んだ言語）ため、
    /// 動作中に変えたときは認識セッションを張り直す。
    var mode: CaptionMode {
        get { CaptionSettings.mode }
        set {
            CaptionSettings.mode = newValue
            logger.notice("字幕モード: \(newValue.rawValue, privacy: .public)")
            restartIfSessionOutdated()
        }
    }

    /// 文字起こしモードで認識する言語
    ///
    /// 認識器はロケールを指定して起動する仕様なので、切り替えたら張り直す。
    var language: CaptionLanguage {
        get { CaptionSettings.language }
        set {
            CaptionSettings.language = newValue
            logger.notice("認識言語: \(newValue.rawValue, privacy: .public)")
            restartIfSessionOutdated()
        }
    }

    /// 文字起こしをローカルへ保存するか
    var savesTranscript: Bool {
        get { CaptionSettings.savesTranscript }
        set {
            CaptionSettings.savesTranscript = newValue
            logger.notice("議事録の保存: \(newValue ? "ON" : "OFF", privacy: .public)")
        }
    }

    /// 使用する翻訳エンジン
    ///
    /// Apple / Gemini / Groq の間は 1 文ごとに解決しているので、動作中に切り替えても次の文から反映される。
    /// Soniox は音声の流し先ごと変わるので、Soniox に出入りするときだけ張り直す。
    var translationEngine: TranslationEngine {
        get { CaptionSettings.translationEngine }
        set {
            CaptionSettings.translationEngine = newValue
            // ユーザーが選び直したら、Soniox が使えなかったときの代替は解除する（もう一度試す）
            sessionEngineOverride.withLock { $0 = nil }
            logger.notice("翻訳エンジンを切り替えました: \(newValue.rawValue, privacy: .public)")
            if restartIfSessionOutdated() { return }
            // Apple へ切り替えたら、その場でモデルの準備状況を確かめて画面に出す
            if newValue == .apple, state.isActive { prepareAppleTranslationIfNeeded() }
            // クラウドへ切り替えたら、先出し用の Apple 翻訳を裏で用意する
            if state.isActive { prepareAppleLiveLineIfNeeded() }
        }
    }

    init() {
        hud.showsSourceText = CaptionSettings.showSourceText
        hud.onVisibilityChanged = { [weak self] visible in self?.onHUDVisibilityChanged?(visible) }
    }

    /// 字幕を開始する
    func start() {
        guard !state.isActive else { return }
        // 画素判定のハーネス実行中は字幕を出さない（パネルが写り込むと誤判定するため）
        guard !CaptionSettings.isDisabledByEnvironment else {
            logger.notice("VOICEKEY_CAPTION_DISABLE=1 のため字幕を開始しません")
            return
        }
        ActionLog.shared.write("caption.CaptionService", "ライブ字幕を開始 (mode=\(CaptionSettings.mode.rawValue))")
        pendingRestart?.cancel()
        pendingRestart = nil
        sessionEngineOverride.withLock { $0 = nil }
        state = .preparing("音声認識モデルを確認中…")
        // 一度でも開始したら、次回起動からは設定に従って自動開始してよい
        // （初回はシステム音声録音の許可ダイアログが出るので自動開始しない）
        CaptionSettings.hasEverStarted = true
        beginAntiNap()
        startSession()
    }

    /// 字幕を停止する
    func stop() {
        guard state.isActive else { return }
        ActionLog.shared.write("caption.CaptionService", "ライブ字幕を停止")
        pendingRestart?.cancel()
        pendingRestart = nil
        sessionEngineOverride.withLock { $0 = nil }
        teardownSession()
        state = .stopped
        endAntiNap()
    }

    /// 字幕を出している間だけ App Nap を抑止する
    ///
    /// `.background` にしているのは、システムスリープは妨げずアイドル時のタイマー間引きだけ
    /// 止めたいため（voicekey 本体の暖機ループと同じ作法）。
    private func beginAntiNap() {
        guard antiNapActivity == nil else { return }
        antiNapActivity = ProcessInfo.processInfo.beginActivity(
            options: .background, reason: "live caption timers"
        )
    }

    /// App Nap の抑止を解除する
    private func endAntiNap() {
        guard let activity = antiNapActivity else { return }
        ProcessInfo.processInfo.endActivity(activity)
        antiNapActivity = nil
    }

    /// アプリ終了時に確実に後片付けする
    ///
    /// タップと Aggregate Device を残すと HAL 側にゴーストデバイスが溜まるため、
    /// 終了処理は同期的に待つ。
    func shutdown() {
        generation.invalidate()
        pendingRestart?.cancel()
        pendingRestart = nil
        narrator.stop()
        // HUD を捨てる経路でもピルを戻せるようにしておく（畳みアニメの完了通知は届かない）
        onHUDVisibilityChanged?(false)
        endAntiNap()
        let stream = sonioxStream
        sonioxStream = nil
        guard let pipeline else {
            stream?.stop()
            return
        }
        self.pipeline = nil
        let semaphore = DispatchSemaphore(value: 0)
        Task {
            await pipeline.stop()
            stream?.stop()
            semaphore.signal()
        }
        _ = semaphore.wait(timeout: .now() + 3.0)
    }

    /// HUD の大きさを既定に戻す（位置はピル固定なので動かない）
    func resetHUDSize() {
        hud.resetSize()
    }

    /// 音声入力中かを伝える
    ///
    /// 録音・変換中・通知の間は字幕を隠す（「音声入力しているときは字幕は表示されなくていい」
    /// 2026-08-10 ユーザー指示）。認識・翻訳は止めないので、戻れば続きから出る。
    ///
    /// - Parameter active: 音声入力中なら true
    func setDictationActive(_ active: Bool) {
        hud.isSuppressed = active
    }


    // MARK: - 議事録

    /// 議事録の保存先フォルダ
    static var transcriptDirectory: URL { TranscriptRecorder.directory }

    /// 確定文をローカルへ保存する（設定 OFF なら何もしない）
    ///
    /// 世代では縛らない。停止の瞬間に確定した最後の文も議事録には残すため。
    private func saveTranscript(_ text: String) {
        guard CaptionSettings.savesTranscript else { return }
        recorder.record(
            text: text,
            context: recordingTarget.withLock { $0 },
            language: CaptionSettings.recognitionLanguageName
        )
    }

    // MARK: - セッション

    /// いまの設定から組むセッションの構成
    private func desiredSessionConfig() -> SessionConfig {
        SessionConfig(
            mode: CaptionSettings.mode,
            localeID: CaptionSettings.recognitionLocale.identifier,
            scope: CaptionSettings.effectiveCaptureScopeMode,
            usesSoniox: effectiveEngine == .soniox
        )
    }

    /// 動作中の構成が設定とずれていたら張り直す
    ///
    /// - Returns: 張り直したら true
    @discardableResult
    private func restartIfSessionOutdated() -> Bool {
        guard let current = sessionConfig, current != desiredSessionConfig() else { return false }
        restartSession()
        return true
    }

    /// 認識セッションを張り直す
    ///
    /// タップと Aggregate Device の作り直しは Mac 全体の既定デバイス再評価を誘発するので、
    /// 後片付け（非同期）が終わる時間を置いてから開始する。状態は準備中のまま保つ
    /// （一度「停止」を挟むとメニューの表示が揺れ、その間の開始操作が二重起動になるため）。
    private func restartSession() {
        guard state.isActive else { return }
        teardownSession()
        state = .preparing("設定を反映中…")
        pendingRestart?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pendingRestart = nil
            self.startSession()
        }
        pendingRestart = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6, execute: work)
    }

    /// 1 セッションぶんを組んで開始する
    private func startSession() {
        let gen = generation.begin()
        latency.reset()
        let config = desiredSessionConfig()
        sessionConfig = config
        let isTranscribing = config.mode == .transcribe
        // 文字起こしモードでは、ライブ行も確定行と同じ見た目で出す（訳を待たないので本文そのもの）
        hud.liveLooksConfirmed = isTranscribing
        captureTargetName = nil
        recordingTarget.withLock { $0 = nil }

        let pipeline: CapturePipeline
        if config.usesSoniox, let stream = makeSonioxStream(generation: gen, isTranscribing: isTranscribing) {
            // Soniox は認識も翻訳も向こうで行う。端末内の認識は起動せず、音声だけを流す
            pipeline = CapturePipeline(
                locale: CaptionSettings.recognitionLocale,
                recognitionPriority: .userInitiated,
                usesOnDeviceRecognizer: false
            )
            pipeline.onAudio = { buffer in stream.send(buffer) }
            sonioxStream = stream
        } else {
            if config.usesSoniox {
                // キー未設定など。保存済みの設定は変えず、このセッションだけ Apple で出す
                sessionEngineOverride.withLock { $0 = .apple }
                sessionConfig = desiredSessionConfig()
                logger.notice("Soniox の API キーが無いため Apple で字幕を出します")
                hud.showNotice("Soniox の API キーが未設定のため Apple で表示します")
            }
            pipeline = makeOnDevicePipeline(generation: gen, isTranscribing: isTranscribing)
        }
        pipeline.onTargetChanged = { [weak self] name in
            self?.recordingTarget.withLock { $0 = name }
            DispatchQueue.main.async { self?.captureTargetName = name }
        }
        // 対象の再生待ちは画面に出さない（字幕の場所に案内を出すと邪魔。2026-10-12 ユーザー指摘）
        pipeline.onWaitingForAudio = { [weak self] name in
            guard let name else { return }
            self?.logger.notice("\(name, privacy: .public) の音声を待っています")
        }
        pipeline.onFirstAudio = { [weak self] origin in
            self?.latency.setAudioOrigin(origin)
        }
        self.pipeline = pipeline

        Task { [weak self] in
            guard let self else { return }
            do {
                try await pipeline.start()
                guard self.generation.isCurrent(gen) else {
                    // 準備中に停止・張り直しされた。起動を復活させず、張ったタップを片付ける
                    await pipeline.stop()
                    return
                }
                // 遅延の基準点は最初のフレーム到達時に onFirstAudio が入れる
                // （開始から再生までの待ち時間を遅延に混ぜないため）
                await MainActor.run {
                    guard self.generation.isCurrent(gen) else { return }
                    self.state = .running
                    self.prepareAppleTranslationIfNeeded()
                    self.prepareAppleLiveLineIfNeeded()
                }
            } catch {
                guard self.generation.isCurrent(gen) else { return }
                let message = String(describing: error)
                self.logger.error("字幕の開始に失敗: \(message, privacy: .public)")
                await MainActor.run {
                    guard self.generation.isCurrent(gen) else { return }
                    self.teardownSession()
                    self.state = .failed("キャプチャを開始できません")
                    self.endAntiNap()
                }
            }
        }
    }

    /// 端末内の認識（Apple）で動くパイプラインと翻訳経路を組む
    private func makeOnDevicePipeline(generation gen: Int, isTranscribing: Bool) -> CapturePipeline {
        let coordinator = TranslationCoordinator(
            translatorProvider: { [weak self] in self?.currentTranslator() ?? MockTranslator() },
            sequenceProvider: { [weak self] in self?.nextSequence() ?? 0 },
            // クラウドが詰まって溢れた確定文も捨てず、Apple で訳す
            overflowTranslatorProvider: { [weak self] in
                guard let self else { return MockTranslator() }
                return Self.usesMockTranslator ? self.mockTranslator : self.appleTranslator
            },
            refinerProvider: { [weak self] in self?.refineTranslator() }
        )
        coordinator.onSourceReady = { [weak self] source in
            DispatchQueue.main.async {
                guard let self, self.generation.isCurrent(gen) else { return }
                // 訳が届くまでの数十 ms を空白で待たせないよう、ライブ行が空なら原文を出しておく。
                // 確定行はこの後の onTranslated / onFailed が必ず 1 行積む。
                self.hud.showPendingSource(source)
            }
        }
        // ストリーミング対応エンジン（Groq）では、訳し終わる前に届いた分をライブ行へ流す。
        // 確定行はこの後の onTranslated が必ず 1 行積むので、ここは途中経過だけ。
        coordinator.onStreamingDelta = { [weak self] source, japanese, _ in
            DispatchQueue.main.async {
                guard let self, self.generation.isCurrent(gen) else { return }
                self.hud.showTranslation(source: source, japanese: japanese, isPartial: true)
            }
        }
        coordinator.onTranslated = { [weak self] source, japanese, sequence in
            DispatchQueue.main.async {
                guard let self, self.generation.isCurrent(gen) else { return }
                // 確定訳は必ず表示する（順序の判定で捨てない）。
                // 採番の更新だけ行い、この文より古い部分訳が後から届いても出さないようにする。
                _ = self.claimDisplay(sequence)
                self.logger.notice("訳文: \(japanese, privacy: .private)")
                self.hud.showTranslation(source: source, japanese: japanese, sequence: sequence)
                self.latency.noteTranslationShown()
                // 読み上げは確定訳だけ。部分訳を読むと同じ話を何度も喋り続けることになる。
                if self.speaksTranslation { self.narrator.enqueue(japanese) }
            }
        }
        // 先に出した Apple の訳がクラウドの訳で差し替わった（同じ行を書き換える）
        coordinator.onRevised = { [weak self] _, japanese, sequence in
            DispatchQueue.main.async {
                guard let self, self.generation.isCurrent(gen) else { return }
                self.logger.notice("訳文を差し替え: \(japanese, privacy: .private)")
                self.hud.reviseTranslation(sequence: sequence, japanese: japanese)
            }
        }
        coordinator.onFailed = { [weak self] source, message in
            DispatchQueue.main.async {
                guard let self, self.generation.isCurrent(gen) else { return }
                self.logger.notice("翻訳失敗のため原文表示: \(message, privacy: .public)")
                self.hud.showSourceOnly(source)
            }
        }
        self.coordinator = coordinator

        // 部分訳。確定を待つと実測で 4 秒以上遅れるため、途中経過を先に訳して出す。
        // クラウド（Gemini / Groq）へは送らない（Apple が使えるときだけ）。
        let partialDriver = PartialTranslationDriver(
            translatorProvider: { [weak self] in self?.partialTranslator() },
            sequenceProvider: { [weak self] in self?.nextSequence() ?? 0 }
        )
        partialDriver.onPartial = { [weak self] source, japanese, sequence in
            DispatchQueue.main.async {
                guard let self, self.generation.isCurrent(gen) else { return }
                guard self.claimDisplay(sequence) else { return }
                self.hud.showTranslation(source: source, japanese: japanese, isPartial: true)
                self.latency.notePartialShown()
            }
        }
        self.partialDriver = partialDriver

        // 認識ロケールはモードで決まる（翻訳＝英語固定 / 文字起こし＝選んだ言語）。
        // 字幕は速さ重視なので、認識の優先度を上げ、0.3 秒の切れ目で確定を前倒しする。
        let pipeline = CapturePipeline(
            locale: CaptionSettings.recognitionLocale,
            recognitionPriority: .userInitiated,
            finalizesOnPause: true
        )
        pipeline.onSegment = { [weak self] segment in
            guard let self else { return }
            if segment.isFinal {
                self.logger.notice("確定: \(segment.text, privacy: .private)")
                self.latency.noteFinal(audioEndSeconds: segment.range.end.seconds)
                // 保存するのは文字起こしだけ（訳文は保存しない）。停止後に届いた最後の文も残す。
                self.saveTranscript(segment.text)
                // 停止・張り直しの後は訳さない（画面に出ない訳のためにクラウドを呼ばない）
                guard self.generation.isCurrent(gen) else { return }
                // この文の途中経過はもう要らない。投げ済みの部分訳が後から届いても出さない
                partialDriver.reset()
                if isTranscribing {
                    DispatchQueue.main.async {
                        guard self.generation.isCurrent(gen) else { return }
                        self.hud.showTranscript(segment.text)
                    }
                } else {
                    coordinator.submit(segment.text)
                }
            } else {
                guard self.generation.isCurrent(gen) else { return }
                self.latency.noteVolatile(audioEndSeconds: segment.range.end.seconds)
                if isTranscribing {
                    DispatchQueue.main.async {
                        guard self.generation.isCurrent(gen) else { return }
                        self.hud.showLiveTranscript(segment.text)
                    }
                } else {
                    partialDriver.submit(segment.text)
                    DispatchQueue.main.async {
                        guard self.generation.isCurrent(gen) else { return }
                        self.hud.showListening(segment.text)
                    }
                }
            }
        }
        return pipeline
    }

    /// Soniox の接続を用意する（キー未設定などで使えなければ nil）
    private func makeSonioxStream(generation gen: Int, isTranscribing: Bool) -> SonioxCaptionStream? {
        // 翻訳モードは英語固定（訳は英→日だけ）。文字起こしモードは選んだ言語をヒントにする
        let language = isTranscribing ? CaptionSettings.language.rawValue : CaptionLanguage.english.rawValue
        let stream = SonioxCaptionStream(translates: !isTranscribing, language: language)
        stream.onLive = { [weak self] source, translation in
            DispatchQueue.main.async {
                guard let self, self.generation.isCurrent(gen) else { return }
                if isTranscribing {
                    self.hud.showLiveTranscript(source)
                } else if translation.isEmpty {
                    self.hud.showListening(source)
                } else {
                    self.hud.showTranslation(source: source, japanese: translation, isPartial: true)
                }
            }
        }
        stream.onConfirmed = { [weak self] source, translation in
            guard let self else { return }
            // 議事録へは確定した原文を書く（世代では縛らない＝停止時の最後の文も残す）
            if !source.isEmpty { self.saveTranscript(source) }
            DispatchQueue.main.async {
                guard self.generation.isCurrent(gen) else { return }
                self.logger.notice("確定: \(source, privacy: .private) / \(translation, privacy: .private)")
                if isTranscribing {
                    self.hud.showTranscript(source)
                } else if !translation.isEmpty {
                    self.hud.showTranslation(source: source, japanese: translation)
                    if self.speaksTranslation { self.narrator.enqueue(translation) }
                } else if !source.isEmpty {
                    // 訳が届かなかった（訳されない言語・時間切れ）。落とさず原文で出す
                    self.hud.showSourceOnly(source)
                }
            }
        }
        stream.onGiveUp = { [weak self] reason in
            DispatchQueue.main.async {
                guard let self, self.generation.isCurrent(gen) else { return }
                // 保存済みの設定は変えず、このセッションだけ Apple に切り替えて続ける
                self.logger.error("Soniox が使えないため Apple に切り替えます: \(reason, privacy: .public)")
                self.sessionEngineOverride.withLock { $0 = .apple }
                self.restartSession()
                self.hud.showNotice("\(reason) — Apple に切り替えました")
            }
        }
        return stream.start() ? stream : nil
    }

    /// 動作中のセッションを畳む（停止・張り直し共通）
    ///
    /// 先に世代を進めるので、この後に届く認識・翻訳の結果は HUD に出ない。
    /// パイプラインと Soniox は最後まで吐き切らせてから閉じる（最後の確定文を議事録に残すため）。
    private func teardownSession() {
        let ended = generation.invalidate()
        sessionConfig = nil
        let pipeline = self.pipeline
        self.pipeline = nil
        let stream = sonioxStream
        sonioxStream = nil
        coordinator?.reset()
        coordinator = nil
        partialDriver?.reset()
        partialDriver = nil
        logger.notice("遅延サマリ: \(self.latency.summaryLine(), privacy: .public)")
        narrator.stop()
        hud.clear()
        // 止めた拍子に待機ピルが戻らなくなるのを塞ぐ。clear() の畳みアニメ完了でも false は
        // 流れるが、その途中で HUD が解放されると通知が消えるため、ここで確実に流しておく。
        onHUDVisibilityChanged?(false)

        Task { [weak self] in
            await pipeline?.stop()
            // 音声を送り切ってから閉じる（未確定分は 1 行に確定して議事録へ届く）
            stream?.stop()
            // 議事録はここで閉じる（次に開始したときは別ファイルになる）。
            // 張り直しで新しいセッションが既に始まっていたら、そちらの記録を切らない
            guard let self, self.generation.isCurrent(ended) else { return }
            self.recorder.endSession()
        }
    }

    // MARK: - 内部処理

    /// このセッションで実際に使うエンジン
    ///
    /// Soniox を選んでいても使えないときは、このセッションだけ代替（Apple）にする。
    private var effectiveEngine: TranslationEngine {
        let chosen = CaptionSettings.translationEngine
        guard chosen == .soniox, let override = sessionEngineOverride.withLock({ $0 }) else { return chosen }
        return override
    }

    /// 表示順の連番を 1 つ払い出す
    private func nextSequence() -> Int {
        sequenceCounter.withLock { value in
            value += 1
            return value
        }
    }

    /// この採番で表示してよいか判定し、よければ確定させる
    ///
    /// 使うのは**部分訳（ライブ行）だけ**。確定訳は必ず表示するので、確定時は
    /// 戻り値を見ずに採番の更新だけ行う（＝古い部分訳を後追いで出さないための門）。
    private func claimDisplay(_ sequence: Int) -> Bool {
        displayedSequence.withLock { current in
            guard sequence > current else { return false }
            current = sequence
            return true
        }
    }

    /// 部分訳（ライブ行）に使う翻訳器（使えないときは nil）
    ///
    /// **クラウドへは確定文しか送らない**という恒久要件は変えない。そのうえで、
    /// ライブ行の暫定訳だけは **Apple のオンデバイス翻訳**で出す（2026-08-10 ユーザー指摘
    /// 「翻訳されるまで時間がかかりすぎる」への対処）。無料・ローカルなので高頻度に呼んでも
    /// 課金も外部通信も発生せず、恒久要件に抵触しない。確定したらクラウドの訳で置き換わる。
    ///
    /// Apple の言語モデルが入っていない環境では nil を返し、従来どおり原文だけのライブ行にする。
    private func partialTranslator() -> Translator? {
        if Self.usesMockTranslator { return mockTranslator }
        guard effectiveEngine.isCloud else { return appleTranslator }
        return appleLiveLineReady.withLock { $0 } ? appleTranslator : nil
    }

    /// クラウドエンジンのときに、先出し用の Apple 翻訳を裏で用意する
    ///
    /// `prepareIfInstalled()` は**未導入なら何もしない**（ダウンロード承認 UI を出さない）ので、
    /// クラウドを選んでいるユーザーに余計なダイアログや通知を出すことがない。
    private func prepareAppleLiveLineIfNeeded() {
        guard CaptionSettings.mode == .translate else { return }
        guard !Self.usesMockTranslator, effectiveEngine.isCloud else { return }
        guard !appleLiveLineReady.withLock({ $0 }) else { return }
        Task { [weak self] in
            guard let self else { return }
            let ready = await self.appleTranslator.prepareIfInstalled()
            self.appleLiveLineReady.withLock { $0 = ready }
            let description = ready ? "利用可" : "言語モデル未導入のため原文のみ"
            self.logger.notice("ライブ行の暫定訳(Apple): \(description, privacy: .public)")
        }
    }

    /// 確定文を最初に訳す翻訳器
    ///
    /// `VOICEKEY_CAPTION_TRANSLATOR=mock` のときだけモックにする
    /// （外部通信なしで HUD・読み上げ・流量制御の結線を検証するため）。
    /// クラウド選択時は、Apple が使えるなら **Apple の訳を先に出し**、クラウドの訳は
    /// `refineTranslator` で後から差し替える（速さ重視。2026-10-12 ユーザー指示）。
    /// Apple が使えない環境では従来どおりクラウドを Apple へのフォールバックで包む。
    private func currentTranslator() -> Translator {
        if Self.usesMockTranslator { return mockTranslator }
        let appleFirst = appleLiveLineReady.withLock { $0 }
        switch effectiveEngine {
        case .apple, .soniox: return appleTranslator
        case .gemini:
            return appleFirst ? appleTranslator : FallbackTranslator(primary: geminiTranslator, fallback: appleTranslator)
        case .groq:
            return appleFirst ? appleTranslator : FallbackTranslator(primary: groqTranslator, fallback: appleTranslator)
        }
    }

    /// 先に出した Apple の訳を差し替えるクラウドの翻訳器（差し替えないなら nil）
    private func refineTranslator() -> Translator? {
        guard !Self.usesMockTranslator, appleLiveLineReady.withLock({ $0 }) else { return nil }
        switch effectiveEngine {
        case .gemini: return geminiTranslator
        case .groq: return groqTranslator
        case .apple, .soniox: return nil
        }
    }

    /// 現在の構成で実際に翻訳できるか
    ///
    /// Apple エンジンはキー不要なので常に true。クラウドはキーが要る。
    /// Soniox はキーが無ければ Apple で代替するので、字幕としては常に出せる。
    var canTranslate: Bool {
        if Self.usesMockTranslator { return true }
        guard let provider = CaptionSettings.translationEngine.apiProvider else { return true }
        return APIKeyStore.isConfigured(provider)
    }

    /// Apple 翻訳の言語モデルを確認し、必要なら準備を促す
    ///
    /// ダウンロードが要る場合は数十秒〜かかるため、無言で待たせず HUD と状態に出す。
    /// 準備が済んでいる普段の開始では何も出さない（開始のたびの「確認中」通知は邪魔なため撤去）。
    private func prepareAppleTranslationIfNeeded() {
        guard CaptionSettings.mode == .translate else { return }
        guard !Self.usesMockTranslator, effectiveEngine == .apple else { return }
        let gen = generation.current
        Task { [weak self] in
            guard let self else { return }
            let readiness = await self.appleTranslator.prepare { progress in
                // ダウンロード待ちは長いので、待ちに入った時点で画面に出す
                DispatchQueue.main.async {
                    guard self.generation.isCurrent(gen) else { return }
                    self.appleReadiness = progress
                    self.hud.showNotice(progress.message)
                }
            }
            await MainActor.run {
                self.appleReadiness = readiness
                guard self.generation.isCurrent(gen) else { return }
                switch readiness {
                case .ready:
                    // ここで HUD を消すと、準備と並行して出始めた字幕まで消えてしまう
                    self.logger.notice("Apple 翻訳の準備完了")
                case .needsDownload:
                    self.hud.showNotice(readiness.message)
                case .unsupported, .failed:
                    self.logger.error("Apple 翻訳を利用できません: \(readiness.message, privacy: .public)")
                    self.hud.showNotice(readiness.message)
                }
            }
        }
    }

    /// Apple 翻訳の準備状況（メニュー表示用）
    private(set) var appleReadiness: AppleTranslationReadiness = .ready

    /// モック翻訳器が指定されているか（実体は CaptionSettings。判定を二重に持たない）
    static var usesMockTranslator: Bool { CaptionSettings.usesMockTranslator }
}
