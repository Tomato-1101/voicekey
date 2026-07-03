//
//  HomeView.swift
//  ダッシュボード面（メインウィンドウのホーム側コンテンツ）
//
//  実績・履歴・アプリ別使用率・アップデート導線をまとめた「今どれだけ使ったか」が
//  一目で分かるダッシュボード。ブランド行やサイドバー・すりガラス下地は MainWindowView が
//  持つため、ここはコンテンツ面だけを描く（v2.1: フラット背景の上に控えめなカードを敷く）。
//

import AppKit
import SwiftUI

struct HomeView: View {
    @ObservedObject var config: ConfigStore
    @ObservedObject var history: HistoryStore
    @ObservedObject var stats: StatsStore
    @ObservedObject var updater: UpdaterController

    /// 直近にコピーした履歴エントリ（行に「コピーしました」を一時表示する）
    @State private var copiedId: UUID?

    var body: some View {
        // レイアウト v2.1: 島で全面を包まない。MainWindowView の frosted backdrop の上に
        // 控えめなカードをフラットに敷く（大きな島で画面を分割しない）。
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                statsCard
                appUsageSection
                historySection
            }
            .padding(20)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - ヘッダ

    /// ヘッダ行。左＝更新ピル（検知時だけ）or ダッシュボードの見出し＋今日の一言。
    /// ブランド行と設定導線はサイドバー（MainWindowView）に移したためここには置かない。
    private var header: some View {
        HStack(spacing: 10) {
            if showUpdatePill {
                updatePill
            } else {
                VStack(alignment: .leading, spacing: 2) {
                    Text("ダッシュボード").font(.title3.weight(.semibold))
                    Text(todayGreeting).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.top, 6)  // タイトルバー帯からヘッダを逃がす
    }

    /// 今日の入力量の一言（ダッシュボードのあいさつ）
    private var todayGreeting: String {
        let today = stats.charactersInLast(days: 1)
        return today > 0 ? "今日はここまで \(today) 文字を入力しました" : "今日はまだ入力していません"
    }

    /// 新バージョン検知時だけ出す更新ピル。落ち着いた青系・小さめの横長角丸ピル。
    /// クリックで Sparkle の対話フロー（DL→インストール）を開始する。
    private var updatePill: some View {
        Button {
            updater.checkForUpdates()
        } label: {
            HStack(spacing: 5) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 12, weight: .semibold))
                Text(updatePillLabel)
                    .font(.system(size: 12, weight: .medium))
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .foregroundStyle(.white)
            .background(
                // アクセントを使わず彩度を抑えた青（雰囲気を壊さない・少しだけ目立つ）
                Capsule().fill(Color(red: 0.32, green: 0.50, blue: 0.80))
            )
            .overlay(
                Capsule().strokeBorder(Color.white.opacity(0.25), lineWidth: 0.5)
            )
        }
        .buttonStyle(.plain)
        .help("新しいバージョンに更新します")
    }

    /// 更新ピルの文言（バージョンが分かれば添える）
    private var updatePillLabel: String {
        if let v = updater.availableVersion {
            return "v\(v) に更新する"
        }
        return "更新する"
    }

    /// 更新ピルを出すか（配布ビルドで新バージョン検知時のみ）
    private var showUpdatePill: Bool {
        updater.isAvailable && updater.updateAvailable
    }

    // MARK: - 統計カード

    /// 今日 / 今週 / 累計のタイルと、レベル・節約時間をまとめた 1 枚のカード。
    private var statsCard: some View {
        VStack(spacing: 12) {
            HStack(spacing: 12) {
                statTile(title: "今日", value: stats.charactersInLast(days: 1),
                         sub: "\(todaySessions) 回")
                Divider().frame(height: 46)
                statTile(title: "今週", value: stats.charactersInLast(days: 7),
                         sub: "録音 \(formattedShort(stats.recordingSecondsInLast(days: 7)))")
                Divider().frame(height: 46)
                statTile(title: "累計", value: stats.totalCharacters,
                         sub: "Lv.\(stats.level)")
            }
            Divider()
            // レベル進捗と推定節約時間（既存 StatsTab の主要指標を簡潔に）
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text("レベル \(stats.level)").font(.headline)
                    Spacer()
                    Text("推定節約時間 \(formattedSaved)")
                        .font(.caption).foregroundStyle(.secondary)
                }
                ProgressView(value: stats.levelProgress)
                Text("あと \(stats.xpToNextLevel) 文字でレベル \(stats.level + 1)")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 12).fill(fillColor)
        )
    }

    /// 今日の入力回数（サマリー補足用）
    private var todaySessions: Int { stats.dailySeries(1).last?.sessions ?? 0 }

    /// サマリー 1 枚（ラベル＋大きな数字＋補足）
    private func statTile(title: String, value: Int, sub: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 2) {
                Text("\(value)")
                    .font(.system(size: 22, weight: .bold, design: .rounded))
                    .monospacedDigit()
                Text("文字").font(.caption2).foregroundStyle(.secondary)
            }
            Text(sub).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - アプリ別使用率

    /// アプリ別使用率（累計文字数の上位 5 アプリを横バーで表示）。
    private var appUsageSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("アプリ別の使用状況").font(.headline)
            if topApps.isEmpty {
                emptyState("使うほど、ここにアプリごとの使用状況がたまります。")
            } else {
                VStack(spacing: 8) {
                    let maxChars = max(1, topApps.first?.stat.characters ?? 1)
                    ForEach(topApps, id: \.bundleID) { entry in
                        appUsageRow(entry: entry, maxChars: maxChars)
                    }
                }
                .padding(14)
                .background(RoundedRectangle(cornerRadius: 12).fill(fillColor))
            }
        }
    }

    /// アプリ別集計の上位 5 件（累計文字数の多い順）
    private var topApps: [(bundleID: String, stat: AppStat)] {
        stats.data.appUsage
            .map { (bundleID: $0.key, stat: $0.value) }
            .sorted { $0.stat.characters > $1.stat.characters }
            .prefix(5)
            .map { $0 }
    }

    /// アプリ 1 行（アイコン＋名前＋割合バー）
    private func appUsageRow(entry: (bundleID: String, stat: AppStat), maxChars: Int) -> some View {
        let ratio = maxChars > 0 ? Double(entry.stat.characters) / Double(maxChars) : 0
        return HStack(spacing: 10) {
            appIconView(bundleID: entry.bundleID, size: 22)
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text(appDisplayName(entry)).font(.system(size: 13)).lineLimit(1)
                    Spacer(minLength: 8)
                    Text("\(entry.stat.characters) 文字")
                        .font(.caption2).foregroundStyle(.secondary).monospacedDigit()
                }
                // 割合バー（最も使ったアプリを 1.0 とした相対長）
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.primary.opacity(0.08))
                        Capsule().fill(Color.accentColor.opacity(0.75))
                            .frame(width: max(6, geo.size.width * ratio))
                    }
                }
                .frame(height: 6)
            }
        }
    }

    /// アプリの表示名（記録した名前が空なら bundleID を出す）
    private func appDisplayName(_ entry: (bundleID: String, stat: AppStat)) -> String {
        if !entry.stat.appName.isEmpty { return entry.stat.appName }
        return entry.bundleID
    }

    // MARK: - 最近の履歴

    /// 最近の履歴（直近 8 件・行クリックでコピー）。ヘッダに「消去」。
    private var historySection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("最近の履歴").font(.headline)
                Spacer()
                if !history.items.isEmpty {
                    Button("消去") { history.clear() }
                        .buttonStyle(.plain)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .help("履歴をすべて消去する")
                }
            }
            if history.items.isEmpty {
                emptyState("音声入力すると、ここに最近の履歴が残ります（クリックでコピーできます）。")
            } else {
                VStack(spacing: 0) {
                    let recent = Array(history.items.prefix(8))
                    ForEach(Array(recent.enumerated()), id: \.element.id) { index, entry in
                        historyRow(entry)
                        if index < recent.count - 1 {
                            Divider().padding(.leading, 34)
                        }
                    }
                }
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 12).fill(fillColor))
            }
        }
    }

    /// 履歴 1 行（アプリアイコン＋テキスト＋相対時刻。クリックでコピー）
    private func historyRow(_ entry: HistoryItem) -> some View {
        Button {
            copyToClipboard(entry)
        } label: {
            HStack(alignment: .top, spacing: 8) {
                appIconView(bundleID: entry.appBundleID, size: 20)
                VStack(alignment: .leading, spacing: 3) {
                    Text(entry.text)
                        .font(.system(size: 12))
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    HStack(spacing: 6) {
                        Text(Self.relativeFormatter.localizedString(for: entry.date, relativeTo: Date()))
                            .font(.caption2).foregroundStyle(.secondary)
                        Spacer()
                        if copiedId == entry.id {
                            Label("コピーしました", systemImage: "checkmark.circle.fill")
                                .font(.caption2).foregroundStyle(.green)
                        } else {
                            Image(systemName: "doc.on.doc")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 6)
            .contentShape(Rectangle())  // 行全体をクリック領域にする
        }
        .buttonStyle(.plain)
    }

    /// 履歴テキストをクリップボードへコピーし、その行に一時フィードバックを出す
    private func copyToClipboard(_ entry: HistoryItem) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(entry.text, forType: .string)
        copiedId = entry.id
        Task {
            try? await Task.sleep(for: .seconds(1.5))
            if copiedId == entry.id { copiedId = nil }
        }
    }

    // MARK: - 共通ヘルパ

    /// カード / ピルの控えめなフィル色（glassFormRows と同じ半透明値で島の質感に揃える）
    @Environment(\.colorScheme) private var colorScheme
    private var fillColor: Color {
        colorScheme == .dark ? Color.white.opacity(0.06) : Color.white.opacity(0.40)
    }

    /// 空状態の案内（待ち/未使用をユーザーが判別できるように）
    private func emptyState(_ message: String) -> some View {
        Text(message)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, minHeight: 64, alignment: .center)
            .multilineTextAlignment(.center)
            .padding(14)
            .background(RoundedRectangle(cornerRadius: 12).fill(fillColor))
    }

    /// bundleID からアプリアイコンを描画する（解決できなければ汎用アイコン）
    @ViewBuilder
    private func appIconView(bundleID: String?, size: CGFloat) -> some View {
        if let icon = Self.appIcon(bundleID: bundleID) {
            Image(nsImage: icon)
                .resizable()
                .frame(width: size, height: size)
                .clipShape(RoundedRectangle(cornerRadius: size * 0.22))
        } else {
            Image(systemName: "app.dashed")
                .font(.system(size: size * 0.8))
                .foregroundStyle(.secondary)
                .frame(width: size, height: size)
        }
    }

    /// bundleID から実行ファイルのアイコンを解決する（未インストール等で見つからなければ nil）
    private static func appIcon(bundleID: String?) -> NSImage? {
        guard let bundleID, !bundleID.isEmpty,
              let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID)
        else { return nil }
        return NSWorkspace.shared.icon(forFile: url.path)
    }

    /// 相対時刻フォーマッタ（「3 分前」等・日本語短縮表記）
    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.locale = Locale(identifier: "ja_JP")
        f.unitsStyle = .short
        return f
    }()

    /// 累計節約時間を「X 時間 Y 分」等に整形する（StatsTab と同一表記）
    private var formattedSaved: String {
        let total = Int(stats.savedSeconds.rounded())
        if total >= 3600 { return "\(total / 3600) 時間 \((total % 3600) / 60) 分" }
        if total >= 60 { return "\(total / 60) 分 \(total % 60) 秒" }
        return "\(total) 秒"
    }

    /// 録音秒数を短く整形する（サマリー補足用）
    private func formattedShort(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        if total >= 3600 { return "\(total / 3600)時間\((total % 3600) / 60)分" }
        if total >= 60 { return "\(total / 60)分" }
        return "\(total)秒"
    }
}
