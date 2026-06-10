//
//  AppConfig.swift
//  設定モデルと永続化（UserDefaults）
//
//  Python 版は settings.yaml + ホットリロードだったが、Mac ネイティブ版は
//  UserDefaults に永続化し、設定 UI から直接 ConfigStore を更新する
//  （ファイル監視そのものが不要になる）。
//

import Combine
import Foundation

/// ホットキーの動作モード
enum HotkeyMode: String, Codable, CaseIterable, Identifiable {
    /// 押している間だけ録音
    case hold
    /// 1 回押して開始、もう 1 回押して停止
    case toggle

    var id: String { rawValue }

    var label: String {
        switch self {
        case .hold: return "押している間"
        case .toggle: return "トグル"
        }
    }
}

/// 文字起こしバックエンド
enum Backend: String, Codable, CaseIterable, Identifiable {
    case openai
    case groq
    case elevenlabs
    case deepgram

    var id: String { rawValue }

    var label: String {
        switch self {
        case .openai: return "OpenAI"
        case .groq: return "Groq"
        case .elevenlabs: return "ElevenLabs"
        case .deepgram: return "Deepgram"
        }
    }

    /// 既知のモデル一覧（設定 UI の候補。自由入力も可）。先頭が既定。
    var knownModels: [String] {
        switch self {
        case .openai: return ["gpt-4o-transcribe", "gpt-4o-mini-transcribe"]
        case .groq: return ["whisper-large-v3-turbo", "whisper-large-v3"]
        // scribe_v2 が最新。ただし日本語長文は scribe_v1 の方が高精度なケースもある
        case .elevenlabs: return ["scribe_v2", "scribe_v1", "scribe_v1_experimental"]
        // nova-3 がベンチで速度・精度とも最良（ストリーミング既定）。ja は多言語モードで対応
        case .deepgram: return ["nova-3", "nova-2"]
        }
    }

    /// 既定モデル
    var defaultModel: String { knownModels[0] }
}

/// ホットキースロット 1 つ分の設定
struct SlotConfig: Codable, Equatable {
    /// ホットキーを構成するキートークン（例: ["cmd_r"], ["ctrl_l", "space"]）
    var hotkey: [String]
    var mode: HotkeyMode
    var backend: Backend
    var model: String
    var prompt: String

    /// 人間が読める表記（例: "右⌘"、"⌃+Space"）
    var hotkeyLabel: String {
        hotkey.isEmpty ? "未設定" : hotkey.map { KeyToken.displayName($0) }.joined(separator: "+")
    }
}

/// アプリ設定の永続化ストア。
/// すべての設定変更は @Published プロパティ経由で行い、変更時に自動保存される。
@MainActor
final class ConfigStore: ObservableObject {

    /// スロット 1（既定: 右⌘ 長押し → OpenAI gpt-4o-transcribe）
    @Published var slot1: SlotConfig
    /// スロット 2（既定: 右⌥ 長押し → Groq whisper-large-v3-turbo）
    @Published var slot2: SlotConfig
    /// 言語コード（"ja" など。空なら API 側の自動判定）
    @Published var language: String
    /// VAD（発話検出）による無音スキップ・トリミングを行うか
    @Published var vadEnabled: Bool
    /// 録音 HUD を表示するか
    @Published var hudEnabled: Bool
    /// Deepgram でリアルタイムストリーミング（ライブ字幕）を使うか
    @Published var streamingEnabled: Bool
    /// ダブルタップ自動 Enter: テキスト挿入から Enter 送信までの待機（ミリ秒）
    @Published var autoEnterDelayMs: Int
    /// 録音に使う入力デバイスの UID（空ならシステム既定マイク）
    @Published var inputDeviceUID: String

    private var cancellables: Set<AnyCancellable> = []
    private let defaults: UserDefaults

    private enum Keys {
        static let slot1 = "slot1"
        static let slot2 = "slot2"
        static let language = "language"
        static let vadEnabled = "vadEnabled"
        static let hudEnabled = "hudEnabled"
        static let streamingEnabled = "streamingEnabled"
        static let autoEnterDelayMs = "autoEnterDelayMs"
        static let inputDeviceUID = "inputDeviceUID"
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults

        slot1 = Self.loadSlot(defaults, key: Keys.slot1) ?? SlotConfig(
            hotkey: ["cmd_r"], mode: .hold, backend: .openai,
            model: Backend.openai.defaultModel, prompt: ""
        )
        slot2 = Self.loadSlot(defaults, key: Keys.slot2) ?? SlotConfig(
            hotkey: ["alt_r"], mode: .hold, backend: .groq,
            model: Backend.groq.defaultModel, prompt: ""
        )
        language = defaults.string(forKey: Keys.language) ?? "ja"
        vadEnabled = defaults.object(forKey: Keys.vadEnabled) as? Bool ?? true
        hudEnabled = defaults.object(forKey: Keys.hudEnabled) as? Bool ?? true
        streamingEnabled = defaults.object(forKey: Keys.streamingEnabled) as? Bool ?? true
        autoEnterDelayMs = defaults.object(forKey: Keys.autoEnterDelayMs) as? Int ?? 50
        inputDeviceUID = defaults.string(forKey: Keys.inputDeviceUID) ?? ""

        // 変更を自動保存（起動直後の初期代入は上で完了しているため安全）
        $slot1.dropFirst().sink { [weak self] in self?.saveSlot($0, key: Keys.slot1) }.store(in: &cancellables)
        $slot2.dropFirst().sink { [weak self] in self?.saveSlot($0, key: Keys.slot2) }.store(in: &cancellables)
        $language.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.language) }.store(in: &cancellables)
        $vadEnabled.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.vadEnabled) }.store(in: &cancellables)
        $hudEnabled.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.hudEnabled) }.store(in: &cancellables)
        $streamingEnabled.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.streamingEnabled) }.store(in: &cancellables)
        $autoEnterDelayMs.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.autoEnterDelayMs) }.store(in: &cancellables)
        $inputDeviceUID.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.inputDeviceUID) }.store(in: &cancellables)
    }

    /// スロット設定を ID で取得する
    func slot(_ id: Int) -> SlotConfig {
        id == 1 ? slot1 : slot2
    }

    private static func loadSlot(_ defaults: UserDefaults, key: String) -> SlotConfig? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(SlotConfig.self, from: data)
    }

    private func saveSlot(_ slot: SlotConfig, key: String) {
        if let data = try? JSONEncoder().encode(slot) {
            defaults.set(data, forKey: key)
        }
    }
}
