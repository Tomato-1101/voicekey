//
//  ApiUsageBackfill.swift
//  計測を始める前の API 使用量を、ログと統計から推定して取り込む（1 回だけ・純関数中心）
//
//  api-usage.json は 2026-10-02 から実測を溜め始めたので、それ以前の「いくら使ったか」は空になる。
//  代わりに次の 2 つの手がかりから推定して、実測と区別できる行（estimated = true）で入れる。
//   A. 行動ログ（~/Library/Logs/voicekey/voicekey-YYYY-MM-DD[.N].log）の「文字起こし要求」行。
//      バックエンド・モデル・音声秒数が残っているので精度が高い。ただしログの保持は 14 日だけ。
//   B. 統計（stats.json の daily）。ログが残っていない古い日の回数・録音秒数だけが分かる。
//      エンジンの情報は無いので Groq whisper-large-v3-turbo の REST と仮定する
//      （当時の常用が Groq だった根拠＝2026-09-02 のログが groq）。この仮定は UI の推定行にも出す。
//  LLM（整形・翻訳）のトークンは当時の記録が無いので取り込まない。
//
//  解析・差分計算・日付範囲の判定は副作用の無い関数に分けてあり、ApiUsageStore が
//  ファイルを読んで結果を書き込む（音声入力の経路には一切載せない）。
//

import Foundation

enum ApiUsageBackfill {

    /// 推定取り込みの版。ロジックを変えて取り直したくなったら上げる（推定行は毎回作り直すので重複しない）
    static let version = 1

    /// データ源 B（エンジン不明の古い日）に当てはめる API。当時の常用が Groq だったため
    static let statsAssumedProvider = ApiProvider.groq
    static let statsAssumedModel = "whisper-large-v3-turbo"

    // MARK: - データ源 A: 行動ログ

    /// ログの「文字起こし要求」1 行ぶん
    struct LoggedRequest: Equatable {
        /// streaming / rest
        var route: String
        /// Backend.rawValue
        var backend: String
        var model: String
        var seconds: Double
    }

    private static let requestPattern = try? NSRegularExpression(
        pattern: #"^\d{2}:\d{2}:\d{2}\.\d{3} \[transcriber\] 文字起こし要求 経路=(streaming|rest) backend=(\S+) model=(.+) 音声=([0-9]+(?:\.[0-9]+)?)s$"#)

    /// ログ 1 行を解析する（文字起こし要求の行以外は nil）
    static func parseLogLine(_ line: String) -> LoggedRequest? {
        guard let re = requestPattern else { return nil }
        let ns = line as NSString
        guard let m = re.firstMatch(in: line, range: NSRange(location: 0, length: ns.length)),
              m.numberOfRanges == 5,
              let seconds = Double(ns.substring(with: m.range(at: 4)))
        else { return nil }
        return LoggedRequest(
            route: ns.substring(with: m.range(at: 1)),
            backend: ns.substring(with: m.range(at: 2)),
            model: ns.substring(with: m.range(at: 3)),
            seconds: seconds)
    }

    /// ログの要求を、記録フックが使う provider とモデル名に直す。無料のローカル認識・未知の backend は nil。
    /// 対応は実際の記録箇所に合わせてある（Transcriber / SonioxLiveTranscriber / OpenAILiveTranscriber）。
    static func billingTarget(for r: LoggedRequest) -> (provider: ApiProvider, model: String)? {
        guard let backend = Backend(rawValue: r.backend) else { return nil }
        let provider = ApiProvider(backend: backend)
        if provider == .apple { return nil }
        // openai_live が REST に落ちたときは、実際に呼ぶ一括用モデルで課金される（Transcriber.restModel）
        if backend == .openaiLive && r.route == "rest" { return (provider, "gpt-transcribe") }
        return (provider, r.model)
    }

    /// 要求を provider × モデルごとの 1 行（用途=文字起こし・推定）にまとめる。最低課金秒数は 1 要求ごとに適用
    static func aggregate(_ requests: [LoggedRequest]) -> [ApiUsageEntry] {
        var rows: [ApiUsageEntry] = []
        for r in requests where r.seconds > 0 {
            guard let t = billingTarget(for: r) else { continue }
            var e = ApiUsageEntry(
                provider: t.provider.rawValue, model: t.model, purpose: ApiUsagePurpose.transcription.rawValue)
            e.requests = 1
            e.audioSeconds = r.seconds
            e.billedAudioSeconds = ApiPricing.billedSeconds(provider: t.provider, model: t.model, audioSeconds: r.seconds)
            e.estimated = true
            if let i = rows.firstIndex(where: { $0.sameKey(e) }) {
                rows[i].add(e)
            } else {
                rows.append(e)
            }
        }
        return rows
    }

    /// ログ由来の推定 = 「ログの秒数 − すでに実測で記録済みの秒数」の正の差分だけ。
    /// 計測の途中から始まった日（2026-10-02 など）で、実測済みの分を二重に数えないため。
    /// 実測側は音声の末尾無音など数 % ずれるので、差が「1 秒」と「ログの 2 %」の大きい方以下なら差とみなさない。
    static func estimates(logged: [ApiUsageEntry], measured: [ApiUsageEntry]) -> [ApiUsageEntry] {
        var out: [ApiUsageEntry] = []
        for l in logged {
            var m = ApiUsageEntry(provider: l.provider, model: l.model, purpose: l.purpose)
            for r in measured where !r.estimated && r.provider == l.provider && r.model == l.model
                && r.purpose == l.purpose {
                m.add(r)
            }
            let audioDiff = l.audioSeconds - m.audioSeconds
            if m.audioSeconds > 0, audioDiff <= max(1.0, 0.02 * l.audioSeconds) { continue }
            guard audioDiff > 0 else { continue }
            var e = l
            e.estimated = true
            e.audioSeconds = audioDiff
            e.requests = max(1, l.requests - m.requests)
            e.billedAudioSeconds = max(audioDiff, l.billedAudioSeconds - m.billedAudioSeconds)
            out.append(e)
        }
        return out
    }

    /// ログファイル名（voicekey-YYYY-MM-DD.log / voicekey-YYYY-MM-DD.2.log）から日付キーを取り出す
    static func logDay(fileName: String) -> String? {
        let prefix = "voicekey-"
        guard fileName.hasPrefix(prefix), fileName.hasSuffix(".log") else { return nil }
        let rest = fileName.dropFirst(prefix.count)
        guard rest.count >= 14 else { return nil }  // YYYY-MM-DD + ".log"
        let day = String(rest.prefix(10))
        guard rest.dropFirst(10).hasPrefix("."), isDayKey(day) else { return nil }
        return day
    }

    static func isDayKey(_ s: String) -> Bool {
        let p = s.split(separator: "-", omittingEmptySubsequences: false)
        return p.count == 3 && p[0].count == 4 && p[1].count == 2 && p[2].count == 2
            && p.allSatisfy { $0.allSatisfy(\.isNumber) }
    }

    // MARK: - データ源 B: 統計（stats.json の daily）

    struct StatsDay: Equatable {
        var sessions: Int
        var recordingSeconds: Double
    }

    /// 統計由来の推定を入れてよい日 = 「ログの最古日」と「実測の最古日」のどちらよりも前の日。
    /// ログがある日はログ（A）を、実測がある日は実測を優先するため。どちらも無ければ全日が対象。
    static func statsCutoffDay(oldestLogDay: String?, earliestMeasuredDay: String?) -> String? {
        [oldestLogDay, earliestMeasuredDay].compactMap { $0 }.min()
    }

    /// 統計の日別（回数・録音秒数）を Groq の REST と仮定した推定行にする。cutoff 未満の日だけ。
    /// 最低課金（1 要求 10 秒）は日の平均秒数で近似する（1 要求ごとの内訳は統計に無いため）。
    static func statsEstimates(
        daily: [String: StatsDay], before cutoff: String?
    ) -> [String: ApiUsageEntry] {
        var out: [String: ApiUsageEntry] = [:]
        for (day, s) in daily where isDayKey(day) && s.recordingSeconds > 0 {
            if let cutoff, !(day < cutoff) { continue }
            let requests = max(1, s.sessions)
            var e = ApiUsageEntry(
                provider: statsAssumedProvider.rawValue, model: statsAssumedModel,
                purpose: ApiUsagePurpose.transcription.rawValue)
            e.requests = requests
            e.audioSeconds = s.recordingSeconds
            e.billedAudioSeconds = Double(requests) * ApiPricing.billedSeconds(
                provider: statsAssumedProvider, model: statsAssumedModel,
                audioSeconds: s.recordingSeconds / Double(requests))
            e.estimated = true
            out[day] = e
        }
        return out
    }

    // MARK: - 取り込み計画（純関数）

    /// 読み込んだ元データ（ファイル I/O の結果）
    struct Sources: Equatable {
        /// 日付キー → その日のログから拾った要求
        var loggedByDay: [String: [LoggedRequest]] = [:]
        var statsDaily: [String: StatsDay] = [:]
    }

    /// 実測（measuredDays）と突き合わせて、日付キー → 追加する推定行 を作る
    static func plan(sources: Sources, measuredDays: [String: [ApiUsageEntry]]) -> [String: [ApiUsageEntry]] {
        var result: [String: [ApiUsageEntry]] = [:]
        for (day, requests) in sources.loggedByDay {
            let rows = estimates(logged: aggregate(requests), measured: measuredDays[day] ?? [])
            if !rows.isEmpty { result[day] = rows }
        }
        let measuredDayKeys = measuredDays.filter { $0.value.contains { !$0.estimated } }.keys
        let cutoff = statsCutoffDay(
            oldestLogDay: sources.loggedByDay.keys.min(), earliestMeasuredDay: measuredDayKeys.min())
        for (day, e) in statsEstimates(daily: sources.statsDaily, before: cutoff) {
            result[day, default: []].append(e)
        }
        return result
    }

    // MARK: - ファイル読み込み（バックグラウンドで 1 回だけ）

    /// ログディレクトリと stats.json を読む。どちらも無ければ空（何も取り込まない）
    static func readSources(logsDirectory: URL, statsFile: URL) -> Sources {
        var sources = Sources()
        let names = (try? FileManager.default.contentsOfDirectory(atPath: logsDirectory.path)) ?? []
        for name in names {
            guard let day = logDay(fileName: name),
                  let raw = try? Data(contentsOf: logsDirectory.appendingPathComponent(name))
            else { continue }
            let text = String(decoding: raw, as: UTF8.self)
            var found: [LoggedRequest] = []
            text.enumerateLines { line, _ in
                // 行頭の時刻から始まる行だけを正規表現にかける（ほとんどの行はここで弾く）
                guard line.contains("文字起こし要求"), let r = parseLogLine(line) else { return }
                found.append(r)
            }
            // 同じ日付キーのファイル（連番ローテート分）はまとめる。要求が 0 件の日も「ログがある日」として残す
            sources.loggedByDay[day, default: []].append(contentsOf: found)
        }
        if let raw = try? Data(contentsOf: statsFile),
           let root = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
           let daily = root["daily"] as? [String: Any] {
            for (day, v) in daily {
                guard let d = v as? [String: Any] else { continue }
                sources.statsDaily[day] = StatsDay(
                    sessions: (d["sessions"] as? NSNumber)?.intValue ?? 0,
                    recordingSeconds: (d["recordingSeconds"] as? NSNumber)?.doubleValue ?? 0)
            }
        }
        return sources
    }
}
