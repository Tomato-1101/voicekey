//
//  GroupedChoiceMenu.swift
//  「提供元の見出し → その下のモデル」を 1 つのポップアップで選ぶ設定行の部品
//
//  文字起こしエンジン（録音キー）・ライブ字幕の翻訳・翻訳して入力で共通に使う。
//  以前は「エンジン」と「モデル」の 2 つのポップアップを順に選ぶ作りで、エンジンを選び直すたびに
//  モデルが黙って切り替わり、何が使われるのか分かりにくかった（2026-10-19 ユーザー指示で 1 つに統合）。
//
//  閉じているときは「提供元 · モデル」を出し、開くと提供元ごとの見出しの下にモデルが並ぶ。
//  選んだ項目にはチェックが付く（Menu の中の Toggle はメニュー標準のチェック項目になる）。
//

import SwiftUI

struct GroupedChoiceMenu<Value: Hashable>: View {
    /// 提供元ごとのまとまり（見出しなしのまとまりは項目だけを並べる）
    let groups: [ChoiceMenuGroup<Value>]
    @Binding var selection: Value
    /// 閉じているときに出す名前（例: 「Soniox · stt-rt-v5」）
    let currentTitle: String

    var body: some View {
        Menu {
            ForEach(Array(groups.enumerated()), id: \.offset) { index, group in
                // まとまりの間は区切り線で分ける（見出し付きは Section が自分で区切る）
                if index > 0, group.title == nil, groups[index - 1].title == nil {
                    Divider()
                }
                if let title = group.title {
                    Section(title) { items(group) }
                } else {
                    items(group)
                }
            }
        } label: {
            // ポップアップと分かるように、上下の山形を右端に付ける（メニュー標準の矢印は隠す）
            HStack(spacing: 8) {
                Text(currentTitle)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
        }
        .menuIndicator(.hidden)
        // 設定画面は行全体スイッチ（RowSwitchToggleStyle）を全体に掛けているので、
        // メニューの中だけは標準（チェック付きの項目）に戻す
        .toggleStyle(.automatic)
        .fixedSize()
    }

    /// まとまり 1 つ分の項目（選択中にチェックが付く）
    @ViewBuilder
    private func items(_ group: ChoiceMenuGroup<Value>) -> some View {
        ForEach(group.options) { option in
            Toggle(option.label, isOn: Binding(
                get: { selection == option.id },
                // チェックを外す操作（選択中をもう一度選ぶ）は無視して、選択を空にしない
                set: { isOn in if isOn { selection = option.id } }
            ))
        }
    }
}
