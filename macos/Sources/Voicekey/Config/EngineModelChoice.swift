//
//  EngineModelChoice.swift
//  文字起こしエンジンとモデルを「1 つのポップアップ」で選ぶための選択肢の組み立て（純ロジック）
//
//  2026-10-19 ユーザー指示「API プロバイダーを一回選んでから下で実際のモデルを選んでいる。
//  1 個のポップアップで、まずプロバイダーを選んでからすぐにモデルを選べるようにして」への対応。
//  以前は「エンジン」と「モデル」の 2 つの Picker で、エンジンを変えると推奨モデルへ黙って
//  切り替わる分かりにくさもあった。1 つのポップアップで (エンジン, モデル) の組を一度に決める。
//
//  SwiftUI に依存させない（並び・表示名の回帰をユニットテストで固定するため）。
//

import Foundation

/// ポップアップの選択肢 1 件（表示名と、選んだときの値）
struct ChoiceMenuOption<Value: Hashable>: Identifiable, Equatable {
    /// 選んだときの値（ForEach の識別子も兼ねる）
    let id: Value
    /// メニューの中に出す名前
    let label: String
}

/// ポップアップの見出し付きまとまり（提供元ごと）
///
/// `title` が nil のまとまりは見出しを付けず、項目だけを区切り線で並べる
/// （選択肢が 1 つしか無い提供元に見出しを付けると、見出しと項目が同じ名前で 2 行並んで冗長なため）。
struct ChoiceMenuGroup<Value: Hashable>: Identifiable, Equatable {
    /// 見出し（nil なら見出しなし）
    let title: String?
    let options: [ChoiceMenuOption<Value>]

    /// ForEach 用の識別子（見出しが無いまとまりは先頭の項目名で区別する）
    var id: String { title ?? ("#" + (options.first?.label ?? "")) }
}

/// 文字起こしのエンジンとモデルの組（録音キーのポップアップの選択値）
struct EngineModelChoice: Hashable {
    let backend: Backend
    let model: String
}

/// 録音キーの「文字起こしエンジン」ポップアップの中身を作る
enum EngineModelCatalog {

    /// 提供元ごとのまとまり（並びは `Backend.selectableCases` のおすすめ順。同じ提供元は 1 つにまとめる）
    ///
    /// - 選択肢が 2 つ以上ある提供元（OpenAI の通常／ライブ・ElevenLabs の v2／v1）は見出しの下にモデル名を並べる
    /// - 選択肢が 1 つの提供元は見出しを付けず「提供元 · モデル」の 1 項目にする（Apple ローカルはエンジン名だけ）
    /// - 保存済みのモデルが一覧に無い（自由入力・旧モデル）ときも、選択中と分かるよう該当の提供元に足す
    ///
    /// - Parameters:
    ///   - current: いま選ばれている組（一覧外のモデルを残すために使う）
    ///   - selectable: 選べるエンジン（テストから差し替える）
    static func groups(
        current: EngineModelChoice, selectable: [Backend] = Backend.selectableCases
    ) -> [ChoiceMenuGroup<EngineModelChoice>] {
        // 提供元の出現順を保ったまま、提供元ごとに (エンジン, モデル) を集める
        var providers: [String] = []
        var entries: [String: [EngineModelChoice]] = [:]
        for backend in selectable {
            var models = backend.knownModels
            // 一覧外の保存値は、そのエンジンの最後に足して選択状態のまま見せる
            if current.backend == backend, !models.contains(current.model), !current.model.isEmpty {
                models.append(current.model)
            }
            let provider = backend.providerName
            if entries[provider] == nil {
                providers.append(provider)
                entries[provider] = []
            }
            entries[provider]?.append(contentsOf: models.map { EngineModelChoice(backend: backend, model: $0) })
        }

        return providers.map { provider in
            let choices = entries[provider] ?? []
            if choices.count == 1, let only = choices.first {
                return ChoiceMenuGroup(title: nil, options: [.init(id: only, label: title(only))])
            }
            return ChoiceMenuGroup(
                title: provider,
                options: choices.map { .init(id: $0, label: modelLabel($0)) }
            )
        }
    }

    /// 閉じたポップアップに出す名前（実プロバイダー名 · モデル名。Apple ローカルはエンジン名だけ）
    static func title(_ choice: EngineModelChoice) -> String {
        if choice.backend == .appleLocal { return choice.backend.label }
        return "\(choice.backend.providerName) · \(choice.model)"
    }

    /// 見出しの下に並べるモデル名（同じエンジンにモデルが複数あるときだけ既定に「（推奨）」）
    static func modelLabel(_ choice: EngineModelChoice) -> String {
        let backend = choice.backend
        var label = choice.model
        if backend.knownModels.count > 1, choice.model == backend.defaultModel {
            label += "（推奨）"
        }
        // 同じ OpenAI の見出しの下で、録音後に送る方式と話しながら送る方式を見分けられるようにする
        if backend == .openaiLive { label += "（ライブ）" }
        return label
    }
}
