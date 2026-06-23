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
    var daily: [String: DayStat] = [:]
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

    init() {
        // ~/Library/Application Support/voicekey/stats.json（HistoryStore と同じ置き場所）
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
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
    }

    /// 音声入力 1 回分を記録する（貼り付け確定後に呼ぶ）。空入力は無視する。
    func recordSession(characters: Int, recordingSeconds: Double) {
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
        save()
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

    // MARK: - 派生値（レベル・XP）

    /// 経験値（= 累計文字数）
    var xp: Int { data.totalCharacters }

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
        var out: [UsagePoint] = []
        for i in stride(from: max(0, numDays) - 1, through: 0, by: -1) {
            guard let d = cal.date(byAdding: .day, value: -i, to: startOfEnd) else { continue }
            let key = Self.dayString(d)
            let b = data.daily[key]
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
        for (key, v) in data.daily {
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
