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

    /// クラウドエンジン選択時に、ライブ行の暫定訳を Apple で出せるか
    ///
    /// 言語モデルが**既に入っている場合だけ** true にする（ダウンロード承認 UI は絶対に出さない）。
    /// 部分訳ドライバの queue から読むので unfair lock で守る。
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

    /// 現在の状態
    private(set) var state: CaptionRunState = .stopped {
        didSet {
            // 動作中だけ HUD のホバー操作バーを有効にする（止まっているときに出しても押す物が無い）
            hud.hoverControlsEnabled = state.isActive
            onStateChange?(state)
        }
    }

    /// HUD のホバー操作バーから「設定」が押されたときのメニュー表示
    var onHUDMenuRequested: ((NSView, NSPoint) -> Void)? {
        get { hud.onMenuRequested }
        set { hud.onMenuRequested = newValue }
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
    /// 切り替えは次に字幕を開始したときから効く（動作中に張り替えると認識が途切れるため）。
    var capturesFrontmostOnly: Bool {
        get { CaptionSettings.captureScopeMode == .frontmost }
        set {
            CaptionSettings.captureScopeMode = newValue ? .frontmost : .all
            logger.notice("キャプチャ対象: \(newValue ? "最前面のアプリだけ" : "すべてのアプリ", privacy: .public)")
        }
    }

    /// いま対象にしているアプリ名（すべてモード・未確定なら nil）
    private(set) var captureTargetName: String?

    /// 対象の再生開始を待つ案内を HUD に出しているか（消すときの判定に使う）
    private var isWaitingForAudio = false

    /// 原文（英語）を併記するか
    var showsSourceText: Bool {
        get { CaptionSettings.showSourceText }
        set {
            CaptionSettings.showSourceText = newValue
            hud.showsSourceText = newValue
        }
    }

    /// 使用する翻訳エンジン
    ///
    /// 翻訳器は 1 文ごとに解決しているため、動作中に切り替えても次の文から反映される。
    var translationEngine: TranslationEngine {
        get { CaptionSettings.translationEngine }
        set {
            guard newValue != CaptionSettings.translationEngine else { return }
            CaptionSettings.translationEngine = newValue
            logger.notice("翻訳エンジンを切り替えました: \(newValue.rawValue, privacy: .public)")
            // Apple へ切り替えたら、その場でモデルの準備状況を確かめて画面に出す
            if newValue == .apple, state.isActive { prepareAppleTranslationIfNeeded() }
            // クラウドへ切り替えたら、ライブ行用の Apple 翻訳を裏で用意する
            if state.isActive { prepareAppleLiveLineIfNeeded() }
        }
    }

    init() {
        hud.showsSourceText = CaptionSettings.showSourceText
        hud.onStopRequested = { [weak self] in self?.stop() }
    }

    /// 字幕を開始する
    func start() {
        guard !state.isActive else { return }
        // 画素判定のハーネス実行中は字幕を出さない（パネルが写り込むと誤判定するため）
        guard !CaptionSettings.isDisabledByEnvironment else {
            logger.notice("VOICEKEY_CAPTION_DISABLE=1 のため字幕を開始しません")
            return
        }
        state = .preparing("音声認識モデルを確認中…")
        // 一度でも開始したら、次回起動からは設定に従って自動開始してよい
        // （初回はシステム音声録音の許可ダイアログが出るので自動開始しない）
        CaptionSettings.hasEverStarted = true
        beginAntiNap()

        latency.reset()
        let coordinator = TranslationCoordinator(
            translatorProvider: { [weak self] in self?.currentTranslator() ?? MockTranslator() },
            sequenceProvider: { [weak self] in self?.nextSequence() ?? 0 }
        )
        coordinator.onSourceReady = { [weak self] source in
            DispatchQueue.main.async {
                // 訳が届くまでの数十 ms を空白で待たせないよう、ライブ行に原文を出しておく。
                // 確定行はこの後の onTranslated / onFailed が必ず 1 行積む。
                self?.hud.showPendingSource(source)
            }
        }
        // ストリーミング対応エンジン（Groq）では、訳し終わる前に届いた分をライブ行へ流す。
        // 確定行はこの後の onTranslated が必ず 1 行積むので、ここは途中経過だけ。
        coordinator.onStreamingDelta = { [weak self] source, japanese, _ in
            DispatchQueue.main.async {
                self?.hud.showTranslation(source: source, japanese: japanese, isPartial: true)
            }
        }
        coordinator.onTranslated = { [weak self] source, japanese, sequence in
            DispatchQueue.main.async {
                guard let self else { return }
                // 確定訳は必ず表示する（順序の判定で捨てない）。
                // 採番の更新だけ行い、この文より古い部分訳が後から届いても出さないようにする。
                _ = self.claimDisplay(sequence)
                self.logger.notice("訳文: \(japanese, privacy: .public)")
                self.hud.showTranslation(source: source, japanese: japanese)
                self.latency.noteTranslationShown()
                // 読み上げは確定訳だけ。部分訳を読むと同じ話を何度も喋り続けることになる。
                if self.speaksTranslation { self.narrator.enqueue(japanese) }
            }
        }
        coordinator.onFailed = { [weak self] source, message in
            DispatchQueue.main.async {
                guard let self else { return }
                self.logger.notice("翻訳失敗のため原文表示: \(message, privacy: .public)")
                self.hud.showSourceOnly(source)
            }
        }
        self.coordinator = coordinator

        // 部分訳。確定を待つと実測で 4 秒以上遅れるため、途中経過を先に訳して出す。
        // クラウド（Gemini）では高頻度呼び出しになるので無効にする。
        let partialDriver = PartialTranslationDriver(
            translatorProvider: { [weak self] in self?.partialTranslator() },
            sequenceProvider: { [weak self] in self?.nextSequence() ?? 0 }
        )
        partialDriver.onPartial = { [weak self] source, japanese, sequence in
            DispatchQueue.main.async {
                guard let self else { return }
                guard self.claimDisplay(sequence) else { return }
                self.hud.showTranslation(source: source, japanese: japanese, isPartial: true)
                self.latency.notePartialShown()
            }
        }
        self.partialDriver = partialDriver

        captureTargetName = nil
        let pipeline = CapturePipeline()
        pipeline.onTargetChanged = { [weak self] name in
            DispatchQueue.main.async { self?.captureTargetName = name }
        }
        // 対象の再生開始を待っている間は、無言で固まって見えないよう HUD に出す
        pipeline.onWaitingForAudio = { [weak self] name in
            DispatchQueue.main.async {
                guard let self else { return }
                if let name {
                    self.isWaitingForAudio = true
                    self.hud.showNotice("\(name) の音声を待っています — 再生を開始してください")
                } else if self.isWaitingForAudio {
                    self.isWaitingForAudio = false
                    self.hud.clear()
                }
            }
        }
        pipeline.onFirstAudio = { [weak self] origin in
            self?.latency.setAudioOrigin(origin)
        }
        pipeline.onSegment = { [weak self] segment in
            guard let self else { return }
            if segment.isFinal {
                self.logger.notice("英文（確定）: \(segment.text, privacy: .public)")
                self.latency.noteFinal(audioEndSeconds: segment.range.end.seconds)
                coordinator.submit(segment.text)
            } else {
                self.latency.noteVolatile(audioEndSeconds: segment.range.end.seconds)
                partialDriver.submit(segment.text)
                DispatchQueue.main.async { self.hud.showListening(segment.text) }
            }
        }
        self.pipeline = pipeline

        Task { [weak self] in
            guard let self else { return }
            do {
                try await pipeline.start()
                // 遅延の基準点は最初のフレーム到達時に onFirstAudio が入れる
                // （開始から再生までの待ち時間を遅延に混ぜないため）
                await MainActor.run { self.state = .running }
                self.prepareAppleTranslationIfNeeded()
                self.prepareAppleLiveLineIfNeeded()
            } catch {
                let message = String(describing: error)
                self.logger.error("字幕の開始に失敗: \(message, privacy: .public)")
                await MainActor.run {
                    self.state = .failed("キャプチャを開始できません")
                    self.pipeline = nil
                }
            }
        }
    }

    /// 字幕を停止する
    func stop() {
        guard state.isActive else { return }
        let pipeline = self.pipeline
        self.pipeline = nil
        coordinator?.flush()
        coordinator?.reset()
        coordinator = nil
        partialDriver?.reset()
        partialDriver = nil
        logger.notice("遅延サマリ: \(self.latency.summaryLine(), privacy: .public)")
        narrator.stop()
        hud.clear()
        state = .stopped
        endAntiNap()

        Task {
            await pipeline?.stop()
        }
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
        narrator.stop()
        endAntiNap()
        guard let pipeline else { return }
        self.pipeline = nil
        let semaphore = DispatchSemaphore(value: 0)
        Task {
            await pipeline.stop()
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


    // MARK: - 内部処理

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
        guard CaptionSettings.translationEngine.isCloud else { return appleTranslator }
        return appleLiveLineReady.withLock { $0 } ? appleTranslator : nil
    }

    /// クラウドエンジンのときに、ライブ行用の Apple 翻訳を裏で用意する
    ///
    /// `prepareIfInstalled()` は**未導入なら何もしない**（ダウンロード承認 UI を出さない）ので、
    /// クラウドを選んでいるユーザーに余計なダイアログや通知を出すことがない。
    private func prepareAppleLiveLineIfNeeded() {
        guard !Self.usesMockTranslator, CaptionSettings.translationEngine.isCloud else { return }
        guard !appleLiveLineReady.withLock({ $0 }) else { return }
        Task { [weak self] in
            guard let self else { return }
            let ready = await self.appleTranslator.prepareIfInstalled()
            self.appleLiveLineReady.withLock { $0 = ready }
            let description = ready ? "利用可" : "言語モデル未導入のため原文のみ"
            self.logger.notice("ライブ行の暫定訳(Apple): \(description, privacy: .public)")
        }
    }

    /// いま使う翻訳器を返す
    ///
    /// `VOICEKEY_CAPTION_TRANSLATOR=mock` のときだけモックにする
    /// （外部通信なしで HUD・読み上げ・流量制御の結線を検証するため）。
    /// クラウドのエンジンは Apple へのフォールバックで包む（429・瞬断で字幕を止めないため）。
    private func currentTranslator() -> Translator {
        if Self.usesMockTranslator { return mockTranslator }
        switch CaptionSettings.translationEngine {
        case .apple: return appleTranslator
        case .gemini: return FallbackTranslator(primary: geminiTranslator, fallback: appleTranslator)
        case .groq: return FallbackTranslator(primary: groqTranslator, fallback: appleTranslator)
        }
    }

    /// 現在の構成で実際に翻訳できるか
    ///
    /// Apple エンジンはキー不要なので常に true。クラウドはキーが要る。
    var canTranslate: Bool {
        if Self.usesMockTranslator { return true }
        guard let provider = CaptionSettings.translationEngine.apiProvider else { return true }
        return APIKeyStore.isConfigured(provider)
    }

    /// Apple 翻訳の言語モデルを確認し、必要なら準備を促す
    ///
    /// ダウンロードが要る場合は数十秒〜かかるため、無言で待たせず HUD と状態に出す。
    private func prepareAppleTranslationIfNeeded() {
        guard !Self.usesMockTranslator, CaptionSettings.translationEngine == .apple else { return }
        Task { [weak self] in
            guard let self else { return }
            await MainActor.run {
                self.hud.showNotice("英→日の翻訳モデルを確認中…")
            }
            let readiness = await self.appleTranslator.prepare { progress in
                // ダウンロード待ちは長いので、待ちに入った時点で画面に出す
                DispatchQueue.main.async {
                    self.appleReadiness = progress
                    self.hud.showNotice(progress.message)
                }
            }
            await MainActor.run {
                switch readiness {
                case .ready:
                    self.logger.notice("Apple 翻訳の準備完了")
                    self.hud.clear()
                case .needsDownload:
                    self.hud.showNotice(readiness.message)
                case .unsupported, .failed:
                    self.logger.error("Apple 翻訳を利用できません: \(readiness.message, privacy: .public)")
                    self.hud.showNotice(readiness.message)
                }
                self.appleReadiness = readiness
            }
        }
    }

    /// Apple 翻訳の準備状況（メニュー表示用）
    private(set) var appleReadiness: AppleTranslationReadiness = .ready

    /// モック翻訳器が指定されているか（実体は CaptionSettings。判定を二重に持たない）
    static var usesMockTranslator: Bool { CaptionSettings.usesMockTranslator }
}
