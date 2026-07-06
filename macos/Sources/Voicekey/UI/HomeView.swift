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

/// ホームの「マイクテスト」カード用モデル（録音せずレベルだけを流すモニタ＋デバイス選択）。
/// オンボーディングのマイクテストと同じ AppController のモニタ機構を借りる。
@MainActor
final class HomeMicTestModel: ObservableObject {
    @Published var running = false
    @Published var level: Float = 0
    @Published var devices: [AudioInputDevice] = []
    @Published var deviceUID: String = ""
    /// 観測はせず（頻繁な録音更新で再描画しないため）参照だけ持つ。
    weak var controller: AppController?

    func configure(_ c: AppController?) {
        controller = c
        deviceUID = c?.config.inputDeviceUID ?? ""
    }

    func start() {
        devices = AudioDevices.inputDevices()
        deviceUID = controller?.config.inputDeviceUID ?? ""
        running = true
        controller?.startMicMonitor { [weak self] lv in self?.level = lv }
    }

    func stop() {
        guard running else { return }
        running = false
        level = 0
        controller?.stopMicMonitor()
    }

    func selectDevice(_ uid: String) {
        deviceUID = uid
        controller?.setMicTestDevice(uid, monitoring: running)
    }
}

struct HomeView: View {
    @ObservedObject var config: ConfigStore
    @ObservedObject var history: HistoryStore
    @ObservedObject var stats: StatsStore
    @ObservedObject var updater: UpdaterController
    /// マイクテスト・ガイド再表示に使う（観測しない＝録音のたびの再描画を避けるため plain 参照）。
    var controller: AppController?
    /// 「セットアップガイド」カードから初回ガイドを再表示する。
    var onShowOnboarding: () -> Void = {}

    /// ホームのマイクテスト（デバイス選択＋ミニレベルメーター）。
    @StateObject private var micTest = HomeMicTestModel()

    /// 直近にコピーした履歴エントリ（行に「コピーしました」を一時表示する）
    @State private var copiedId: UUID?

    /// 期間カードの選択（1=今日 / 7=今週）。UserDefaults に保存し、次回起動でも維持する。
    @AppStorage("home.periodDays") private var periodDays: Int = 1

    var body: some View {
        // レイアウト v2.1: 島で全面を包まない。MainWindowView の frosted backdrop の上に
        // 控えめなカードをフラットに敷く（大きな島で画面を分割しない）。
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                statsCard
                toolsSection
                appUsageSection
                historySection
            }
            .padding(20)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear { micTest.configure(controller) }
        .onDisappear { micTest.stop() }
    }

    // MARK: - ツール（マイクテスト・セットアップガイド）

    /// マイクテストとセットアップガイド再表示の 2 カードを横並びで置く。
    private var toolsSection: some View {
        HStack(alignment: .top, spacing: 12) {
            micTestCard
            guideCard
        }
    }

    /// マイクテストカード（デバイス選択＋「テスト」でミニレベルメーターが動く。録音しない）。
    private var micTestCard: some View {
        dashCard {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Label("マイクテスト", systemImage: "mic.fill").font(.subheadline.weight(.semibold))
                    Spacer()
                    Button(micTest.running ? "停止" : "テスト") {
                        micTest.running ? micTest.stop() : micTest.start()
                    }
                    .buttonStyle(.borderless)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.tint)
                }
                Text(micTest.running ? "話すと、下のバーが動きます。" : "「テスト」を押して、マイクが拾えているか確かめます。")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                homeMiniMeter
                // デバイス選択（システム既定＋接続中のマイク）
                Picker("", selection: Binding(get: { micTest.deviceUID }, set: { micTest.selectDevice($0) })) {
                    Text("システム既定").tag("")
                    ForEach(micTest.devices) { d in Text(d.name).tag(d.uid) }
                    if !micTest.deviceUID.isEmpty,
                       !micTest.devices.contains(where: { $0.uid == micTest.deviceUID }) {
                        Text("（未接続のデバイス）").tag(micTest.deviceUID)
                    }
                }
                .labelsHidden()
                .onAppear { if micTest.devices.isEmpty { micTest.devices = AudioDevices.inputDevices() } }
            }
        }
    }

    /// ホームのミニレベルメーター（テスト中のみ反応。停止中はグレーのまま）。
    private var homeMiniMeter: some View {
        let bars = 20
        return HStack(spacing: 3) {
            ForEach(0..<bars, id: \.self) { i in
                let threshold = Float(i) / Float(bars)
                let on = micTest.running && micTest.level >= threshold + 0.02
                Capsule()
                    .fill(on ? Color.accentColor : Color.primary.opacity(0.12))
                    .frame(height: 14)
                    .animation(.easeOut(duration: 0.08), value: micTest.level)
            }
        }
        .frame(maxWidth: .infinity)
    }

    /// セットアップガイドカード（もう一度見る→ガイドを再表示）。
    private var guideCard: some View {
        dashCard {
            VStack(alignment: .leading, spacing: 10) {
                Label("セットアップガイド", systemImage: "sparkles").font(.subheadline.weight(.semibold))
                Text("使い方をはじめから、もう一度見られます。")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button("もう一度見る") { onShowOnboarding() }
                    .buttonStyle(.borderless)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.tint)
            }
        }
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

    /// 統計セクション。上段に「累計文字数」「節約できた時間」を横並び、
    /// 下段に期間切替つきの「期間カード」を全幅で置く 2 段構成。
    /// 主役の指標（文字数・節約時間・期間文字数）を大きく見せ、回数や録音時間などの
    /// 補足は小さく下へ回す（ユーザー指示: 重要でないものは小さく下に）。
    private var statsCard: some View {
        VStack(spacing: 12) {
            // 上段: 累計と節約は横並び。高さが揃うよう alignment .top（dashCard 側で伸長）。
            HStack(alignment: .top, spacing: 12) {
                cumulativeCard
                savedCard
            }
            // 下段: 期間カードはセグメント切替を右上に置くため全幅で 1 枚に。
            periodCard
        }
    }

    /// カード「累計文字数」。主役に累計文字数、補足に回数・録音時間、
    /// 下部にレベル制のゲーミフィケーション（進捗バー・次レベルまで）を統合する。
    private var cumulativeCard: some View {
        dashCard {
            VStack(alignment: .leading, spacing: 8) {
                Text("累計入力").font(.caption).foregroundStyle(.secondary)
                // 主役: 累計文字数（カンマ区切り・等幅数字で堂々と）
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    bigNumberText(grouped(stats.totalCharacters))
                    Text("文字").font(.subheadline).foregroundStyle(.secondary)
                }
                // 補足: 累計回数・累計録音時間（重要度が低いので小さく）
                HStack(spacing: 12) {
                    Label("\(grouped(stats.totalSessions)) 回", systemImage: "mic.fill")
                    Label(formattedDuration(stats.totalRecordingSeconds), systemImage: "waveform")
                }
                .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                Divider().padding(.top, 2)
                // 遊び要素: 累計文字数を経験値としたレベル制（続けたくなる仕掛けを残す）
                VStack(alignment: .leading, spacing: 5) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("レベル \(stats.level)").font(.subheadline.weight(.semibold))
                        Spacer(minLength: 6)
                        Text("あと \(grouped(stats.xpToNextLevel)) 文字で Lv.\(stats.level + 1)")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    ProgressView(value: stats.levelProgress)
                }
            }
        }
    }

    /// カード「節約できた時間」。主役に推定節約時間、下に身近なものへの換算を 1 行添える
    /// （遊び要素: 到達した最大のマイルストーンを自動選択して個数を出す）。
    private var savedCard: some View {
        dashCard {
            VStack(alignment: .leading, spacing: 8) {
                Text("節約できた時間").font(.caption).foregroundStyle(.secondary)
                // 主役: 推定節約時間（時間表記で桁が伸びやすいので少し小さめに）
                bigNumberText(formattedDuration(stats.savedSeconds), size: 28)
                // 実感できる換算の一言。SF Symbols アイコンで彩る（絵文字は使わない）
                Label(savedComparison.text, systemImage: savedComparison.symbol)
                    .font(.caption).foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// カード「期間」。右上のセグメントで今日 / 今週を切り替える（選択は UserDefaults に保存）。
    /// 主役は期間内の入力文字数、補足に録音時間・回数。切替時は数値がロールする。
    private var periodCard: some View {
        dashCard {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("この期間").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    // 今日=直近1日 / 今週=直近7日（既存 charactersInLast の日数と対応）
                    Picker("期間", selection: $periodDays) {
                        Text("今日").tag(1)
                        Text("今週").tag(7)
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .fixedSize()  // 内容幅に収め、右上のコンパクトなトグルにする
                }
                // 主役: 期間内の入力文字数（切替でロールするよう numericText を付与）
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    bigNumberText(grouped(stats.charactersInLast(days: periodDays)))
                        .contentTransition(.numericText())
                    Text("文字").font(.subheadline).foregroundStyle(.secondary)
                }
                // 補足: 期間内の録音時間・回数
                HStack(spacing: 14) {
                    Label(formattedDuration(stats.recordingSecondsInLast(days: periodDays)), systemImage: "waveform")
                        .contentTransition(.numericText())
                    Label("\(grouped(stats.sessionsInLast(days: periodDays))) 回", systemImage: "mic.fill")
                        .contentTransition(.numericText())
                }
                .font(.caption).foregroundStyle(.secondary)
            }
            // periodDays の変化を 1 つのトランザクションにまとめ、numericText を滑らかに走らせる
            .animation(.snappy, value: periodDays)
        }
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

    /// 最近の履歴（直近 8 件・行クリックでコピー）。
    /// 手動の全削除は置かない（履歴は上限件数を超えると古い順に自動で消える。
    /// 統計は履歴と独立した累計のため、履歴が消えても減らない）。
    private var historySection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("最近の履歴").font(.headline)
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

    /// 秒数を「X時間Y分」等に整形する（節約時間の主役表示・録音時間の補足で共用）。
    /// 桁が伸びすぎないよう分・秒単位で丸め、カードに収まる短さに保つ。
    private func formattedDuration(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        if total >= 3600 { return "\(total / 3600)時間\((total % 3600) / 60)分" }
        if total >= 60 { return "\(total / 60)分\(total % 60)秒" }
        return "\(total)秒"
    }

    /// 3 桁区切り（カンマ）の数値文字列。主役の大きな数字を読みやすくする。
    private static let groupingFormatter: NumberFormatter = {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.groupingSeparator = ","
        return f
    }()
    private func grouped(_ n: Int) -> String {
        Self.groupingFormatter.string(from: NSNumber(value: n)) ?? "\(n)"
    }

    /// 主役の大きな数字の共通スタイル（太字・角丸・等幅数字）。
    /// 桁あふれ時は縮小して 1 行に収める（0 件でも「0」で自然に表示される）。
    private func bigNumberText(_ text: String, size: CGFloat = 30) -> some View {
        Text(text)
            .font(.system(size: size, weight: .bold, design: .rounded))
            .monospacedDigit()
            .lineLimit(1)
            .minimumScaleFactor(0.55)
    }

    /// カードの共通コンテナ（既存スタイル: 角丸 12＋控えめなフィル）。
    /// maxHeight を伸ばして横並びカード同士の高さを揃え、内容は左上に寄せる。
    private func dashCard<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        content()
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .padding(16)
            .background(RoundedRectangle(cornerRadius: 12).fill(fillColor))
    }

    /// 節約時間を身近なものへ換算した一言（遊び要素）。
    /// 到達した最大のマイルストーンを選び、その個数を添える。段階は monotonic に大きくなる。
    /// 3 分未満（新規ユーザー含む）は換算せず前向きな一言を返す＝0 でも破綻しない。
    private var savedComparison: (text: String, symbol: String) {
        let s = stats.savedSeconds
        if s >= 86400 {                       // まる 1 日（24 時間）
            return ("まるっと \(Int(s / 86400)) 日ぶんの時間", "sun.max.fill")
        }
        if s >= 28800 {                       // ぐっすり睡眠（8 時間）
            return ("ぐっすり睡眠 \(Int(s / 28800)) 回ぶん", "bed.double.fill")
        }
        if s >= 7200 {                        // 映画（2 時間）
            return ("映画 \(Int(s / 7200)) 本ぶんの尺", "film.fill")
        }
        if s >= 1800 {                        // 通勤（片道 30 分）
            return ("通勤 \(Int(s / 1800)) 回ぶんの移動時間", "tram.fill")
        }
        if s >= 180 {                         // カップ麺（3 分）
            return ("カップ麺 \(Int(s / 180)) 個ぶんの待ち時間", "cup.and.saucer.fill")
        }
        return ("これから時間が積み上がっていきます", "hourglass")
    }
}
