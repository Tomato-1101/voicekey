//
//  SettingsView.swift
//  設定ウィンドウ（一般 / ホットキー / API キー）
//

import ServiceManagement
import SwiftUI

struct SettingsView: View {
    @ObservedObject var config: ConfigStore
    @State private var selectedTab: Int

    init(config: ConfigStore, initialTab: Int = 0) {
        self.config = config
        _selectedTab = State(initialValue: initialTab)
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            GeneralSettingsTab(config: config)
                .tabItem { Label("一般", systemImage: "gearshape") }
                .tag(0)
            SlotSettingsTab(title: "ホットキー 1", slot: $config.slot1)
                .tabItem { Label("ホットキー 1", systemImage: "1.circle") }
                .tag(1)
            SlotSettingsTab(title: "ホットキー 2", slot: $config.slot2)
                .tabItem { Label("ホットキー 2", systemImage: "2.circle") }
                .tag(2)
            ApiKeysTab()
                .tabItem { Label("API キー", systemImage: "key") }
                .tag(3)
        }
        // fixedSize() だと NSHostingController 上で高さが潰れて
        // 入力欄が描画されないことがあるため、明示サイズを与える
        .frame(width: 480, height: 520)
    }
}

// MARK: - 一般

private struct GeneralSettingsTab: View {
    @ObservedObject var config: ConfigStore
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled
    /// 入力デバイス一覧（開いたタイミングと更新ボタンで読み直す）
    @State private var inputDevices: [AudioInputDevice] = AudioDevices.inputDevices()

    var body: some View {
        Form {
            Picker("言語", selection: $config.language) {
                Text("日本語").tag("ja")
                Text("英語").tag("en")
                Text("自動判定").tag("")
            }

            LabeledContent("入力デバイス") {
                HStack(spacing: 8) {
                    Picker("", selection: $config.inputDeviceUID) {
                        Text("システム既定").tag("")
                        ForEach(inputDevices) { device in
                            Text(device.name).tag(device.uid)
                        }
                        // 保存済みデバイスが現在見つからない場合も選択を保持して表示する
                        if !config.inputDeviceUID.isEmpty,
                           !inputDevices.contains(where: { $0.uid == config.inputDeviceUID }) {
                            Text("（未接続のデバイス）").tag(config.inputDeviceUID)
                        }
                    }
                    .labelsHidden()
                    Button {
                        inputDevices = AudioDevices.inputDevices()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .help("デバイス一覧を更新")
                }
            }
            .onAppear { inputDevices = AudioDevices.inputDevices() }
            Text("録音に使うマイク。「システム既定」は macOS の設定に従います。")
                .font(.caption)
                .foregroundStyle(.secondary)

            Toggle("無音を自動スキップ（VAD）", isOn: $config.vadEnabled)
            Text("発話が検出されない録音を API に送らず、幻覚と無駄なコストを防ぎます。")
                .font(.caption)
                .foregroundStyle(.secondary)

            Toggle("録音中に HUD を表示", isOn: $config.hudEnabled)

            Toggle("リアルタイムストリーミング（Deepgram）", isOn: $config.streamingEnabled)
            Text("バックエンドが Deepgram のホットキーで、話しながら HUD に文字を表示し、離した瞬間に確定します。オフにすると従来どおり録音後にまとめて変換します。")
                .font(.caption)
                .foregroundStyle(.secondary)

            LabeledContent("自動 Enter の遅延") {
                HStack {
                    TextField(
                        "",
                        value: $config.autoEnterDelayMs,
                        format: .number
                    )
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 60)
                    .multilineTextAlignment(.trailing)
                    Text("ms")
                }
            }
            Text("ホットキーを素早く 2 回押すと、貼り付け後に Enter を自動送信します（チャット送信用）。")
                .font(.caption)
                .foregroundStyle(.secondary)

            Divider()

            Toggle("ログイン時に起動", isOn: $launchAtLogin)
                .onChange(of: launchAtLogin) { _, enabled in
                    do {
                        if enabled {
                            try SMAppService.mainApp.register()
                        } else {
                            try SMAppService.mainApp.unregister()
                        }
                    } catch {
                        // 開発実行（未バンドル）では失敗するため表示を戻すだけ
                        launchAtLogin = SMAppService.mainApp.status == .enabled
                    }
                }
        }
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }
}

// MARK: - ホットキースロット

private struct SlotSettingsTab: View {
    let title: String
    @Binding var slot: SlotConfig

    var body: some View {
        Form {
            LabeledContent("ホットキー") {
                HotkeyRecorderView(hotkey: $slot.hotkey)
                    .frame(width: 220)
            }

            Picker("動作", selection: $slot.mode) {
                ForEach(HotkeyMode.allCases) { mode in
                    Text(mode.label).tag(mode)
                }
            }
            .pickerStyle(.segmented)

            Picker("バックエンド", selection: $slot.backend) {
                ForEach(Backend.allCases) { backend in
                    Text(backend.label).tag(backend)
                }
            }
            .onChange(of: slot.backend) { _, newBackend in
                // バックエンド変更時はそのバックエンドの既定モデルに切り替える
                if !newBackend.knownModels.contains(slot.model) {
                    slot.model = newBackend.defaultModel
                }
            }

            Picker("モデル", selection: $slot.model) {
                ForEach(slot.backend.knownModels, id: \.self) { model in
                    Text(model).tag(model)
                }
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("プロンプト（任意）")
                TextField("専門用語や固有名詞のヒントを入力", text: $slot.prompt, axis: .vertical)
                    .lineLimit(2...4)
                    .textFieldStyle(.roundedBorder)
                Text("文字起こしのヒント。よく使う固有名詞を書いておくと精度が上がります。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }
}

// MARK: - API キー

private struct ApiKeysTab: View {
    var body: some View {
        Form {
            ForEach(Backend.allCases) { backend in
                ApiKeyRow(backend: backend)
            }
            Text("API キーは macOS のキーチェーンに安全に保存されます。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .formStyle(.grouped)
        .padding(.vertical, 8)
    }
}

private struct ApiKeyRow: View {
    let backend: Backend
    @State private var input = ""
    @State private var saved = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(backend.label)
                    .fontWeight(.medium)
                if saved {
                    Label("設定済み", systemImage: "checkmark.circle.fill")
                        .font(.caption)
                        .foregroundStyle(.green)
                }
            }
            HStack {
                // グループ化フォーム内の既定スタイルは枠が描画されず
                // 入力欄と認識できないため、明示的に枠付きにする
                SecureField("API キーを入力", text: $input)
                    .textFieldStyle(.roundedBorder)
                Button("保存") {
                    let key = input.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !key.isEmpty else { return }
                    Keychain.setApiKey(key, for: backend)
                    input = ""
                    saved = true
                }
                .disabled(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                Button("削除") {
                    Keychain.deleteApiKey(for: backend)
                    saved = false
                }
                .disabled(!saved)
            }
        }
        .onAppear {
            saved = Keychain.hasApiKey(for: backend)
        }
    }
}
