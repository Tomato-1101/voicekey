//
//  StatsStore.swift
//  使用実績（節約時間・レベル・連続日数）の集計と永続化
//
//  音声入力 1 回ごとに「文字数・録音秒数・日付」を積み上げ、累計の
//  推定節約時間・レベル・連続利用日数を計算して設定 UI に見せる。
//  すべて貼り付け確定「後」のローカル集計なので、音声→テキストの遅延には
//  一切影響しない（音声経路には乗らない）。
//  アプリ再起動後も残るよう Application Support に JSON で保存する。
//

import Foundation
import os.log

private let statsLog = Logger(subsystem: "com.voicekey.app", category: "stats")

/// 日付ごとの入力量（チャート用の 1 日ぶん）
struct DayStat: Codable, Equatable {
    var characters: Int = 0
    var recordingSeconds: Double = 0
    var sessions: Int = 0
}

/// アプリ別の累計集計（貼り付け先アプリごとの利用量）。ホームの「アプリ別使用率」で表示する。
struct AppStat: Codable, Equatable {
    /// 表示名（記録時の最新の localizedName で更新する。bundleID しか無ければ空）
    var appName: String = ""
    /// このアプリへ入力した回数
    var sessions: Int = 0
    /// このアプリへ入力した累計文字数
    var characters: Int = 0
    /// このアプリでの累計録音秒数
    var seconds: Double = 0
}

/// チャートの棒 1 本ぶん（日次・月次で共用）。
/// label は曜日 / 日 / 月（View 側で期間に応じて整形）、date は並べ替え・整形の基準。
struct UsagePoint: Identifiable {
    let id: String       // 一意キー（yyyy-MM-dd または yyyy-MM）
    let label: String
    let date: Date
    let characters: Int
    let recordingSeconds: Double
    let sessions: Int
}

/// 永続化する累計実績（JSON エンコード対象）
struct StatsData: Codable, Equatable {
    /// 音声入力した回数
    var totalSessions: Int = 0
    /// 累計の出力文字数（= 経験値）
    var totalCharacters: Int = 0
    /// 累計の録音秒数
    var totalRecordingSeconds: Double = 0
    /// 累計の推定節約秒数（手入力にかかる時間 − 実際の発話時間の積み上げ）
    var savedSeconds: Double = 0
    /// 初回利用日時
    var firstUseDate: Date?
    /// 最後に使った日（ローカル yyyy-MM-dd）。連続利用日数の判定に使う
    var lastUsedDay: String = ""
    /// 現在の連続利用日数
    var currentStreak: Int = 0
    /// これまでの最長連続利用日数
    var longestStreak: Int = 0
    /// 日付ごとの入力量（チャート用）。キーはローカル yyyy-MM-dd
    /// ★これは「この端末で記録した分」だけを保持する＝サーバーへ送る送信元。
    ///   端末横断の表示には accountDaily を使う（二重計上を避けるため別管理）。
    var daily: [String: DayStat] = [:]
    /// 貼り付け先アプリ別の累計集計（キー = bundleID）。この端末のローカル集計のみ
    /// （サーバー横断集計はしない＝アプリ別はローカル表示専用）。
    var appUsage: [String: AppStat] = [:]

    // MARK: - アカウント連携（#10。ログイン中のみ・端末横断の取り込み結果を保持）

    /// サーバーから取り込んだ端末横断の日次合算（day(yyyy-MM-dd)→合算値）。
    /// nil = 未取り込み（未ログイン or 取得前）。再起動後も表示が消えないよう永続化する。
    var accountDaily: [String: DayStat]?
    /// 端末横断の累計文字数（サーバー totals）。レベル/XP の算出に使う
    var accountTotalCharacters: Int?
    /// 端末横断の累計回数（サーバー totals）
    var accountTotalSessions: Int?
    /// 端末横断の累計録音秒数（サーバー totals）
    var accountTotalRecordingSeconds: Double?
    /// #11: サーバーが持つ「この端末ぶん」の日次（server baseline）。
    /// 端末横断 accountDaily からこの端末の寄与を引き、未同期のローカル差分だけを
    /// 足すために使う＝二重計上も過少計上もしない。nil=旧サーバー（従来の max 合成）。
    var accountSelfDaily: [String: DayStat]?
}

/// ログイン/ログアウトを各ストアへ伝えるための通知（LoginCoordinator が発火する）
extension Notification.Name {
    /// ブラウザ経由ログインのトークン交換が成功した直後
    static let voicekeyDidLogin = Notification.Name("com.voicekey.didLogin")
    /// ログアウトした直後
    static let voicekeyDidLogout = Notification.Name("com.voicekey.didLogout")
}

extension StatsData {
    /// 後からフィールドを増やしても古い JSON を読めるよう、全項目を decodeIfPresent で読む
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        totalSessions = try c.decodeIfPresent(Int.self, forKey: .totalSessions) ?? 0
        totalCharacters = try c.decodeIfPresent(Int.self, forKey: .totalCharacters) ?? 0
        totalRecordingSeconds = try c.decodeIfPresent(Double.self, forKey: .totalRecordingSeconds) ?? 0
        savedSeconds = try c.decodeIfPresent(Double.self, forKey: .savedSeconds) ?? 0
        firstUseDate = try c.decodeIfPresent(Date.self, forKey: .firstUseDate)
        lastUsedDay = try c.decodeIfPresent(String.self, forKey: .lastUsedDay) ?? ""
        currentStreak = try c.decodeIfPresent(Int.self, forKey: .currentStreak) ?? 0
        longestStreak = try c.decodeIfPresent(Int.self, forKey: .longestStreak) ?? 0
        daily = try c.decodeIfPresent([String: DayStat].self, forKey: .daily) ?? [:]
        appUsage = try c.decodeIfPresent([String: AppStat].self, forKey: .appUsage) ?? [:]
        accountDaily = try c.decodeIfPresent([String: DayStat].self, forKey: .accountDaily)
        accountTotalCharacters = try c.decodeIfPresent(Int.self, forKey: .accountTotalCharacters)
        accountTotalSessions = try c.decodeIfPresent(Int.self, forKey: .accountTotalSessions)
        accountTotalRecordingSeconds = try c.decodeIfPresent(Double.self, forKey: .accountTotalRecordingSeconds)
        accountSelfDaily = try c.decodeIfPresent([String: DayStat].self, forKey: .accountSelfDaily)
    }
}

/// 使用実績のストア。
/// 記録・リセットのたびに自動保存され、設定 UI が @Published を購読して即時反映する。
@MainActor
final class StatsStore: ObservableObject {

    /// タイピング速度の仮定（文字/秒）。節約時間 = 文字数 / これ − 録音秒数。
    /// 過大表示を避けるため速め（240 字/分）に置く＝これより速く打てる人は節約が控えめに出る。
    static let assumedTypingCharsPerSecond: Double = 4.0

    @Published private(set) var data = StatsData()

    private let fileURL: URL

    /// - Parameter directory: 保存先ディレクトリ（テスト時に一時ディレクトリを注入する）。
    ///   nil なら本番の ~/Library/Application Support/voicekey を使う。
    init(directory: URL? = nil) {
        // ~/Library/Application Support/voicekey/stats.json（HistoryStore と同じ置き場所）
        let dir = directory ?? FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("voicekey", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        fileURL = dir.appendingPathComponent("stats.json")

        if let raw = try? Data(contentsOf: fileURL) {
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            if let loaded = try? decoder.decode(StatsData.self, from: raw) {
                data = loaded
            }
        }

        // ログイン/ログアウトに追従して端末横断の実績を取り込む/破棄する（#10）
        NotificationCenter.default.addObserver(
            forName: .voicekeyDidLogin, object: nil, queue: nil
        ) { [weak self] _ in
            Task { @MainActor in await self?.syncOnLogin() }
        }
        NotificationCenter.default.addObserver(
            forName: .voicekeyDidLogout, object: nil, queue: nil
        ) { [weak self] _ in
            Task { @MainActor in self?.clearAccount() }
        }
        // すでにログイン済みで起動した場合は、起動時に一度だけ同期する
        if BackendClient.isLoggedIn {
            Task { @MainActor in await self.syncOnLogin() }
        }
    }

    /// 音声入力 1 回分を記録する（貼り付け確定後に呼ぶ）。空入力は無視する。
    /// appBundleID / appName を渡すと貼り付け先アプリ別にも累計する（nil 許容）。
    func recordSession(
        characters: Int, recordingSeconds: Double,
        appBundleID: String? = nil, appName: String? = nil
    ) {
        guard characters > 0 else { return }
        let now = Date()
        if data.firstUseDate == nil { data.firstUseDate = now }
        data.totalSessions += 1
        data.totalCharacters += characters
        let recSec = max(0, recordingSeconds)
        data.totalRecordingSeconds += recSec
        // 推定節約 = 手入力にかかる時間 − 実際の発話時間（マイナスは 0 に丸める）
        let typingSeconds = Double(characters) / Self.assumedTypingCharsPerSecond
        data.savedSeconds += max(0, typingSeconds - recSec)
        updateStreak(now: now)
        recordDaily(now: now, characters: characters, recordingSeconds: recSec)
        recordApp(bundleID: appBundleID, appName: appName, characters: characters, recordingSeconds: recSec)
        save()
        // ログイン中なら当日分（この端末の絶対値）をサーバーへ送る（音声経路には乗らない）
        syncTodayInBackground()
    }

    /// 日付ごとのバケット（文字数・録音秒・回数）を積み増す。チャート表示用
    private func recordDaily(now: Date, characters: Int, recordingSeconds: Double) {
        let day = Self.dayString(now)
        var bucket = data.daily[day] ?? DayStat()
        bucket.characters += characters
        bucket.recordingSeconds += recordingSeconds
        bucket.sessions += 1
        data.daily[day] = bucket
        pruneDaily()
    }

    /// 貼り付け先アプリ別の累計を積み増す（bundleID が無ければ何もしない）。
    /// 表示名は記録のたびに最新へ更新する（アプリ名が変わっても最後の名前で出す）。
    private func recordApp(bundleID: String?, appName: String?, characters: Int, recordingSeconds: Double) {
        guard let bundleID, !bundleID.isEmpty else { return }
        var stat = data.appUsage[bundleID] ?? AppStat()
        if let appName, !appName.isEmpty { stat.appName = appName }
        stat.sessions += 1
        stat.characters += characters
        stat.seconds += recordingSeconds
        data.appUsage[bundleID] = stat
    }

    /// 日次バケットが増えすぎないよう、古い日から落として直近 800 日に保つ
    private func pruneDaily() {
        guard data.daily.count > 800 else { return }
        let keep = Set(data.daily.keys.sorted().suffix(800))
        data.daily = data.daily.filter { keep.contains($0.key) }
    }

    /// 実績をすべてリセットする（テスト用・ユーザーが消したいとき用）
    func reset() {
        data = StatsData()
        save()
    }

    /// 連続利用日数を更新する。同日2回目は据え置き、前日からの継続で +1、空きが出たら 1 に戻す
    private func updateStreak(now: Date) {
        let today = Self.dayString(now)
        if data.lastUsedDay == today { return }
        let yesterdayDate = Calendar.current.date(byAdding: .day, value: -1, to: now) ?? now
        let yesterday = Self.dayString(yesterdayDate)
        data.currentStreak = (data.lastUsedDay == yesterday) ? data.currentStreak + 1 : 1
        data.lastUsedDay = today
        data.longestStreak = max(data.longestStreak, data.currentStreak)
    }

    // MARK: - アカウント横断の派生値（#10。未ログイン時はローカルにフォールバック）

    /// 表示用の日次（チャート用）。ログイン中はサーバー合算（端末横断）にこの端末の未送信分を重ねる。
    /// #11: server baseline(accountSelfDaily) があれば effective = account + max(0, local - baseline)
    /// で合成する。account は baseline を含むので、未同期のローカル差分だけを足す＝二重計上も
    /// 過少計上もしない。baseline が無い旧サーバーは従来の field-wise max にフォールバック
    /// （二重計上を避ける安全側）。未ログインはローカルのみ。
    var effectiveDaily: [String: DayStat] {
        guard let acc = data.accountDaily else { return data.daily }
        var out = acc
        let baseline = data.accountSelfDaily
        for (key, local) in data.daily {
            var merged = out[key] ?? DayStat()
            if let baseline {
                let bl = baseline[key] ?? DayStat()
                merged.characters += max(0, local.characters - bl.characters)
                merged.recordingSeconds += max(0, local.recordingSeconds - bl.recordingSeconds)
                merged.sessions += max(0, local.sessions - bl.sessions)
            } else {
                merged.characters = max(merged.characters, local.characters)
                merged.recordingSeconds = max(merged.recordingSeconds, local.recordingSeconds)
                merged.sessions = max(merged.sessions, local.sessions)
            }
            out[key] = merged
        }
        return out
    }

    /// 表示用の累計を端末横断＋未同期分で算出するヘルパ（#11）。
    /// server baseline があれば account + max(0, local - baselineSum)、無ければ max(local, account)。
    private func mergedTotal(local: Int, account: Int?, baselineSum: Int) -> Int {
        guard let account else { return local }
        if data.accountSelfDaily != nil {
            return account + max(0, local - baselineSum)
        }
        return max(local, account)
    }

    private func mergedTotal(local: Double, account: Double?, baselineSum: Double) -> Double {
        guard let account else { return local }
        if data.accountSelfDaily != nil {
            return account + max(0, local - baselineSum)
        }
        return max(local, account)
    }

    /// server baseline(accountSelfDaily) の各フィールド合計（未ログイン/旧サーバーは 0）
    private var baselineCharacters: Int { (data.accountSelfDaily ?? [:]).values.reduce(0) { $0 + $1.characters } }
    private var baselineSessions: Int { (data.accountSelfDaily ?? [:]).values.reduce(0) { $0 + $1.sessions } }
    private var baselineRecordingSeconds: Double { (data.accountSelfDaily ?? [:]).values.reduce(0) { $0 + $1.recordingSeconds } }

    /// 表示用の累計文字数。#11: server baseline があれば端末横断の合算にこの端末の
    /// 未同期分だけを足す（無ければローカルを下限にした max で XP が下がらないようにする）。
    var totalCharacters: Int {
        mergedTotal(local: data.totalCharacters, account: data.accountTotalCharacters, baselineSum: baselineCharacters)
    }

    /// 表示用の累計回数（同上）
    var totalSessions: Int {
        mergedTotal(local: data.totalSessions, account: data.accountTotalSessions, baselineSum: baselineSessions)
    }

    /// 表示用の累計録音秒数（同上）
    var totalRecordingSeconds: Double {
        mergedTotal(local: data.totalRecordingSeconds, account: data.accountTotalRecordingSeconds, baselineSum: baselineRecordingSeconds)
    }

    /// 表示用の推定節約秒数。未ログインはローカル精密値、ログイン中は端末横断の日次から再計算。
    /// （サーバーは節約秒を持たないため日次の文字数・録音秒から同じ式で復元する）
    var savedSeconds: Double {
        guard data.accountDaily != nil else { return data.savedSeconds }
        let fromDaily = effectiveDaily.values.reduce(0.0) { acc, d in
            acc + max(0, Double(d.characters) / Self.assumedTypingCharsPerSecond - d.recordingSeconds)
        }
        return max(data.savedSeconds, fromDaily)  // 古い実績で下がらないようローカルを下限にする
    }

    /// 利用のあった日付キー集合（端末横断・表示用）
    private var activeDayKeys: Set<String> {
        Set(effectiveDaily.compactMap { $0.value.characters > 0 || $0.value.sessions > 0 ? $0.key : nil })
    }

    /// 表示用の現在の連続利用日数。未ログインはローカル、ログイン中は端末横断の日付集合から算出。
    var currentStreak: Int {
        guard data.accountDaily != nil else { return data.currentStreak }
        let days = activeDayKeys
        guard !days.isEmpty else { return data.currentStreak }
        let cal = Calendar.current
        var cursor = cal.startOfDay(for: Date())
        // 今日まだ記録が無くても、昨日まで連続していれば途切れさせない
        if !days.contains(Self.dayString(cursor)) {
            cursor = cal.date(byAdding: .day, value: -1, to: cursor) ?? cursor
        }
        var streak = 0
        while days.contains(Self.dayString(cursor)) {
            streak += 1
            guard let prev = cal.date(byAdding: .day, value: -1, to: cursor) else { break }
            cursor = prev
        }
        return max(streak, data.currentStreak)
    }

    /// 表示用の最長連続利用日数（同上）
    var longestStreak: Int {
        guard data.accountDaily != nil else { return data.longestStreak }
        let keys = activeDayKeys.sorted()
        guard !keys.isEmpty else { return data.longestStreak }
        let cal = Calendar.current
        let f = DateFormatter()
        f.calendar = cal
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        var longest = 1, run = 1
        var prev = f.date(from: keys[0])
        for k in keys.dropFirst() {
            guard let cur = f.date(from: k) else { continue }
            if let p = prev, (cal.dateComponents([.day], from: p, to: cur).day ?? 0) == 1 {
                run += 1
            } else {
                run = 1
            }
            longest = max(longest, run)
            prev = cur
        }
        return max(longest, data.longestStreak)
    }

    // MARK: - 派生値（レベル・XP）

    /// 経験値（= 累計文字数。ログイン中は端末横断）
    var xp: Int { totalCharacters }

    /// 現在レベル
    var level: Int { Self.level(forXP: xp) }

    /// 現在レベル内の進捗（0...1）。プログレスバー表示用
    var levelProgress: Double {
        let base = Self.threshold(level)
        let next = Self.threshold(level + 1)
        guard next > base else { return 0 }
        return min(1, max(0, Double(xp - base) / Double(next - base)))
    }

    /// 次のレベルまで残り何文字か
    var xpToNextLevel: Int { max(0, Self.threshold(level + 1) - xp) }

    /// レベル L に到達するのに必要な累計 XP（= 250*(L-1)*L）。
    /// L1:0 / L2:500 / L3:1500 / L4:3000 / L5:5000 … と必要量が増える
    static func threshold(_ L: Int) -> Int { 250 * max(0, L - 1) * max(0, L) }

    /// 累計 XP から現在レベルを逆算する（threshold(L) <= xp を満たす最大の L）
    static func level(forXP xp: Int) -> Int {
        let x = Double(max(0, xp))
        let inner: Double = 1.0 + 4.0 * x / 250.0
        let root = inner.squareRoot()
        let l = Int((1.0 + root) / 2.0)
        return max(1, l)
    }

    /// ローカルタイムゾーンでの yyyy-MM-dd 文字列（連続日数の判定キー）
    static func dayString(_ date: Date) -> String {
        let f = DateFormatter()
        f.calendar = Calendar.current
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }

    /// ローカルタイムゾーンでの yyyy-MM 文字列（月次集計のキー）
    static func monthString(_ date: Date) -> String {
        let f = DateFormatter()
        f.calendar = Calendar.current
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM"
        return f.string(from: date)
    }

    // MARK: - チャート用の系列（日次 / 月次）

    /// 末尾を end（既定=今日）として直近 numDays 日分を古い順で返す（記録の無い日は 0）
    func dailySeries(_ numDays: Int, endingAt end: Date = Date()) -> [UsagePoint] {
        let cal = Calendar.current
        let startOfEnd = cal.startOfDay(for: end)
        let source = effectiveDaily
        var out: [UsagePoint] = []
        for i in stride(from: max(0, numDays) - 1, through: 0, by: -1) {
            guard let d = cal.date(byAdding: .day, value: -i, to: startOfEnd) else { continue }
            let key = Self.dayString(d)
            let b = source[key]
            out.append(UsagePoint(
                id: key, label: key, date: d,
                characters: b?.characters ?? 0,
                recordingSeconds: b?.recordingSeconds ?? 0,
                sessions: b?.sessions ?? 0))
        }
        return out
    }

    /// 末尾を end（既定=今月）として直近 numMonths ヶ月分を古い順で返す（記録の無い月は 0）
    func monthlySeries(_ numMonths: Int, endingAt end: Date = Date()) -> [UsagePoint] {
        let cal = Calendar.current
        let comps = cal.dateComponents([.year, .month], from: end)
        guard let startOfEndMonth = cal.date(from: comps) else { return [] }
        // 日次バケットを月キー（yyyy-MM）へ集約する
        var agg: [String: (chars: Int, sec: Double, sess: Int)] = [:]
        for (key, v) in effectiveDaily {
            let mk = String(key.prefix(7))
            var a = agg[mk] ?? (0, 0, 0)
            a.chars += v.characters; a.sec += v.recordingSeconds; a.sess += v.sessions
            agg[mk] = a
        }
        var out: [UsagePoint] = []
        for i in stride(from: max(0, numMonths) - 1, through: 0, by: -1) {
            guard let d = cal.date(byAdding: .month, value: -i, to: startOfEndMonth) else { continue }
            let mk = Self.monthString(d)
            let a = agg[mk]
            out.append(UsagePoint(
                id: mk, label: mk, date: d,
                characters: a?.chars ?? 0,
                recordingSeconds: a?.sec ?? 0,
                sessions: a?.sess ?? 0))
        }
        return out
    }

    /// 直近 n 日の合計文字数（サマリーの「今日 / 今週」表示用）
    func charactersInLast(days: Int) -> Int {
        dailySeries(days).reduce(0) { $0 + $1.characters }
    }

    /// 直近 n 日の合計録音秒数
    func recordingSecondsInLast(days: Int) -> Double {
        dailySeries(days).reduce(0) { $0 + $1.recordingSeconds }
    }

    // MARK: - アカウント同期（#10）

    // #11: 当日分の送信を「アカウント単位で直列化＋coalesce」する単一フライト制御。
    // @MainActor 隔離なので await をまたがない限り不可分。録音のたびに新タスクを撃って
    // 送信を並走させると古い絶対値が新しい値を上書きする順序逆転が起きうるため、
    // ワーカーは 1 本だけ動かし、走行中の追加要求は syncPending に畳む。
    private var syncInFlight = false
    private var syncPending = false

    /// 当日分（この端末の絶対値）をサーバーへ送る。ログイン中のみ・撃ちっぱなし。
    /// (user_id,device_id,day) の絶対値 upsert なので冪等。
    func syncTodayInBackground() {
        guard BackendClient.isLoggedIn else { return }
        if syncInFlight {
            syncPending = true  // 走行中＝最新値の再送だけ予約して畳む
            return
        }
        syncInFlight = true
        Task { await self.syncWorker() }
    }

    /// 当日分の送信ワーカー（単一フライト）。pending が立っている限り最新値を送り直す。
    private func syncWorker() async {
        repeat {
            syncPending = false
            guard BackendClient.isLoggedIn else { break }
            let key = Self.dayString(Date())
            guard let b = data.daily[key] else { break }
            let payload = BackendClient.StatsDayPayload(
                day: key,
                chars: b.characters,
                sessions: b.sessions,
                duration_ms: Int((b.recordingSeconds * 1000).rounded()))
            try? await BackendClient.syncStats(days: [payload])
        } while syncPending
        syncInFlight = false
    }

    /// ログイン直後/起動時に呼ぶ。まずこの端末の直近分を押し上げ、続いて端末横断の集計を取り込む。
    func syncOnLogin() async {
        let recent = recentOwnDays(60)
        if !recent.isEmpty {
            try? await BackendClient.syncStats(days: recent)
        }
        await refreshAccount()
    }

    /// 端末横断の集計をサーバーから取り込む（accountDaily / account累計を更新して永続化）。
    /// #11: 取得開始時の認証世代を控え、取得中にログアウト/別ログインが起きていたら結果を捨てる
    /// （前アカウントの実績を復活させる「アカウント漏洩」を防ぐ）。
    func refreshAccount() async {
        let gen = AuthClient.generation
        guard let snap = try? await BackendClient.fetchStats() else { return }
        guard gen == AuthClient.generation else { return }  // 取得中にアカウントが変わった＝破棄
        data.accountDaily = snap.daily
        data.accountTotalCharacters = snap.totalCharacters
        data.accountTotalSessions = snap.totalSessions
        data.accountTotalRecordingSeconds = snap.totalRecordingSeconds
        data.accountSelfDaily = snap.selfDaily  // server baseline（旧サーバーは nil）
        save()
    }

    /// ログアウト時に端末横断の取り込み結果を破棄し、ローカル表示へ戻す。
    func clearAccount() {
        data.accountDaily = nil
        data.accountTotalCharacters = nil
        data.accountTotalSessions = nil
        data.accountTotalRecordingSeconds = nil
        data.accountSelfDaily = nil
        save()
    }

    /// この端末で記録した直近 numDays 日分を送信形（絶対値）で返す（記録の無い日は除外）。
    private func recentOwnDays(_ numDays: Int) -> [BackendClient.StatsDayPayload] {
        let cal = Calendar.current
        let start = cal.startOfDay(for: Date())
        var out: [BackendClient.StatsDayPayload] = []
        for i in 0..<max(0, numDays) {
            guard let d = cal.date(byAdding: .day, value: -i, to: start) else { continue }
            let key = Self.dayString(d)
            guard let b = data.daily[key] else { continue }
            out.append(BackendClient.StatsDayPayload(
                day: key,
                chars: b.characters,
                sessions: b.sessions,
                duration_ms: Int((b.recordingSeconds * 1000).rounded())))
        }
        return out
    }

    private func save() {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        do {
            try encoder.encode(data).write(to: fileURL, options: [.atomic])
        } catch {
            statsLog.error("実績の保存に失敗: \(error.localizedDescription)")
        }
    }
}
