//
//  UISnapshotTestMode.swift
//  メインウィンドウ（ホーム＋全設定タブ）を画面に出さずに PNG へ書き出す撮影ハーネス（CLI モード）
//
//  デザイン確認のたびに実ウィンドウを開いてスクリーンショットを撮ると、ユーザーの画面を奪う
//  （前面に出る・Dock にアイコンが出る）。ここでは画面外に置いた「表示しない」ウィンドウに
//  MainWindowView を載せ、cacheDisplay でビットマップへ描いて書き出すだけにする。
//
//  使い方:
//    dist/voicekey.app/Contents/MacOS/voicekey --ui-snapshot <出力ディレクトリ> [--appearance light|dark] [--log-file <path>]
//  出力: <home|タブ名>-<幅>x<高さ>-<light|dark>.png（760x600 と 1100x760 の 2 通り）
//  最終行の [VERDICT] status=ok files=<数> を判定に使う。
//
//  AppController を作らない＝ホットキー監視・録音・マイク・文字起こし・履歴同期の通信を一切起動しない。
//  設定・履歴・実績は実データを読むだけで書き換えず、「前回の画面」の保存もしない。
//  ガラス効果（NSVisualEffectView / glassEffect）は画面外の描画では写らない（レイアウト確認用）ので、
//  読みやすさのためにウィンドウ背景色の上へ重ねて書き出す。
//

import AppKit
import SwiftUI

@MainActor
enum UISnapshotTestMode {

    /// 撮影サイズ（既定＝最小の 760×600 と、広げたときの伸び方を見る 1100×760）
    private static let sizes: [NSSize] = [
        NSSize(width: 760, height: 600),
        NSSize(width: 1100, height: 760),
    ]

    /// 描画前にメインループを回す時間。onAppear で @State を埋めるタブ（マイク一覧・キーの状態など）の
    /// 反映を待つため
    private static let settleSeconds: TimeInterval = 0.4

    /// ファイル名に使う英字のタブ名（日本語のタブ名はファイル名に向かないため。未知のタグは tab<番号>）
    private static let tabSlugs: [Int: String] = [
        0: "general", 1: "slot1", 2: "slot2", 8: "dictionary", 9: "caption",
        10: "translate-input", 6: "account", 7: "about", 5: "api-keys",
    ]

    /// 標準出力と --log-file の両方へ書く（字幕側の writer は macOS 26 限定なので使わない）
    private static var logHandle: FileHandle?

    /// 引数を見てハーネスを実行する
    /// - Returns: ハーネスとして実行した（＝通常のアプリ起動をしない）なら true（実際には exit で終わる）
    static func runIfRequested() -> Bool {
        let arguments = CommandLine.arguments
        guard arguments.contains("--ui-snapshot") else { return false }

        if let path = optionValue("--log-file", in: arguments) {
            FileManager.default.createFile(atPath: path, contents: nil)
            logHandle = FileHandle(forWritingAtPath: path)
        }
        guard let outputPath = optionValue("--ui-snapshot", in: arguments) else {
            emit("[ERROR] 出力ディレクトリを指定してください: --ui-snapshot <dir>")
            finish(written: 0, failed: 1)
        }
        let appearanceName = optionValue("--appearance", in: arguments) ?? "light"
        guard appearanceName == "light" || appearanceName == "dark" else {
            emit("[ERROR] --appearance は light か dark を指定してください: \(appearanceName)")
            finish(written: 0, failed: 1)
        }
        let outputDir = URL(fileURLWithPath: outputPath, isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)
        } catch {
            emit("[ERROR] 出力ディレクトリを作れませんでした: \(error.localizedDescription)")
            finish(written: 0, failed: 1)
        }

        // Dock にもメニューバーにも出さない（ユーザーの画面に一切現れないようにする）
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let appearance = NSAppearance(named: appearanceName == "dark" ? .darkAqua : .aqua)
        app.appearance = appearance

        // View が依存するストア（読むだけ）。HistorySync は自動スケジュールを切り、applyConfig も呼ばない
        // ＝同期の通信も共有トークンの Keychain 読み出しも起きない
        let config = ConfigStore()
        let history = HistoryStore()
        let historySync = HistorySync(
            history: history,
            configuration: { .init(enabled: false, url: "") },
            automaticScheduling: false
        )
        let stats = StatsStore()

        var screens: [(name: String, showingSettings: Bool, tab: Int)] = [("home", false, 0)]
        for id in MainWindowView.settingsTabIDs {
            screens.append((tabSlugs[id] ?? "tab\(id)", true, id))
        }
        emit("[INFO] ui-snapshot 開始 appearance=\(appearanceName) screens=\(screens.map(\.name).joined(separator: ",")) personal=\(EmbeddedKeys.isPersonal)")

        var written = 0
        var failed = 0
        for size in sizes {
            for screen in screens {
                // 保存先なしのモデル＝タブを切り替えてもユーザーの「前回の画面」を書き換えない
                let model = MainWindowModel(showingSettings: screen.showingSettings, settingsTab: screen.tab)
                let view = MainWindowView(
                    config: config,
                    history: history,
                    historySync: historySync,
                    stats: stats,
                    updater: UpdaterController.shared,
                    model: model,
                    controller: nil
                )
                .tint(Brand.signal)
                let fileName = "\(screen.name)-\(Int(size.width))x\(Int(size.height))-\(appearanceName).png"
                let fileURL = outputDir.appendingPathComponent(fileName)
                guard let png = render(view, size: size, appearance: appearance) else {
                    emit("[FAIL] 描画できませんでした: \(fileName)")
                    failed += 1
                    continue
                }
                do {
                    try png.write(to: fileURL, options: .atomic)
                    emit("[FILE] \(fileURL.path)")
                    written += 1
                } catch {
                    emit("[FAIL] 書き込めませんでした: \(fileName) \(error.localizedDescription)")
                    failed += 1
                }
            }
        }
        finish(written: written, failed: failed)
    }

    /// View を画面外のウィンドウで描き、PNG データにする
    private static func render<V: View>(_ view: V, size: NSSize, appearance: NSAppearance?) -> Data? {
        let hosting = NSHostingView(rootView: view)
        // 画面外に置き、orderFront しない（＝ユーザーの画面に一切出ない）。SwiftUI に外観と
        // ウィンドウ環境（タイトルバー下まで広げる等）を与えるためだけに使う
        let window = NSWindow(
            contentRect: NSRect(origin: NSPoint(x: -20000, y: -20000), size: size),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.isReleasedWhenClosed = false
        window.appearance = appearance
        GlassWindow.applyFrostedChrome(to: window)
        window.contentView = hosting
        hosting.frame = NSRect(origin: .zero, size: size)
        defer { window.contentView = nil }

        hosting.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(settleSeconds))
        hosting.layoutSubtreeIfNeeded()

        guard let content = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) else { return nil }
        hosting.cacheDisplay(in: hosting.bounds, to: content)

        // 透明のままだと文字が読みにくいので、ウィンドウ背景色の上に重ねて不透明にする
        guard let flattened = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: content.pixelsWide,
            pixelsHigh: content.pixelsHigh,
            bitsPerSample: 8,
            samplesPerPixel: 4,
            hasAlpha: true,
            isPlanar: false,
            colorSpaceName: .deviceRGB,
            bytesPerRow: 0,
            bitsPerPixel: 0
        ) else { return nil }
        // Retina では画素数がポイントの 2 倍。コンテキストを作る前に size（ポイント）を合わせないと
        // 座標系が画素単位になり、左下 1/4 にしか描かれない
        flattened.size = content.size
        guard let context = NSGraphicsContext(bitmapImageRep: flattened) else { return nil }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        let rect = NSRect(origin: .zero, size: content.size)
        (appearance ?? NSAppearance.currentDrawing()).performAsCurrentDrawingAppearance {
            NSColor.windowBackgroundColor.setFill()
            rect.fill()
        }
        content.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
        NSGraphicsContext.restoreGraphicsState()
        return flattened.representation(using: .png, properties: [:])
    }

    /// 1 行出力する（標準出力＋ --log-file）
    private static func emit(_ line: String) {
        print(line)
        fflush(stdout)
        if let data = (line + "\n").data(using: .utf8) {
            try? logHandle?.write(contentsOf: data)
        }
    }

    /// 判定行を出して終了する
    private static func finish(written: Int, failed: Int) -> Never {
        let ok = failed == 0 && written > 0
        emit("[VERDICT] status=\(ok ? "ok" : "failed") files=\(written)")
        try? logHandle?.close()
        exit(ok ? 0 : 1)
    }

    /// 指定オプションの直後の値を取り出す（CaptionTestMode と同じ規則）
    private static func optionValue(_ name: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
        let value = arguments[index + 1]
        return value.hasPrefix("--") ? nil : value
    }
}
