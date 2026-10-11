//
//  LLMModelPicker.swift
//  整形・翻訳して入力・字幕翻訳で共通の LLM モデル選択行
//
//  選択肢は `LLMModelCatalog`（おすすめを先頭に「（おすすめ）」付き）。選んだモデルの一言説明を
//  項目名の下に添える（他の設定行と同じ SettingRowLabel の作り）。開いたときに実在一覧を取り直す。
//

import SwiftUI

struct LLMModelPicker: View {
    let title: String
    let provider: LLMProvider
    @Binding var selection: String

    /// API から取った実在一覧（未取得なら nil＝静的一覧だけで出す）
    @State private var available: [String]?

    var body: some View {
        Picker(selection: $selection) {
            ForEach(LLMModelCatalog.options(provider, current: selection, available: available)) { option in
                Text(option.label).tag(option.id)
            }
        } label: {
            SettingRowLabel(
                title: title,
                detail: LLMModelCatalog.detail(provider, id: selection, available: available)
            )
        }
        .task {
            // キャッシュで先に描き、取り直せたら差し替える（一覧 GET は無料。キー探索は非メインで行う）
            available = LLMModelCatalog.cachedAvailable(provider)
            await LLMModelCatalog.refreshAvailable(provider, force: true)
            available = LLMModelCatalog.cachedAvailable(provider)
        }
    }
}
