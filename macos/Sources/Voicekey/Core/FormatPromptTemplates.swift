//
//  FormatPromptTemplates.swift
//  整形プロンプトのひな形（設定 › プロンプト の「ひな形から始める」）
//
//  2026-10-19 ユーザー指示「プロンプトは共有プロンプトで、左のタブにプロンプトタブを追加して
//  そこで編集できるようにする」に合わせて、整形の指示を 1 か所（`ConfigStore.autoFormatPrompt`）に
//  まとめた。
//
//  それまで録音キーごとに選べた「整え方」（標準／そのまま／すっきり／箇条書き）は、サーバー整形
//  （旧製品版のログイン時だけ）に preset_id を渡すだけで、自前のキーで整形する personal 版では
//  何も効いていなかった（クライアント整形は常に共有の指示文を使う）。そこで 4 種を「指示文のひな形」
//  に作り直し、選ぶと共有の指示文がその内容に置き換わるようにした（実際に効く形にする）。
//
//  標準は `TextFormatter.defaultPrompt` そのもの。ほかの 3 種は、既定文の共通ルール部分を
//  そのまま使い、末尾の「追加で許可される操作」だけを差し替える（共通ルールの改善が自動で揃う）。
//

import Foundation

/// 整形の指示文のひな形 1 件
struct FormatPromptTemplate: Identifiable, Equatable {
    /// サーバーの preset_id と同じ値（standard / punctuation / clean / bullets）
    let id: String
    /// メニューに出す名前
    let title: String
    /// 一言説明
    let summary: String
    /// 指示文の本文（共通フッターは `TextFormatter.systemPrompt` が自動で足す）
    let prompt: String
}

enum FormatPromptTemplates {

    /// 既定文の「追加で許可される操作」の見出し（ここから後ろをひな形ごとに差し替える）
    static let presetMarker = "このプリセットで追加で許可される操作:"

    /// 既定文の共通ルール部分（見出しの手前まで。見出しが見つからなければ既定文全体）
    static var commonPart: String {
        let base = TextFormatter.defaultPrompt
        guard let range = base.range(of: presetMarker) else { return base + "\n\n" }
        return String(base[..<range.lowerBound])
    }

    /// ひな形の一覧（並びがそのままメニューの表示順）
    static var all: [FormatPromptTemplate] {
        [
            FormatPromptTemplate(
                id: "standard",
                title: "標準（言いよどみだけ除去）",
                summary: "「えー」「あの」などの言いよどみだけを消し、話した内容はそのまま残します。",
                prompt: TextFormatter.defaultPrompt
            ),
            FormatPromptTemplate(
                id: "punctuation",
                title: "そのまま（句読点だけ整える）",
                summary: "言葉は一切消さず、句読点・改行だけを整えます。",
                prompt: commonPart + presetMarker + "\n"
                    + "- なし。言いよどみ（えーと、あの など）や言い直し・繰り返しも含めて、言葉は一つも削除しない\n"
                    + "- 句読点・改行・段落の区切りだけを整える"
            ),
            FormatPromptTemplate(
                id: "clean",
                title: "すっきり（言い直しを整理）",
                summary: "言いよどみに加え、言い直し（例: 火曜いや水曜）も最終形に整理します。内容は残します。",
                prompt: commonPart + presetMarker + "\n"
                    + "- フィラー（えーと、あの、えっと、まあ、なんか、um、uh など）は削除してよい\n"
                    + "- 言い直し（例:「火曜、いや水曜」）は、最後に言い直した形だけを残してよい\n"
                    + "- 同じ内容をそのまま繰り返した部分は 1 回にまとめてよい\n"
                    + "- それ以外の言葉はすべて残す"
            ),
            FormatPromptTemplate(
                id: "bullets",
                title: "箇条書き（リストに整理）",
                summary: "列挙や手順を「- 」の箇条書きに整理します。各項目の内容は残します。",
                prompt: commonPart + presetMarker + "\n"
                    + "- フィラー（えーと、あの、えっと、まあ、なんか、um、uh など）は削除してよい\n"
                    + "- 列挙・手順・複数の要点を話している部分は、「- 」で始まる箇条書きに整理してよい\n"
                    + "- 各項目の内容は削らない。箇条書きにならない部分は普通の文章のまま残す"
            ),
        ]
    }

    /// 指示文がどのひな形と同じか（前後の空白は無視。どれとも違えば nil＝自分で書き換えた文）
    static func matching(_ prompt: String) -> FormatPromptTemplate? {
        let trimmed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        return all.first { $0.prompt.trimmingCharacters(in: .whitespacesAndNewlines) == trimmed }
    }
}
