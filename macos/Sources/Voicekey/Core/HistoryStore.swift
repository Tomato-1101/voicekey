//
//  HistoryStore.swift
//  音声入力履歴の保持と永続化
//
//  文字起こし（整形後）のテキストを新しい順に最大 10 件保持し、
//  設定ウィンドウの「履歴」タブから再コピーできるようにする。
//  アプリを再起動しても残るよう Application Support に JSON で保存する。
//

import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "history")

/// 音声入力 1 回分の履歴エントリ
struct HistoryEntry: Codable, Identifiable, Equatable {
    let id: UUID
    let text: String
    let date: Date
}

/// 音声入力履歴のストア。
/// 追加・消去のたびに自動保存される（設定 UI が @Published を購読して即時反映）。
@MainActor
final class HistoryStore: ObservableObject {

    /// 保持する最大件数（超過分は古いものから捨てる）
    static let maxItems = 10

    /// 履歴（新しい順）
    @Published private(set) var items: [HistoryEntry] = []

    private let fileURL: URL

    init() {
        // ~/Library/Application Support/voicekey/history.json
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("voicekey", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        fileURL = dir.appendingPathComponent("history.json")

        if let data = try? Data(contentsOf: fileURL) {
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            if let loaded = try? decoder.decode([HistoryEntry].self, from: data) {
                items = Array(loaded.prefix(Self.maxItems))
            }
        }
    }

    /// 履歴に 1 件追加する（空テキストは無視）
    func add(_ text: String) {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        items.insert(HistoryEntry(id: UUID(), text: text, date: Date()), at: 0)
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
