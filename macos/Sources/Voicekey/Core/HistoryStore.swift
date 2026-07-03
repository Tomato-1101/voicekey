//
//  HistoryStore.swift
//  音声入力履歴の保持と永続化
//
//  文字起こし（整形後）のテキストを新しい順に最大 200 件保持し、設定・ホーム・
//  サイドノッチから再コピー/再貼り付けできるようにする。各エントリには貼り付け先
//  アプリ（bundleID・名前）と文字数のメタデータを持たせる。
//  アプリを再起動しても残るよう Application Support に JSON で保存する。
//

import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "history")

/// 音声入力 1 回分の履歴エントリ。
/// appBundleID / appName は貼り付け先アプリ（旧データには無いので Optional）、
/// characters は出力文字数（旧データには無いので読み込み時に text から補完する）。
struct HistoryItem: Codable, Identifiable, Equatable {
    let id: UUID
    let text: String
    let date: Date
    let appBundleID: String?
    let appName: String?
    let characters: Int
}

extension HistoryItem {
    /// 旧 JSON（appBundleID / appName / characters が無い形式）からも読めるよう
    /// 全項目を decodeIfPresent で読む。本体宣言内に書くと memberwise init が消えるため extension に置く。
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(UUID.self, forKey: .id) ?? UUID()
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        date = try c.decodeIfPresent(Date.self, forKey: .date) ?? Date()
        appBundleID = try c.decodeIfPresent(String.self, forKey: .appBundleID)
        appName = try c.decodeIfPresent(String.self, forKey: .appName)
        // 旧データは文字数を持たないので text の文字数で補完する
        let decodedText = text
        characters = try c.decodeIfPresent(Int.self, forKey: .characters) ?? decodedText.count
    }
}

/// 音声入力履歴のストア。
/// 追加・消去のたびに自動保存される（設定 UI が @Published を購読して即時反映）。
@MainActor
final class HistoryStore: ObservableObject {

    /// 保持する最大件数（超過分は古いものから捨てる）
    static let maxItems = 200

    /// 履歴（新しい順）
    @Published private(set) var items: [HistoryItem] = []

    private let fileURL: URL

    /// - Parameter directory: 保存先ディレクトリ（テスト時に一時ディレクトリを注入する）。
    ///   nil なら本番の ~/Library/Application Support/voicekey を使う。
    init(directory: URL? = nil) {
        let dir = directory ?? FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("voicekey", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        fileURL = dir.appendingPathComponent("history.json")
        items = Self.load(from: fileURL)
    }

    /// 保存済み JSON を読み込む。新形式（構造体配列）を第一に試し、読めなければ
    /// 旧・単純な文字列配列としても試す。どちらでも読めなければ空で開始（クラッシュしない）。
    private static func load(from fileURL: URL) -> [HistoryItem] {
        guard let data = try? Data(contentsOf: fileURL) else { return [] }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        // 新形式（[HistoryItem]）。HistoryItem.init(from:) が旧「id/text/date のみ」も吸収する
        if let loaded = try? decoder.decode([HistoryItem].self, from: data) {
            return Array(loaded.prefix(maxItems))
        }
        // さらに古い「文字列配列」形式のフォールバック（Windows 版由来の互換）
        if let strings = try? decoder.decode([String].self, from: data) {
            let items = strings.prefix(maxItems).map {
                HistoryItem(id: UUID(), text: $0, date: Date(),
                            appBundleID: nil, appName: nil, characters: $0.count)
            }
            return Array(items)
        }
        return []
    }

    /// 履歴に 1 件追加する（空テキストは無視）。貼り付け先アプリのメタデータを付ける。
    func add(_ text: String, appBundleID: String? = nil, appName: String? = nil) {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        let item = HistoryItem(
            id: UUID(), text: text, date: Date(),
            appBundleID: appBundleID, appName: appName, characters: text.count
        )
        items.insert(item, at: 0)
        if items.count > Self.maxItems {
            items.removeLast(items.count - Self.maxItems)
        }
        save()
    }

    /// 履歴をすべて消去する（発話内容をディスクに残したくないとき用）
    func clear() {
        items = []
        save()
    }

    private func save() {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        do {
            let data = try encoder.encode(items)
            try data.write(to: fileURL, options: [.atomic])
        } catch {
            log.error("履歴の保存に失敗: \(error.localizedDescription)")
        }
    }
}
