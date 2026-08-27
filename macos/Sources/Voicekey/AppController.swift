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

/// 録音がこの秒数を超えたら自動停止する保険
private let kMaxRecordingSec: TimeInterval = 300
/// これより短い録音は誤操作とみなして破棄（秒）
private let kMinAudioSec = 0.3

/// ログ用の秒表記。os_log の補間は @escaping autoclosure なので、
/// MainActor 隔離のメンバーを掴ませないよう free function にしてある
private func secText(_ seconds: TimeInterval) -> String { String(format: "%.2f", seconds) }

/// 録音末尾の指定秒数を切り落とす。ダブルタップの 2 打目を待つあいだ録音を止めない設計なので、
/// 2 打目が来なかったときは待っていたぶんの無音が末尾に付く。それを文字起こしへ送らないため。
/// audio キューの完了ハンドラから呼ばれるので MainActor には触れない。
private func trimTrailing(_ samples: [Float], seconds: TimeInterval) -> [Float] {
    guard seconds > 0 else { return samples }
    let drop = Int(seconds * AudioRecorder.sampleRate)
    guard drop > 0 else { return samples }
    guard drop < samples.count else { return [] }
    return Array(samples.prefix(samples.count - drop))
}

@MainActor
final class AppController: ObservableObject {

    /// 現在のアプリ状態（メニューバーアイコン・HUD が購読）
    @Published private(set) var state: AppState = .idle

    let config = ConfigStore()
    let hud = HudController()
    /// 音声入力履歴（貼り付けたテキストを最大 200 件保持。設定の「履歴」タブで再コピー可）
    let history = HistoryStore()
    /// 使用実績（節約時間・レベル・連続日数。設定の「実績」タブで表示。貼り付け後に集計する）
    let stats = StatsStore()
    /// 前面アプリ（貼り付け先）の追跡。HUD アイコン表示とアプリ別実績の記録に使う
    let frontApp = FrontAppTracker()

    /// ライブ字幕サービスの実体（macOS 26 以降のみ）。
    /// 型を直接持つと Package の最低 OS（macOS 14）でコンパイルできないため AnyObject で保持する。
    /// **最初に `caption` を参照するまで生成しない**（ディクテーションの起動経路に一切足さない）。
    private var captionStorage: AnyObject?

    /// 「翻訳して入力」の翻訳器。Apple のセッションを使い回して 2 回目以降の遅延を消すため保持する。
    /// macOS 26 限定型なので caption と同じく AnyObject で持ち、使うときにキャストする。
    private var translatorStorage: AnyObject?
    /// 保持中の翻訳器の構成（エンジン/出力言語）。変わったら作り直す。
    private var translatorKey = ""

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
    /// ストリーミング録音中のライブセッション（Deepgram / OpenAI ライブ。非ストリーミング時は nil）
    private var streamer: (any LiveTranscribing)?
    /// 録音開始時に確定した不変の処理コンテキスト（finishRecording で処理タスクへ引き継ぐ）
    private var recordContext: RecordContext?
    /// 未完了の文字起こしタスク数
    private var outstanding = 0
    /// 録音の最大時間の保険タイマー
    private var failsafeTask: Task<Void, Never>?
    /// 録音セッションの世代（録音を始めるたびに +1）。
    ///
    /// ライブ字幕やモデル準備の進捗は**離鍵より後に**遅れて届く（ストリーミングの最終確定は
    /// 実測で離鍵の 100ms 前後あと・パイプラインが詰まればさらに遅れる）。すぐ次の録音を
    /// 始めるとその取りこぼしが新しい録音のピルに書き込まれ、「前回の入力の文字が残って
    /// 波形が出ない」状態になる。コールバックに開始時の世代を持たせ、古いものは捨てる。
    private var recordingGeneration = 0

    // --- ダブルタップ検出 ---
    /// 1 打目を離した後、2 打目を待っているあいだの状態。**録音は止めずに続いている**。
    /// nil でなければ「キーは離されたが録音は継続中」を意味する。
    private var pendingDoubleTap: DoubleTapPolicy.Pending?
    /// 2 打目が来なかったときに録音を確定させるタイマー
    private var pendingFinishTask: Task<Void, Never>?
    /// 録音開始時刻（＝押下時刻。ダブルタップ 1 打目のホールド時間の算出に使う）
    private var recordingStartedAt: TimeInterval = 0

    /// 文字起こしパイプラインの直列化チェーン。
    /// 連続録音時も「録音順にテキストが挿入される」ことを保証する
    private var pipelineTail: Task<Void, Never>?

    private var configObservation: Any?

    /// オンボーディングの整形体験ステップ中だけ整形を強制 ON にする一時フラグ（保存しない）。
    /// true の間の録音は、スロットの formatEnabled 設定に関わらず整形を実行する
    /// （フィラー除去を体験させるため）。ステップを出たら必ず false へ戻す。
    var practiceFormatOverride = false

    /// オンボーディングのホットキーテスト中フラグ。true の間はキー押下で録音を始めず、
    /// 押された録音キーのスロットだけを onHotkeyHeldChanged で通知する（無料枠を消費させない）。
    var hotkeyTestActive = false
    /// ホットキーテストで押されている録音キーのスロット（1/2・押されていなければ nil）の変化通知。
    /// オンボーディングの「巨大キー点灯」表示がこのコールバックを登録して購読する。
    var onHotkeyHeldChanged: ((Int?) -> Void)?

    /// マイクテスト（モニタ）中にレベルを受け取るハンドラ。非 nil の間はレベルを HUD ではなく
    /// こちらへ流す（録音とモニタは排他なので、これで出し分けできる）。
    private var micMonitorHandler: ((Float) -> Void)?

    /// マイク／入力監視サブシステムの起動済みフラグ（冪等化＝プロンプトや二重起動を避ける）。
    private var micSubsystemStarted = false
    private var hotkeySubsystemStarted = false

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
                // ピル常時表示トグルの変更を即時反映（待機中なら idle 分岐を再評価する）
                self.hud.alwaysVisible = self.config.hudAlwaysVisible
                if self.state == .idle { self.hud.update(for: .idle) }
                // Dock 常時表示 ON なら即 Dock を出す（OFF はウィンドウを閉じたときに戻る）
                if self.config.dockIconAlwaysVisible {
                    NSApp.setActivationPolicy(.regular)
                }
            }
        }
        hud.enabled = config.hudEnabled
        hud.alwaysVisible = config.hudAlwaysVisible
        // 音声入力中（録音・変換中・通知）はライブ字幕を隠す。字幕を一度も使っていなければ何もしない。
        hud.model.onModeChanged = { [weak self] mode in
            self?.applyCaptionVisibility(for: mode)
        }
        // 起動時に常時表示 ON なら待機ピルを出す（state は初期値 idle のままで変化しないため明示的に呼ぶ）
        if hud.alwaysVisible { hud.update(for: .idle) }
        // デバッグ用: VOICEKEY_HUD_DEBUG_STATE が指定されていれば HUD を固定状態で表示する（検証専用・
        // 指定時は update(for:) が無視され、その状態を出しっぱなしにして見え方を計測できる）
        hud.applyDebugStateIfNeeded()

        // 音声レベル → HUD（audio スレッドから来るためメインへホップ）。
        // マイクテスト（モニタ）中はオンボーディング／ホームのメーターへ流し、HUD には出さない。
        recorder.levelHandler = { [weak self] level in
            DispatchQueue.main.async {
                guard let self else { return }
                if let monitor = self.micMonitorHandler {
                    monitor(level)
                } else {
                    self.hud.pushLevel(level)
                }
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
    /// オンボーディング完了後・既存ユーザー・スキップ時に呼ぶ「全部起動」の入口。
    ///
    /// 権限プロンプトの直列化（重要）: このメソッドはマイク／入力監視サブシステムを起動するが、
    /// **オンボーディング中は呼ばない**。オンボーディングの各権限ステップで個別に許可を取り、
    /// マイクテスト／ホットキーテスト／練習の各ステップで対応サブシステムだけを起動する
    /// （下の startMicSubsystem / startHotkeySubsystem）。これにより「初回起動で権限ポップアップが
    /// 一気に複数出る」事故を防ぐ。ここへ来る時点では各権限は確定済み（付与 or 拒否）のため、
    /// サブシステム起動でプロンプトは出ない。
    /// - Parameter showPermissionAlert: 権限不足時の NSAlert を出すか。オンボーディング完了直後は
    ///   すでに権限を案内済みなので false を渡し、案内が二重に出ないようにする。
    func startup(showPermissionAlert: Bool = true) {
        guard !started else { return }
        started = true
        // 前回録音中に異常終了していた場合のダッキング巻き戻し（残存フラグがあれば元音量へ戻す）
        MediaDucker.restore()
        log.notice("startup(): 権限誘発サブシステムをまとめて起動します")
        Task {
            let micOK = await startMicSubsystem()
            startBackendWarmupIfLoggedIn()
            // 入力監視・アクセシビリティ権限の確認と監視開始
            let tapOK = startHotkeySubsystem()
            checkPermissions(micGranted: micOK, tapCreated: tapOK, showAlert: showPermissionAlert)
        }
    }

    /// マイクサブシステム（権限確認＋入力ユニットのプリウォーム）を起動する。冪等。
    /// マイク許可が確定済みのステップ（マイクテスト・練習・完了）でのみ呼ぶこと。
    /// requestPermission は未決定のときだけダイアログを出す（付与済みなら出ない）ため、
    /// マイク権限ステップより後に呼ぶ限りプロンプトは発火しない。
    /// - Returns: マイクが許可されているか。
    @discardableResult
    func startMicSubsystem() async -> Bool {
        let micOK = await AudioRecorder.requestPermission()
        if micOK {
            if !micSubsystemStarted {
                micSubsystemStarted = true
                // 初回録音の開始遅延を減らすため入力ユニットを温めておく
                recorder.prewarm()
                log.notice("マイクサブシステムを起動しました（プリウォーム）")
            }
        } else {
            log.error("マイクの使用が許可されていません")
        }
        return micOK
    }

    /// 入力監視サブシステム（ホットキーのイベントタップ）を起動する。冪等。
    /// 入力監視許可が確定済みのステップ（ホットキーテスト・練習・完了）でのみ呼ぶこと。
    /// tapCreate は未許可でも失敗してログを出すだけでプロンプトは出さない（明示の許可要求は
    /// オンボーディングの入力監視ステップの CGRequestListenEventAccess が担う）。
    /// - Returns: タップ作成に成功したか。
    @discardableResult
    func startHotkeySubsystem() -> Bool {
        if hotkeySubsystemStarted { return true }
        let ok = hotkeys.start()
        if ok {
            hotkeySubsystemStarted = true
            log.notice("入力監視サブシステム（ホットキー監視）を起動しました")
        }
        return ok
    }

    /// 製品版（ログイン済み）なら、サーバー接続の暖機を開始する（トークン先読み・暖機ループ・
    /// App Nap 抑止・スリープ復帰ハンドラ）。権限には触れないため任意のタイミングで呼べる。冪等
    /// （warmTimer/antiNapActivity は再セットしても実害はないが、二重登録を避けるため guard する）。
    private func startBackendWarmupIfLoggedIn() {
        guard BackendClient.isLoggedIn else { return }
        // 起動時にサーバー接続を 1 回だけ暖機して初回のサーバー往復（serverless cold start を
        // 含む最大数秒）を前払いする。有料・無料とも再利用可トークンを先取りしてキャッシュへ載せる
        // （消費ゼロ・往復ゼロで話し始められる）。失敗は無視（録音時に通常経路で再取得される）。
        Task { await self.warmBackendsNow() }
        if warmTimer == nil {
            startEphemeralWarmLoop()  // 以後も数分間隔でプロキシ関数を温存（cold start 回避）
        }
        // 放置中も暖機ループが確実に回るよう App Nap を抑止する。
        // .background＝システムスリープは妨げず、アイドル時のタイマー間引き（nap）だけ止める。
        if antiNapActivity == nil {
            antiNapActivity = ProcessInfo.processInfo.beginActivity(
                options: .background,
                reason: "keep auth session and transcription token warm"
            )
            // スリープ復帰直後は serverless 関数が冷え・短命トークンも失効済み＝「放置後の初回録音が
            // 遅い」の主因。復帰イベントで即・暖機してから話し始めの待ちを消す。
            NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
            ) { [weak self] _ in
                Task { @MainActor in await self?.warmBackendsNow() }
            }
        }
    }

    // MARK: - オンボーディング用（マイクテスト・ホットキーテスト・練習エンジン）

    /// 練習ステップ用: 録音に必要なサブシステム（マイク＋入力監視＋バックエンド暖機）を起動する。
    /// 各サブシステムは個別・冪等に起動するため、マイクテスト／ホットキーテストで既に起動済みなら
    /// 何もしない。権限は前段のステップで確定済みのためプロンプトは出ない。
    func startEngineForPractice() {
        Task {
            await startMicSubsystem()
            startBackendWarmupIfLoggedIn()
            startHotkeySubsystem()
        }
    }

    /// マイクテスト: 録音せずレベルだけを onLevel へ流すモニタリングを開始する。
    /// マイク許可が確定済みのステップでのみ呼ぶこと（内部で startMicSubsystem を通す）。
    func startMicMonitor(_ onLevel: @escaping (Float) -> Void) {
        micMonitorHandler = onLevel
        // 選択中の入力デバイスをモニタにも反映する（録音開始時と同じ扱い）
        recorder.inputDeviceUID = config.inputDeviceUID
        Task {
            let micOK = await startMicSubsystem()
            guard micOK else { return }
            recorder.startMonitoring { _ in }
        }
    }

    /// マイクテストのモニタリングを停止する。
    func stopMicMonitor() {
        recorder.stopMonitoring()
        micMonitorHandler = nil
    }

    /// マイクテストの入力デバイスを切り替える（モニタ中なら止めて選択デバイスで再開する）。
    /// config へ保存もするため、以後の録音でも選択が使われる。
    func setMicTestDevice(_ uid: String, monitoring: Bool) {
        config.inputDeviceUID = uid
        recorder.inputDeviceUID = uid
        guard monitoring else { return }
        recorder.stopMonitoring { [weak self] in
            self?.recorder.startMonitoring { _ in }
        }
    }

    /// オンボーディング終了時に、テスト用の一時状態（マイクモニタ・ホットキーテスト・整形オーバーライド）を
    /// すべて解除する。× クローズ・完了のどちらでも呼ぶ（冪等）。
    func teardownOnboardingTestModes() {
        stopMicMonitor()
        hotkeyTestActive = false
        onHotkeyHeldChanged = nil
        practiceFormatOverride = false
    }

    /// ライブ字幕サービス（初回アクセス時に生成する）
    ///
    /// システム音声のキャプチャ・翻訳・字幕 HUD 一式を持つ。**参照するまで何も作らない**ので、
    /// 字幕を使わない限り録音〜貼り付けのクリティカルパスには一切乗らない。
    @available(macOS 26.0, *)
    var caption: CaptionService {
        if let existing = captionStorage as? CaptionService { return existing }
        let created = CaptionService()
        captionStorage = created
        // 字幕が出ている間は待機ピルを引っ込める。字幕はピルの真上でピルから育つ設計なので、
        // 残したままだと半透明の字幕越しにピルが透け、字幕とピルが別物に見える（2026-08-12 修正）。
        // 通常はメインスレッドから来るので同期で反映する（字幕の伸縮と同じ実行ターンで
        // ピルを出し入れしないと、順序が入れ替わってピルが出たままになりうる）。
        // ただし停止経路は CoreAudio のコールバック側から呼ばれうるため、そのときだけ
        // main へ回す。assumeIsolated をメイン以外で直に踏むとその場で落ちる。
        created.onHUDVisibilityChanged = { [weak self] visible in
            guard Thread.isMainThread else {
                DispatchQueue.main.async { [weak self] in
                    MainActor.assumeIsolated { self?.hud.captionCovering = visible }
                }
                return
            }
            MainActor.assumeIsolated { self?.hud.captionCovering = visible }
        }
        // 録音中に字幕を開始したときも隠れた状態から始まるよう、今の HUD 状態を反映しておく
        applyCaptionVisibility(for: hud.model.mode)
        log.notice("ライブ字幕サービスを初期化しました")
        return created
    }

    /// ライブ字幕が既に生成されているか（終了処理で「作っていなければ触らない」ために使う）
    var hasCaptionService: Bool { captionStorage != nil }

    /// HUD の表示状態に合わせてライブ字幕を隠す／戻す
    ///
    /// 音声入力（録音・変換中・通知）の間は字幕を出さない（2026-08-10 ユーザー指示）。
    /// 字幕サービスを**作っていなければ何もしない**（遅延生成を壊さないため）。
    ///
    /// - Parameter mode: HUD の新しい表示状態
    private func applyCaptionVisibility(for mode: HudModel.Mode) {
        guard #available(macOS 26.0, *), let caption = captionStorage as? CaptionService else { return }
        let dictating: Bool
        switch mode {
        case .recording, .transcribing, .notice: dictating = true
        case .hidden, .idlePill: dictating = false
        }
        caption.setDictationActive(dictating)
    }

    func shutdown() {
        // 字幕を動かしていたらタップと Aggregate Device を確実に片付ける
        // （残すと HAL 側にゴーストデバイスが溜まる）
        if #available(macOS 26.0, *), let caption = captionStorage as? CaptionService {
            caption.shutdown()
        }
        // 字幕を畳んだので待機ピルの退避も解く（ピルが出せない状態で残らないように）
        hud.captionCovering = false
        hotkeys.stop()
        pendingFinishTask?.cancel()
        pendingFinishTask = nil
        pendingDoubleTap = nil
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

    /// 使用中プロバイダのサーバー関数を温め、短命トークンを先読みする（消費ゼロ）。
    /// 起動時・録音後・暖機ループ・スリープ復帰直後に共通で呼ぶ。
    /// トークンは残 TTL が warmMinRemaining(360s) 未満なら強制再取得する＝暖機間隔 240s では満了の
    /// 120 秒以上前に必ず差し替わり、アイドル中も常に手元に有効トークンがある（録音開始ゼロ待ち）。
    /// 以前は「残 15 秒超なら取得しない」短絡で満了直前まで更新されず、満了〜次 tick に周期コールド窓が
    /// 生じていた（token_grants が毎時 1 回しか並ばない実測で確認）＝それを根治した。
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
            // 残 TTL が warmMinRemaining(360s) 未満なら強制再取得＝満了 120 秒以上前に必ず更新して
            // 「満了〜次 tick」の周期コールド窓（録音がトークン往復＋WS 接続を待つ）を消す。
            let warm = await BackendClient.warmEphemeralToken(minRemaining: BackendClient.warmMinRemaining)
            let remain = warm.remaining.map { Int($0) } ?? -1
            log.notice("warm tick: token \(warm.fetched ? "fetched" : "skip", privacy: .public) remain=\(remain, privacy: .public)s")
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
        // ホットキーテスト中は録音を始めず、押された録音キーのスロットだけを点灯通知する
        // （録音→文字起こしで無料枠を消費させないため）。
        if hotkeyTestActive {
            for slotId in [1, 2] {
                let slot = config.slot(slotId)
                if !slot.hotkey.isEmpty, slotMatches(slot, pressed: pressed) {
                    onHotkeyHeldChanged?(slotId)
                    return
                }
            }
            return
        }

        // 1 打目を離した後、録音を止めずに 2 打目を待っている状態。
        // 下の「録音中」分岐より先に見る（recordingSlot は入ったままなので、
        // ここで拾わないと 2 打目が「録音中の再押下」に食われて無視される）。
        if let pending = pendingDoubleTap {
            let handsfree = !config.handsfreeKey.isEmpty && handsfreeKeyPressed(pressed)
            let hitSlot = [1, 2].first { slotId in
                let slot = config.slot(slotId)
                return !slot.hotkey.isEmpty && slotMatches(slot, pressed: pressed)
            }
            let now = ProcessInfo.processInfo.systemUptime
            let window = DoubleTapPolicy.gapWindow(doubleClickInterval: NSEvent.doubleClickInterval)
            let decision: DoubleTapPolicy.Decision
            if let hitSlot, !handsfree {
                decision = DoubleTapPolicy.decide(
                    pending: pending, slotId: hitSlot, pressedAt: now, gapWindow: window)
            } else {
                // ハンズフリー起動や無関係なキーはダブルタップにしない
                decision = .otherSlot
            }
            logDoubleTap(decision, pending: pending, pressedAt: now, window: window)
            if decision.isDoubleTap {
                acceptDoubleTap()
                return
            }
            // 2 打目ではなかった。1 打目を単発の録音として確定してから通常処理へ進む
            resolvePendingAsSingleTap()
        }

        if let slotId = recordingSlot {
            let slot = config.slot(slotId)
            // toggle 実効モード: 録音中の再押下で停止（ハンズフリー切替キー併用での起動も含む）
            if recordingEffectiveMode == .toggle, slotMatches(slot, pressed: pressed) {
                finishRecording()
                return
            }
            // hold 実効モードのダブルタップは上の保留分岐で処理済み。ここに来るのは
            // 押しっぱなし中の別キー押下など（録音は続行させる）。
            return
        }

        // 再貼り付けショートカット（録音中でない・キー設定あり・全キー押下）。
        // 録音キー判定より先に見て、押されていれば録音を始めず直近テキストを貼り付ける。
        if !config.repasteKey.isEmpty, repasteKeyPressed(pressed) {
            repasteLast()
            return
        }

        for slotId in [1, 2] {
            let slot = config.slot(slotId)
            guard !slot.hotkey.isEmpty, slotMatches(slot, pressed: pressed) else { continue }
            // ハンズフリー切替キーが併用されていれば、このスロットを toggle 実効で起動する
            // （切替キーは空でなく全キーが押されていること。スロット設定が hold でも toggle になる）
            let handsfree = !config.handsfreeKey.isEmpty && handsfreeKeyPressed(pressed)
            let effectiveMode: HotkeyMode = handsfree ? .toggle : slot.mode
            // ダブルタップ（auto_enter）はここでは決めない。1 打目の離鍵で「保留」に入り、
            // 2 打目の押下で **同じ録音を auto_enter に切り替える**方式にしたため。
            // 録音を作り直さないので、1 打目に入った声が消えず、録音の開始タイミングも
            // auto_enter かどうかで変わらない（どちらも最初の押下が開始点）。
            beginRecording(slotId: slotId, autoEnter: false, effectiveMode: effectiveMode)
            break
        }
    }

    private func handleRelease(token: String, pressed: Set<String>) {
        // ホットキーテスト中: どの録音キーも押されなくなったら消灯（録音はしない）。
        if hotkeyTestActive {
            let held = [1, 2].first { slotId in
                let slot = config.slot(slotId)
                return !slot.hotkey.isEmpty && slotMatches(slot, pressed: pressed)
            }
            onHotkeyHeldChanged?(held)
            return
        }

        guard let slotId = recordingSlot else { return }
        let slot = config.slot(slotId)
        // toggle 実効モード（ハンズフリー起動含む）は離鍵で止めない（再押下で停止）
        guard recordingEffectiveMode == .hold else { return }

        // 離されたキーがホットキーの構成キーなら録音停止
        let isHotkeyKey = slot.hotkey.contains { required in
            KeyToken.acceptableNames(for: required).contains(token)
        }
        if isHotkeyKey {
            // ダブルタップ確定後（autoEnter）の離鍵はもう待たない。この録音で確定する。
            if autoEnter {
                finishRecording()
                return
            }
            let now = ProcessInfo.processInfo.systemUptime
            let pending = DoubleTapPolicy.Pending(
                slotId: slotId, pressedAt: recordingStartedAt, releasedAt: now)
            guard DoubleTapPolicy.isTapCandidate(hold: pending.hold) else {
                // 口述の長さ。ダブルタップの 1 打目にはしない（長く喋った直後に素早く
                // 押し直しただけで Enter が自動送信される事故を防ぐ）。そのまま確定する。
                log.notice("""
                    ダブルタップ判定 slot=\(slotId, privacy: .public) \
                    hold=\(secText(pending.hold), privacy: .public)s \
                    hold上限=\(secText(kTapHoldMax), privacy: .public)s \
                    結果=不成立(hold超過)
                    """)
                finishRecording()
                return
            }
            // タップ相当の短さ。**録音は止めずに**2 打目を待つ。ここで止めると 2 打目で
            // 録り直しになり、1 打目に入った声が捨てられるうえ、auto_enter のときだけ
            // 録音の開始点が 2 打目までずれる（どちらもユーザー指摘）。
            beginPendingDoubleTap(pending)
        }
    }

    /// 1 打目の離鍵。録音は続けたまま 2 打目を待ち、来なければ単発として確定する。
    private func beginPendingDoubleTap(_ pending: DoubleTapPolicy.Pending) {
        pendingDoubleTap = pending
        let window = DoubleTapPolicy.gapWindow(doubleClickInterval: NSEvent.doubleClickInterval)
        pendingFinishTask?.cancel()
        pendingFinishTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(window))
            guard let self, !Task.isCancelled, self.pendingDoubleTap == pending else { return }
            self.logDoubleTap(
                .noSecondTap, pending: pending,
                pressedAt: pending.releasedAt + window, window: window)
            self.resolvePendingAsSingleTap()
        }
    }

    /// 2 打目が来た。**録音は作り直さず**、いま録っているものを auto_enter に切り替えるだけ。
    private func acceptDoubleTap() {
        pendingFinishTask?.cancel()
        pendingFinishTask = nil
        pendingDoubleTap = nil
        autoEnter = true
        // HUD は録音中のまま auto_enter 表示に変わる（作り直しの明滅が起きない）
        emitState()
    }

    /// 2 打目が来なかった（または無関係なキーが来た）。1 打目を単発の録音として確定する。
    /// 待っていたあいだに伸びた末尾は切り落とす（その無音を文字起こしへ送らないため）。
    private func resolvePendingAsSingleTap() {
        guard let pending = pendingDoubleTap else { return }
        pendingFinishTask?.cancel()
        pendingFinishTask = nil
        pendingDoubleTap = nil
        let extra = ProcessInfo.processInfo.systemUptime - pending.releasedAt
        finishRecording(quietIfNoSpeech: true, trimTrailingSec: extra)
    }

    /// ダブルタップ判定の実測値をそのまま残す。
    /// 「たまに検出されない」を次回は推測でなく数値で追うための記録なので、
    /// **必ず .notice**（info は永続化されず `log show` から追えない）。
    /// 出すのは数値と結果だけ（キー名や本文は出さない）。
    private func logDoubleTap(
        _ decision: DoubleTapPolicy.Decision,
        pending: DoubleTapPolicy.Pending,
        pressedAt: TimeInterval,
        window: TimeInterval
    ) {
        log.notice("""
            ダブルタップ判定 slot=\(pending.slotId, privacy: .public) \
            hold=\(secText(pending.hold), privacy: .public)s \
            gap=\(secText(pressedAt - pending.releasedAt), privacy: .public)s \
            hold上限=\(secText(kTapHoldMax), privacy: .public)s \
            窓=\(secText(window), privacy: .public)s \
            結果=\(decision.logLabel, privacy: .public)
            """)
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

    /// 再貼り付けキーがすべて押されているか（汎用修飾キーは左右どちらでも可）
    private func repasteKeyPressed(_ pressed: Set<String>) -> Bool {
        config.repasteKey.allSatisfy { required in
            !KeyToken.acceptableNames(for: required).isDisjoint(with: pressed)
        }
    }

    /// 履歴の先頭テキストをもう一度貼り付ける（履歴が空なら何もしない）。
    /// 録音中はこの経路に来ない（handlePress の録音中分岐で return 済み）。
    private func repasteLast() {
        guard let last = history.items.first else { return }
        log.info("再貼り付けショートカット: 直近テキストを貼り付け (\(last.text.count) 文字)")
        Task { await Paster.paste(last.text) }
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
        /// 整形プリセット（standard/punctuation/clean/bullets）。サーバー統合整形・
        /// クライアント整形のどちらの経路にも渡し、整形の「削り方」を切り替える。
        let formatPresetId: String
        /// サーバー統合整形（Groq プロキシで STT と整形を 1 リクエストに）を使えるか。
        /// groq スロット × 整形 ON × ログイン済み × ハンズフリー EL 差し替えでない、が条件。
        /// 単発送信のときだけこれを見て serverFormat=true にし、後段のクライアント整形をスキップする。
        let serverFormatEligible: Bool
    }

    private func beginRecording(slotId: Int, autoEnter: Bool, effectiveMode: HotkeyMode = .hold) {
        guard recordingSlot == nil else { return }
        // 操作音・メディアダッキングを撃ちっぱなしで開始（音声パイプラインは一切待たせない）
        if config.soundEffectsEnabled { SoundFX.shared.play(.start) }
        if config.duckMediaEnabled { MediaDucker.duck() }
        // 貼り付け先アプリのアイコンを HUD に載せる（録音開始時点のスナップショット。emitState 前に設定）
        hud.setAppIcon(frontApp.currentIcon())
        recordingSlot = slotId
        // この録音の世代。ライブ系のコールバックはこれを捕まえて、前の録音のぶんを捨てる
        recordingGeneration &+= 1
        let generation = recordingGeneration
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

        // オンボーディング体験（整形ステップ）中はスロット設定に関わらず整形を強制 ON にする。
        let effectiveFormatEnabled = practiceFormatOverride || slot.formatEnabled

        // サーバー統合整形（Groq プロキシで STT と整形を 1 リクエストに）が使えるか。
        // 条件: activeTranscriber が groq（＝ハンズフリー EL 差し替えなら backend が .elevenlabs に
        // なるので自動的に除外される）× 整形 ON × ログイン済み。単発送信のときだけ発動する。
        let serverFormatEligible = activeTranscriber?.backend == .groq
            && effectiveFormatEnabled
            && BackendClient.isLoggedIn

        // 録音開始時点の transcriber（プロバイダー）と処理フラグを不変スナップショット化する。
        // 設定 hot-reload やスロット変更が録音中〜処理開始に挟まっても開始時の設定で完遂する
        recordContext = RecordContext(
            transcriber: activeTranscriber,
            vadEnabled: config.vadEnabled,
            splitEnabled: config.splitParallelEnabled,
            autoEnterDelayMs: config.autoEnterDelayMs,
            formatEnabled: effectiveFormatEnabled,
            formatPrompt: config.autoFormatPrompt,
            formatModel: config.formatModel,
            formatPresetId: slot.formatPresetId,
            serverFormatEligible: serverFormatEligible
        )

        // 設定された入力デバイスを反映（変更がなければ recorder 側では何もしない）
        recorder.inputDeviceUID = config.inputDeviceUID

        // ライブ系バックエンド（Deepgram / OpenAI ライブ）かつストリーミング有効なら
        // WebSocket を開いて低遅延で確定テキストを得る。
        // 開始できなければ（キー無し等）chunkHandler を張らないため REST 経路に自動フォールバック。
        // chunkHandler は録音開始前に張る必要がある（最初のチャンクを取りこぼさない）
        if config.streamingEnabled, let stream = Self.makeLiveTranscriber(
            backend: slot.backend, model: slot.model, language: config.language) {
            // personal（個人用最速版）: ストリーミングの暫定（interim）テキストを HUD に
            // ライブ字幕として逐次表示する（喋りながら見せる）。onInterim は URLSession の
            // 受信キューから来るためメインへホップする。確定入力（貼付）は従来どおり。
            if EmbeddedKeys.isPersonal {
                // 数字正規化の設定を録音開始時にスナップショット（確定入力と同じ規則・保護リストを
                // 字幕にも効かせる）。captured な不変値なので受信キューから直接読んでも thread-safe。
                let numEnabled = config.numeralNormalizeEnabled
                let numCounter = config.numeralConvertCounter
                let numProtect = Set(config.numeralProtectWords)
                stream.onInterim = { [weak self] text in
                    // ライブ字幕にも確定入力と同じ数字正規化（漢数字/全角→半角アラビア）をかけて、
                    // 「喋っている最中は漢数字→確定で半角に変わる」チラつき（不一致）を無くす。
                    // NumeralNormalizer は純関数なので受信キューから直接呼んで描画直前に一枚噛ませる。
                    let normalized = NumeralNormalizer.normalize(
                        text, enabled: numEnabled, convertCounter: numCounter, protectWords: numProtect)
                    DispatchQueue.main.async {
                        // 前の録音の遅れて届いた更新は捨てる（新しいピルに前回の文字を残さない）
                        guard let self, self.recordingGeneration == generation else { return }
                        self.hud.setLiveText(normalized)
                    }
                }
            }
            // ローカル（Apple）は初回だけ言語モデルのダウンロードが走る。無言で固まって
            // 見えないよう進捗を出す（導入済みなら一度も呼ばれない）。
            // **通知（notice）ではなくピルの中に出す**。通知はピルを丸ごと置き換えるので、
            // 進捗が来るたびに録音表示が消えてしまう（ユーザー指摘の再発源）。
            if #available(macOS 26.0, *), let local = stream as? LocalSpeechTranscriber {
                local.onAssetProgress = { [weak self] message in
                    DispatchQueue.main.async {
                        guard let self, self.recordingGeneration == generation else { return }
                        self.hud.setLiveText(message)
                    }
                }
            }
            if stream.start() {
                streamer = stream
                recorder.chunkHandler = { [weak stream] chunk in stream?.send(chunk) }
            }
        }

        // マイク起動を最優先で仕掛ける（プリウォーム類は後ろに置き、
        // メインスレッドの Keychain 読みなどで録音開始を遅らせない）
        recorder.start { [weak self] failure in
            guard let failure else { return }
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
                self.hud.notice(failure.noticeText)
            }
        }

        // 録音中に TLS 接続を事前確立して、停止後の初回 API 往復を短縮（切替時は EL 側を温める）
        activeTranscriber?.prewarm()
        // 整形が有効なら整形 LLM への接続も録音中に温めておく
        if effectiveFormatEnabled {
            formatter.prewarm()
        }
        // 「翻訳して入力」が ON なら翻訳セッションも録音中に温める（初回のモデルロードを隠す）。
        // OFF のときは何も生成しない＝他バックエンドのクリティカルパスに一切足さない。
        if DictationTranslation.isEnabled, #available(macOS 26.0, *) {
            let translator = dictationTranslator()
            Task { await translator.prepare() }
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

    // MARK: - 翻訳して入力

    /// 「翻訳して入力」が ON なら最終テキストを訳す（OFF なら経路に一切触れない）。
    ///
    /// 訳すのは貼り付け直前の 1 回だけ。失敗したら原文をそのまま返す（テキストを失わない）。
    ///
    /// - Parameter text: 整形・数字正規化・ユーザー辞書まで済んだ最終テキスト
    /// - Returns: 貼り付けるテキスト
    private func translateIfEnabled(_ text: String) async -> String {
        guard DictationTranslation.isEnabled, !text.isEmpty else { return text }
        guard #available(macOS 26.0, *) else { return text }

        let translator = dictationTranslator()
        let started = ProcessInfo.processInfo.systemUptime
        let result = await translator.translate(text)
        let elapsedMs = Int((ProcessInfo.processInfo.systemUptime - started) * 1000)
        if result.didTranslate {
            log.info("翻訳して入力: \(DictationTranslation.engine.rawValue, privacy: .public) → \(DictationTranslation.targetLanguage, privacy: .public) \(elapsedMs)ms")
        } else {
            // 無言で原文が入ると「翻訳が効いていない」ことに気付けないので必ず知らせる
            log.notice("翻訳して入力に失敗したため原文を入力します: \(result.failureReason ?? "不明", privacy: .public)")
            hud.notice("翻訳できなかったため原文を入力しました")
        }
        return result.text
    }

    /// 現在の設定に合う翻訳器を返す（構成が変わらない限り使い回す）
    @available(macOS 26.0, *)
    private func dictationTranslator() -> DictationTranslator {
        let engine = DictationTranslation.engine
        let target = DictationTranslation.targetLanguage
        let source = config.language
        let key = "\(engine.rawValue)/\(source)/\(target)"
        if key == translatorKey, let existing = translatorStorage as? DictationTranslator {
            return existing
        }
        let created = DictationTranslator(
            engine: engine, sourceLanguage: source, targetLanguage: target
        )
        translatorStorage = created
        translatorKey = key
        return created
    }

    /// ライブ（WebSocket）文字起こしのセッションをバックエンドに応じて生成する。
    /// ライブ非対応のバックエンド（groq / elevenlabs / openai）は nil＝REST 経路で処理する。
    /// 生成しただけでは接続しない（呼び出し側が start() の成否でフォールバックを判断する）。
    private static func makeLiveTranscriber(
        backend: Backend, model: String, language: String
    ) -> (any LiveTranscribing)? {
        switch backend {
        case .deepgram:
            return StreamingTranscriber(model: model, language: language)
        case .openaiLive:
            return OpenAILiveTranscriber(model: model, language: language)
        case .appleLocal:
            // オンデバイス（SpeechAnalyzer）。macOS 26 未満では選択肢に出ないが、
            // 旧 OS に保存値が残っていた場合は nil＝REST 経路でエラー文言を出す。
            guard #available(macOS 26.0, *) else { return nil }
            return LocalSpeechTranscriber(language: language)
        case .groq, .elevenlabs, .openai:
            return nil
        }
    }

    private func finishRecording(quietIfNoSpeech: Bool = false, trimTrailingSec: TimeInterval = 0) {
        guard recordingSlot != nil else { return }
        // 保留（2 打目待ち）から抜ける。保険タイマーや toggle 経由でここに来たときも
        // 保留を残さない（残すと次の押下が「2 打目」として扱われる）
        pendingFinishTask?.cancel()
        pendingFinishTask = nil
        pendingDoubleTap = nil
        // 停止の操作音・メディア音量の復元（撃ちっぱなし）。
        // restore は無条件で呼ぶ（録音中に設定を OFF にしても、下げた音量を確実に戻すため）。
        if config.soundEffectsEnabled { SoundFX.shared.play(.stop) }
        MediaDucker.restore()
        let useAutoEnter = autoEnter
        // ストリーミング送信を打ち切り、確定待ちはパイプライン側で行う
        let activeStreamer = streamer
        let context = recordContext  // 録音開始時に確定した処理コンテキスト
        streamer = nil
        recordContext = nil
        recorder.chunkHandler = nil
        recordingSlot = nil
        autoEnter = false
        failsafeTask?.cancel()
        outstanding += 1
        emitState()

        recorder.stop { [weak self] samples in
            // audio キューから呼ばれる。メインへホップしてタスク起動
            let kept = trimTrailing(samples, seconds: trimTrailingSec)
            DispatchQueue.main.async {
                self?.processAudio(kept, context: context,
                                   autoEnter: useAutoEnter, streamer: activeStreamer,
                                   quietIfNoSpeech: quietIfNoSpeech)
            }
        }
    }

    // MARK: - 文字起こしパイプライン

    private func processAudio(
        _ samples: [Float], context: RecordContext?, autoEnter: Bool,
        streamer: (any LiveTranscribing)?, quietIfNoSpeech: Bool = false
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
        let formatPresetId = context.formatPresetId
        let serverFormatEligible = context.serverFormatEligible

        // 直前のタスク完了を待ってから処理する（録音順のテキスト挿入を保証）
        let previous = pipelineTail
        pipelineTail = Task {
            await previous?.value
            defer { taskFinished() }

            // --- ストリーミング経路: ライブ（Deepgram / OpenAI ライブ）の確定テキストを受け取って貼り付け ---
            if let streamer {
                let streamed = await streamer.finish()
                if !streamed.isEmpty {
                    // 整形が有効なら貼り付け前に LLM で整形（失敗時は原文が返る）
                    let formatted = formatEnabled
                        ? await formatter.format(streamed, prompt: formatPrompt, model: formatModel, presetId: formatPresetId)
                        : streamed
                    // 数字表記を半角アラビア数字へ正規化してからユーザー辞書置換を適用
                    // （全角→半角・連続漢数字→算用数字。置換はその後＝正規化後テキストに効かせる）
                    let corrected = config.applyReplacements(NumeralNormalizer.normalize(
                        formatted,
                        enabled: config.numeralNormalizeEnabled,
                        convertCounter: config.numeralConvertCounter,
                        protectWords: Set(config.numeralProtectWords)
                    ))
                    // 「翻訳して入力」が ON なら、この最終テキストを 1 回だけ訳す
                    // （正規化・ユーザー辞書は原文の誤認識を直すためのものなので、翻訳より前に効かせる）
                    let output = await self.translateIfEnabled(corrected)
                    // 貼り付け先アプリを貼り付け直前に再取得する（録音中に切り替えた場合は今の前面が正）
                    let target = frontApp.snapshot()
                    // 貼り付けに失敗しても履歴から救出できるよう、貼り付け前に記録する（履歴保存 OFF はスキップ）
                    if config.historyEnabled {
                        history.add(output, appBundleID: target.bundleID, appName: target.name)
                    }
                    // 実績を集計（貼り付け後のローカル処理なので遅延に影響しない）
                    stats.recordSession(
                        characters: output.count,
                        recordingSeconds: Double(samples.count) / AudioRecorder.sampleRate,
                        appBundleID: target.bundleID, appName: target.name
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
            // 即時入力=Deepgram はストリーミング経路（上）を通り、ここは普通入力=Groq プロキシ・
            // ハンズフリー=EL プロキシ、およびストリーミング確定が空だった時のフォールバックが通る。
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
                    serverFormat: serverFormatEligible, presetId: formatPresetId
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
                formatted = await formatter.format(text, prompt: formatPrompt, model: formatModel, presetId: formatPresetId)
            } else {
                formatted = text
            }
            let fmtMs = Int((ProcessInfo.processInfo.systemUptime - fmtStart) * 1000)
            // 数字表記を半角アラビア数字へ正規化してからユーザー辞書置換を適用
            // （全角→半角・連続漢数字→算用数字。置換はその後＝正規化後テキストに効かせる）
            let corrected = config.applyReplacements(NumeralNormalizer.normalize(
                formatted,
                enabled: config.numeralNormalizeEnabled,
                convertCounter: config.numeralConvertCounter,
                protectWords: Set(config.numeralProtectWords)
            ))
            // 「翻訳して入力」が ON なら、この最終テキストを 1 回だけ訳す（OFF なら素通り）
            let output = await self.translateIfEnabled(corrected)
            // 貼り付け先アプリを貼り付け直前に再取得する（録音中に切り替えた場合は今の前面が正）
            let target = frontApp.snapshot()
            // 貼り付けに失敗しても履歴から救出できるよう、貼り付け前に記録する（履歴保存 OFF はスキップ）
            if config.historyEnabled {
                history.add(output, appBundleID: target.bundleID, appName: target.name)
            }
            // 実績を集計（貼り付け後のローカル処理なので遅延に影響しない）
            stats.recordSession(characters: output.count, recordingSeconds: duration,
                                appBundleID: target.bundleID, appName: target.name)
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
        _ samples: [Float], transcriber: Transcriber, splitEnabled: Bool, serverFormat: Bool,
        presetId: String = "standard"
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
        return (try await transcriber.transcribe(samples: samples, serverFormat: serverFormat, presetId: presetId), serverFormat)
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
        // warm ループと同じ 360s 閾値で「満了のはるか手前でだけ差し替える」先読み扱いにする。
        if BackendClient.isLoggedIn, config.streamingEnabled,
           config.slot(1).backend == .deepgram || config.slot(2).backend == .deepgram {
            Task { _ = try? await BackendClient.fetchEphemeralToken(minRemaining: BackendClient.warmMinRemaining) }
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
