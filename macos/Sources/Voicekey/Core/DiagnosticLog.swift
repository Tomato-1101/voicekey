//
//  DiagnosticLog.swift
//  不具合調査用の詳細ログ（行動ログ ActionLog に永続化する）の組み立て部品
//
//  「入力されるまで遅い」「たまに入らない」「表示がおかしい」を、再現できなくても
//  ログだけで追えるようにするための部品を集めている。
//  - DictationTimeline: 1 回の音声入力を 1 行の `[計測]` サマリにまとめる（全経路で必ず 1 行）
//  - LiveSessionLog: ライブ接続（Soniox / OpenAI ライブ / Deepgram）1 セッション分の計測
//  - SettingsChangeLog: 設定変更の差分（プロンプト・URL は長さだけ）
//  - HotkeyEventClock: CGEvent のタイムスタンプ → systemUptime の換算
//
//  方針（ActionLog と同じ）: 話した内容（文字起こし・整形後・翻訳文）・プロンプト本文・API キーは
//  一切出さない。出すのは文字数・時間・種別だけ。行の組み立ては純関数にしてテストで固定する。
//  時刻はすべて ProcessInfo.systemUptime（単調時計）で持つ（壁時計の補正で差分が狂わないように）。
//

import Foundation

/// ログ用の文字列整形（純関数）
enum DiagnosticText {
    /// 長すぎる値を切り詰める（1 行を短く保ち、想定外に長いエラー本文で日次ログを膨らませない）
    static func clip(_ text: String, _ limit: Int = 120) -> String {
        let oneLine = text.replacingOccurrences(of: "\n", with: " ")
        guard oneLine.count > limit else { return oneLine }
        return String(oneLine.prefix(limit)) + "…"
    }

    /// 2 時刻の差を「Xms」で返す。どちらかが無ければ "-"
    static func ms(from start: TimeInterval?, to end: TimeInterval?) -> String {
        guard let start, let end else { return "-" }
        return "\(Int(((end - start) * 1000).rounded()))ms"
    }

    /// ミリ秒値を「Xms」で返す。無ければ "-"
    static func ms(_ value: Int?) -> String {
        guard let value else { return "-" }
        return "\(value)ms"
    }

    /// 世代番号。0 は「録音に紐付かない」（起動時・Soniox 再送など）なので "-" にする
    static func gen(_ generation: Int) -> String {
        generation > 0 ? "\(generation)" : "-"
    }
}

// MARK: - 1 回の音声入力のタイムライン

/// 1 回の音声入力（押下〜貼り付け or スキップ）の各時点を記録し、`[計測]` 1 行にまとめる。
///
/// どの経路（ライブ / REST / ローカル / Soniox 再送 / フォールバック）で終わっても、
/// スキップ（短すぎ・無音・空結果・失敗・打ち切り）でも必ず 1 行出すことで、
/// 「入らなかった 1 回」をログから取りこぼさない。
struct DictationTimeline {
    /// 結果の表記（ログの検索キーになるので固定文言にしておく）
    enum Outcome {
        static let pasted = "貼付"
        /// ⌘V を送れなかった（結果はクリップボードに残してある）
        static let pasteFailed = "貼付失敗"
        static let tooShort = "短すぎ"
        static let noSpeech = "無音"
        static let emptyResult = "空結果"
        static let liveEmptyCompleted = "ライブ空(正常終了)"
        static let transcribeFailed = "文字起こし失敗"
        static let recordStartFailed = "録音開始失敗"
        static let recordStartTimeout = "録音開始タイムアウト"
        static let rejectedStalled = "録音拒否(オーディオキュー復帰待ち)"
        static let abandoned = "打ち切り"
        static let abandonedTimeout = "打ち切り(タイムアウト)"
        static let noContext = "コンテキストなし"
        static let unfinished = "未完了"
    }

    let generation: Int
    let slot: Int
    let backend: String
    let model: String
    /// 押下時刻（CGEvent の時刻が取れればそれ、無ければ押下処理の時刻）
    let pressedAt: TimeInterval

    var route: String?
    var ending: String?
    var outcome: String = Outcome.unfinished
    var recordStartedAt: TimeInterval?
    var firstAudioAt: TimeInterval?
    var recordingSec: Double?
    /// 離鍵（toggle は停止の再押下、保険停止は停止処理）の時刻
    var releasedAt: TimeInterval?
    /// 実際に録音を止めた時刻（短いタップは 2 打目待ちのぶん離鍵より後になる）
    var stoppedAt: TimeInterval?
    /// 確定テキストが手元に揃った時刻（ライブ=finish 完了 / REST=応答受信）
    var finalizedAt: TimeInterval?
    var sttMs: Int?
    var vadMs: Int?
    var formatMs: Int?
    var translateMs: Int?
    var pasteMs: Int?
    var pastedAt: TimeInterval?
    var characters: Int?
    var targetBundleID: String?
    var autoEnter = false

    init(generation: Int, slot: Int, backend: String, model: String, pressedAt: TimeInterval) {
        self.generation = generation
        self.slot = slot
        self.backend = backend
        self.model = model
        self.pressedAt = pressedAt
    }

    /// `[計測]` サマリ 1 行（純関数）。キーの順序は固定（grep・集計スクリプトが位置に頼れるように）
    func summaryLine() -> String {
        let recording = recordingSec.map { String(format: "%.2fs", $0) } ?? "-"
        let fields: [String] = [
            "gen=\(DiagnosticText.gen(generation))",
            "slot=\(slot)",
            "backend=\(backend)",
            "model=\(model.isEmpty ? "-" : model)",
            "経路=\(route ?? "-")",
            "終わり方=\(ending ?? "-")",
            "結果=\(outcome)",
            "押下→録音開始=\(DiagnosticText.ms(from: pressedAt, to: recordStartedAt))",
            "押下→最初の音声=\(DiagnosticText.ms(from: pressedAt, to: firstAudioAt))",
            "録音=\(recording)",
            "離鍵→確定=\(DiagnosticText.ms(from: releasedAt, to: finalizedAt))",
            "離鍵→停止=\(DiagnosticText.ms(from: releasedAt, to: stoppedAt))",
            "停止→確定=\(DiagnosticText.ms(from: stoppedAt, to: finalizedAt))",
            "STT=\(DiagnosticText.ms(sttMs))",
            "VAD=\(DiagnosticText.ms(vadMs))",
            "整形=\(DiagnosticText.ms(formatMs))",
            "翻訳=\(DiagnosticText.ms(translateMs))",
            "貼付=\(DiagnosticText.ms(pasteMs))",
            "離鍵→貼付完了=\(DiagnosticText.ms(from: releasedAt, to: pastedAt))",
            "押下→貼付完了=\(DiagnosticText.ms(from: pressedAt, to: pastedAt))",
            "文字数=\(characters.map(String.init) ?? "-")",
            "貼付先=\(targetBundleID ?? "-")",
            "auto_enter=\(autoEnter)",
        ]
        return "[計測] " + fields.joined(separator: " ")
    }

    /// 文字起こしの経路を分類する（純関数）。
    /// - Parameters:
    ///   - backend: 録音開始時のバックエンド
    ///   - hadStreamer: ライブ接続を張れていたか
    ///   - streamedText: ライブの確定が空でなかったか（ライブ経路で終わったか）
    static func route(backend: Backend, hadStreamer: Bool, streamedText: Bool) -> String {
        if hadStreamer {
            if streamedText { return backend == .appleLocal ? "local" : "streaming" }
            // ライブが空で REST へ回った。Soniox は録音全体の再送、それ以外は REST へのフォールバック
            return backend == .soniox ? "replay" : "fallback"
        }
        switch backend {
        case .soniox: return "replay"
        case .appleLocal: return "local"
        default: return "rest"
        }
    }
}

// MARK: - ライブ接続 1 セッションの計測

/// ライブ（WebSocket）文字起こし 1 セッション分の計測を集め、行動ログへ出す。
///
/// 書き込むのは「失敗したとき」と「終わったとき（サマリ 1 行）」だけ。チャンク送信ごとに
/// 行を出すと日次ログが膨れるので、送信は最初の 1 回の時刻と失敗件数だけを持つ。
/// 送受信のコールバック（URLSession のキュー）と audio スレッドの両方から触られるため lock で守る。
final class LiveSessionLog: @unchecked Sendable {
    let provider: String
    private let lock = NSLock()
    private let createdAt = ProcessInfo.processInfo.systemUptime
    private var _generation = 0
    private var connectedAt: TimeInterval?
    private var firstAudioSentAt: TimeInterval?
    private var firstResultAt: TimeInterval?
    private var endRequestedAt: TimeInterval?
    private var sendErrors = 0
    private var _endingLabel: String?
    /// 結果を使わずに破棄した（cancel()）。自分で切った接続の -999 を切断失敗として残さないため
    private var cancelled = false

    init(provider: String) {
        self.provider = provider
    }

    /// この接続が属する録音世代（AppController が start() の前に入れる。再送などは 0 のまま）
    var generation: Int {
        get { lock.lock(); defer { lock.unlock() }; return _generation }
        set { lock.lock(); _generation = newValue; lock.unlock() }
    }

    /// 終わり方（ログ用表記）。未解決なら nil
    var endingLabel: String? {
        lock.lock(); defer { lock.unlock() }
        return _endingLabel
    }

    /// 送信が初めて成功した＝ WebSocket のハンドシェイクが済んだ（delegate を持たない実装なので
    /// 最初の送信完了を接続確立とみなす）
    func markConnected() {
        lock.lock()
        if connectedAt == nil { connectedAt = ProcessInfo.processInfo.systemUptime }
        lock.unlock()
    }

    /// 音声チャンクを初めて WebSocket へ送った（以降は何もしない＝チャンクごとのコストは lock 1 回だけ）
    func markAudioSent() {
        lock.lock()
        if firstAudioSentAt == nil { firstAudioSentAt = ProcessInfo.processInfo.systemUptime }
        lock.unlock()
    }

    /// 最初の暫定/確定テキストが届いた
    func markFirstResult() {
        lock.lock()
        if firstResultAt == nil { firstResultAt = ProcessInfo.processInfo.systemUptime }
        lock.unlock()
    }

    /// 終了（finish）を要求した
    func markEndRequested() {
        lock.lock()
        if endRequestedAt == nil { endRequestedAt = ProcessInfo.processInfo.systemUptime }
        lock.unlock()
    }

    /// 結果を使わずに破棄する（各実装の cancel() が接続を切る前に呼ぶ）。
    /// 切った直後の受信失敗（-999）が resolve より先に届いても、終わり方を cancelled として残すため
    func markCancelled() {
        lock.lock()
        cancelled = true
        lock.unlock()
    }

    /// 送信失敗。最初の 1 件だけ内容を残し、以降は件数だけ数える（終了サマリに件数を出す）
    func sendFailed(_ error: Error, what: String) {
        lock.lock()
        sendErrors += 1
        let first = sendErrors == 1
        let resolved = _endingLabel != nil
        let gen = _generation
        lock.unlock()
        // 終わった後（自分で閉じた後）の送信失敗は想定内なので残さない
        guard first, !resolved else { return }
        ActionLog.shared.write(
            "live",
            "[ライブ] gen=\(DiagnosticText.gen(gen)) provider=\(provider) 送信エラー(初回) 対象=\(what) "
                + Self.describe(error)
        )
    }

    /// 受信失敗（切断）。エラーの domain / code / 説明と、分かれば close code を残す。
    /// 既に終わったセッション（finish 後に自分で閉じた）の失敗は想定内なので残さない。
    func receiveFailed(_ error: Error, task: URLSessionWebSocketTask?) {
        // -999（NSURLErrorCancelled）は自分で task を切った結果であって切断失敗ではない
        let nsError = error as NSError
        let isCancelError = nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled
        lock.lock()
        if isCancelError { cancelled = true }
        let skip = _endingLabel != nil || cancelled
        let gen = _generation
        let elapsed = ProcessInfo.processInfo.systemUptime - createdAt
        lock.unlock()
        guard !skip else { return }
        var close = "-"
        if let task, task.closeCode != .invalid {
            close = "\(task.closeCode.rawValue)"
            if let reason = task.closeReason, let text = String(data: reason, encoding: .utf8), !text.isEmpty {
                close += "(\(DiagnosticText.clip(text, 80)))"
            }
        }
        ActionLog.shared.write(
            "live",
            "[ライブ] gen=\(DiagnosticText.gen(gen)) provider=\(provider) 受信エラー "
                + "開始から=\(Int((elapsed * 1000).rounded()))ms \(Self.describe(error)) close=\(close)"
        )
    }

    /// 任意の補足（Soniox のリージョン判定など）
    func note(_ message: String) {
        ActionLog.shared.write("live", "[ライブ] gen=\(DiagnosticText.gen(generation)) provider=\(provider) \(message)")
    }

    /// 終わり方が決まった（最初の 1 回だけ有効）。ここでセッションのサマリを 1 行出す
    func resolve(_ label: String) {
        let now = ProcessInfo.processInfo.systemUptime
        lock.lock()
        guard _endingLabel == nil else { lock.unlock(); return }
        // 破棄・-999 の後の解決は、受信失敗経由で disconnect と渡されても cancelled として残す（ログだけ。動作上の ending は各実装のまま）
        let finalLabel = cancelled ? "cancelled" : label
        _endingLabel = finalLabel
        let line = Self.summaryLine(
            generation: _generation, provider: provider, createdAt: createdAt,
            connectedAt: connectedAt, firstAudioSentAt: firstAudioSentAt,
            firstResultAt: firstResultAt, endRequestedAt: endRequestedAt,
            resolvedAt: now, ending: finalLabel, sendErrors: sendErrors)
        lock.unlock()
        ActionLog.shared.write("live", line)
    }

    /// セッションサマリ 1 行（純関数・テスト対象）。時間はすべてセッション生成（≒押下）からの差
    static func summaryLine(
        generation: Int, provider: String, createdAt: TimeInterval,
        connectedAt: TimeInterval?, firstAudioSentAt: TimeInterval?, firstResultAt: TimeInterval?,
        endRequestedAt: TimeInterval?, resolvedAt: TimeInterval, ending: String, sendErrors: Int
    ) -> String {
        "[ライブ] gen=\(DiagnosticText.gen(generation)) provider=\(provider) "
            + "接続確立=\(DiagnosticText.ms(from: createdAt, to: connectedAt)) "
            + "最初の音声送信=\(DiagnosticText.ms(from: createdAt, to: firstAudioSentAt)) "
            + "最初の結果=\(DiagnosticText.ms(from: createdAt, to: firstResultAt)) "
            + "終了要求→確定=\(DiagnosticText.ms(from: endRequestedAt, to: endRequestedAt == nil ? nil : resolvedAt)) "
            + "終わり方=\(ending) 送信エラー=\(sendErrors)"
    }

    /// Deepgram / OpenAI ライブの resolveFinish の理由文字列を、LiveEnding.logLabel と同じ表記へ寄せる
    /// （純関数・テスト対象）。動作上の `ending` は変えず、ログの終わり方だけを区別するためのもの。
    static func endingLabel(forReason reason: String) -> String {
        switch reason {
        case "metadata", "completed": return "completed"
        case "timeout": return "failed:timeout"
        case "disconnect": return "failed:disconnect"
        case "error": return "failed:error"
        default: return reason
        }
    }

    /// エラーを「domain/code 説明」の短い表記にする
    static func describe(_ error: Error) -> String {
        let ns = error as NSError
        return "error=\(ns.domain)/\(ns.code) \(DiagnosticText.clip(ns.localizedDescription, 100))"
    }
}

// MARK: - 設定変更の差分

/// 設定変更をキー名（＋新しい値）で残すための写し。
/// プロンプト・同期 URL は本文を出さず長さだけ、ユーザー辞書・保護語は件数だけにする
/// （話した内容に準じる個人的な語やトークン入り URL をログに残さないため）。
enum SettingsChangeLog {
    /// ログに出してよい形へ写した設定（キー → 値の表記）
    @MainActor
    static func snapshot(_ c: ConfigStore) -> [String: String] {
        var s: [String: String] = [:]
        for (name, slot) in [("slot1", c.slot1), ("slot2", c.slot2)] {
            s["\(name).hotkey"] = slot.hotkey.joined(separator: "+")
            s["\(name).mode"] = slot.mode.rawValue
            s["\(name).backend"] = slot.backend.rawValue
            s["\(name).model"] = slot.model
            s["\(name).prompt"] = "長さ\(slot.prompt.count)"
            s["\(name).formatEnabled"] = "\(slot.formatEnabled)"
            s["\(name).formatPresetId"] = slot.formatPresetId
        }
        s["language"] = c.language
        s["hudEnabled"] = "\(c.hudEnabled)"
        s["streamingEnabled"] = "\(c.streamingEnabled)"
        s["autoEnterDelayMs"] = "\(c.autoEnterDelayMs)"
        s["inputDeviceUID"] = c.inputDeviceUID.isEmpty ? "既定" : c.inputDeviceUID
        s["formatModel"] = c.formatModel
        s["autoFormatPrompt"] = "長さ\(c.autoFormatPrompt.count)"
        s["handsfreeKey"] = c.handsfreeKey.joined(separator: "+")
        s["replacements"] = "件数\(c.replacements.count)"
        s["soundEffectsEnabled"] = "\(c.soundEffectsEnabled)"
        s["duckMediaEnabled"] = "\(c.duckMediaEnabled)"
        s["repasteKey"] = c.repasteKey.joined(separator: "+")
        s["hudAlwaysVisible"] = "\(c.hudAlwaysVisible)"
        s["dockIconAlwaysVisible"] = "\(c.dockIconAlwaysVisible)"
        s["sideNotchEnabled"] = "\(c.sideNotchEnabled)"
        s["historyEnabled"] = "\(c.historyEnabled)"
        s["historySyncEnabled"] = "\(c.historySyncEnabled)"
        s["historySyncURL"] = "長さ\(c.historySyncURL.count)"
        s["numeralNormalizeEnabled"] = "\(c.numeralNormalizeEnabled)"
        s["numeralConvertCounter"] = "\(c.numeralConvertCounter)"
        s["numeralProtectWords"] = "件数\(c.numeralProtectWords.count)"
        s["translateInputEnabled"] = "\(c.translateInputEnabled)"
        s["translateInputTarget"] = c.translateInputTarget
        s["translateInputEngine"] = c.translateInputEngine.rawValue
        return s
    }

    /// 変わったキーだけを「key=新しい値」でキー名順に並べる。変化が無ければ nil（純関数・テスト対象）
    static func diff(old: [String: String], new: [String: String]) -> String? {
        let keys = Set(old.keys).union(new.keys).filter { old[$0] != new[$0] }.sorted()
        guard !keys.isEmpty else { return nil }
        return keys.map { "\($0)=\(DiagnosticText.clip(new[$0] ?? "(削除)", 60))" }.joined(separator: " ")
    }

    /// スロット 1 つ分の要約（起動時の環境ログ用。プロンプトは長さだけ）
    static func slotSummary(_ id: Int, _ slot: SlotConfig) -> String {
        "slot\(id): hotkey=\(slot.hotkey.isEmpty ? "-" : slot.hotkey.joined(separator: "+")) "
            + "mode=\(slot.mode.rawValue) backend=\(slot.backend.rawValue) model=\(slot.model) "
            + "prompt長=\(slot.prompt.count) 整形=\(slot.formatEnabled)"
    }
}

// MARK: - ホットキーイベントの時刻

/// CGEvent のタイムスタンプを systemUptime 基準へ換算する。
///
/// CGEventTimestamp は「起動からのナノ秒」と文書化されているが、Apple Silicon では
/// mach_absolute_time の生の tick が入ってくる版がある（tick と ns の比が 1 でない）。
/// どちらの解釈かを決め打ちせず、両方を計算して「処理時刻より前・10 秒以内」に収まる方を採る。
/// どちらも収まらなければ nil（ログでは "-"）にして、誤った遅延を出さない。
enum HotkeyEventClock {
    static func uptime(
        timestamp: UInt64, handledAt: TimeInterval, numer: UInt32, denom: UInt32
    ) -> TimeInterval? {
        guard timestamp > 0, denom > 0 else { return nil }
        let asNanos = Double(timestamp) / 1e9
        let asTicks = Double(timestamp) * Double(numer) / Double(denom) / 1e9
        for candidate in [asNanos, asTicks] {
            let delay = handledAt - candidate
            // わずかな負値は時計の読み取り順の誤差として許す
            if delay >= -0.005 && delay <= 10 { return candidate }
        }
        return nil
    }

    /// この Mac の mach_timebase_info（起動中は変わらないので 1 回だけ取る）
    static let timebase: (numer: UInt32, denom: UInt32) = {
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        return (info.numer, info.denom)
    }()
}
