//
//  StatusIconTests.swift
//  メニューバーアイコン（キーのグリフ）の回帰テスト
//
//  - 待機だけがテンプレート画像（OS がメニューバーの明暗に合わせる）で、色付きの状態は非テンプレート
//  - 灯りの色・形が状態どおり（録音=橙の点／待機・変換中=輪で中心は抜け）
//  - 色付き状態の枠は描画時の外観で解決される（ライト=暗い枠／ダーク=明るい枠）
//
//  目視確認用に、環境変数 VOICEKEY_STATUSICON_PNG_DIR を渡すと全状態 × ライト/ダークを PNG に書き出す
//  （アプリ本体に開発用フラグを増やさないためテスト側で描く）。
//

import AppKit
import XCTest
@testable import voicekey

@MainActor
final class StatusIconTests: XCTestCase {

    private let states: [(name: String, state: AppState)] = [
        ("idle", .idle),
        ("recording", .recording(autoEnter: false, handsFree: false)),
        ("recording-autoEnter", .recording(autoEnter: true, handsFree: false)),
        ("recording-handsFree", .recording(autoEnter: false, handsFree: true)),
        ("transcribing", .transcribing),
    ]

    /// 描画倍率（1pt = scale px）
    private let scale: CGFloat = 8

    func testOnlyIdleIsTemplate() {
        for (name, state) in states {
            let image = StatusIcon.image(for: state)
            XCTAssertEqual(image.size, NSSize(width: 18, height: 18), name)
            XCTAssertEqual(image.isTemplate, state == .idle, name)
            XCTAssertEqual(image.accessibilityDescription, "voicekey", name)
        }
    }

    func testLightColorAndShapePerState() throws {
        // 灯りの中心 (11.7, 6.3)（y 下向き）
        for (name, state) in states {
            let rep = try render(StatusIcon.image(for: state), appearance: .aqua, background: nil)
            let center = pixel(rep, x: 11.7, y: 6.3)
            switch state {
            case .recording:
                assertSignal(center, "\(name) の点")
            case .idle, .transcribing:
                // 輪なので中心は抜けている
                XCTAssertLessThan(center.alphaComponent, 0.05, "\(name) の中心は透明")
            }
        }
        // 変換中の輪（半径 1.6 の線上）は橙
        let transcribing = try render(StatusIcon.image(for: .transcribing), appearance: .aqua, background: nil)
        assertSignal(pixel(transcribing, x: 11.7 + 1.6, y: 6.3), "変換中の輪")
        // 自動送信だけ刻印バー (4.5, 11.4, 4.875, 1.35) も橙
        let auto = try render(StatusIcon.image(for: .recording(autoEnter: true, handsFree: false)),
                              appearance: .aqua, background: nil)
        assertSignal(pixel(auto, x: 6.9, y: 12.07), "自動送信の刻印")
        let normal = try render(StatusIcon.image(for: .recording(autoEnter: false, handsFree: false)),
                                appearance: .aqua, background: nil)
        XCTAssertFalse(isSignal(pixel(normal, x: 6.9, y: 12.07)), "通常録音の刻印は橙にしない")
        // ハンズフリーだけ光輪（半径 3.2 の線上）がある
        let handsFree = try render(StatusIcon.image(for: .recording(autoEnter: false, handsFree: true)),
                                   appearance: .aqua, background: nil)
        XCTAssertGreaterThan(pixel(handsFree, x: 11.7 - 3.2, y: 6.3).alphaComponent, 0.2, "ハンズフリーの光輪")
        XCTAssertLessThan(pixel(normal, x: 11.7 - 3.2, y: 6.3).alphaComponent, 0.05, "通常録音に光輪は無い")
    }

    func testColoredFrameFollowsAppearance() throws {
        // キー枠の左辺（x = 1.875 + 線幅/2 付近）の明るさが外観で反転すること
        for (name, state) in states where state != .idle {
            let light = try render(StatusIcon.image(for: state), appearance: .aqua, background: nil)
            let dark = try render(StatusIcon.image(for: state), appearance: .darkAqua, background: nil)
            let l = pixel(light, x: 2.2, y: 9).usingColorSpace(.sRGB)!
            let d = pixel(dark, x: 2.2, y: 9).usingColorSpace(.sRGB)!
            XCTAssertGreaterThan(l.alphaComponent, 0.5, "\(name) ライトで枠が描かれている")
            XCTAssertGreaterThan(d.alphaComponent, 0.5, "\(name) ダークで枠が描かれている")
            XCTAssertLessThan(l.brightnessComponent, 0.3, "\(name) ライトの枠は暗い")
            XCTAssertGreaterThan(d.brightnessComponent, 0.7, "\(name) ダークの枠は明るい")
        }
    }

    /// 目視確認用の PNG 書き出し（環境変数があるときだけ）
    func testExportPNGsForReview() throws {
        guard let dir = ProcessInfo.processInfo.environment["VOICEKEY_STATUSICON_PNG_DIR"] else {
            throw XCTSkip("VOICEKEY_STATUSICON_PNG_DIR 未指定のため書き出さない")
        }
        // メニューバーに近い背景色（ライト=明るいグレー／ダーク=暗いグレー）
        let looks: [(String, NSAppearance.Name, NSColor)] = [
            ("light", .aqua, NSColor(srgbRed: 0.93, green: 0.93, blue: 0.93, alpha: 1)),
            ("dark", .darkAqua, NSColor(srgbRed: 0.14, green: 0.14, blue: 0.15, alpha: 1)),
        ]
        for (look, appearance, bg) in looks {
            for (name, state) in states {
                let rep = try render(StatusIcon.image(for: state), appearance: appearance, background: bg)
                let url = URL(fileURLWithPath: dir).appendingPathComponent("statusicon-\(look)-\(name).png")
                try XCTUnwrap(rep.representation(using: .png, properties: [:])).write(to: url)
            }
        }
    }

    // MARK: - ヘルパ

    /// 指定の外観でアイコンを描く。テンプレート画像は OS と同じく文字色で塗って見せる
    private func render(_ image: NSImage, appearance name: NSAppearance.Name, background: NSColor?) throws -> NSBitmapImageRep {
        let px = Int(18 * scale)
        let rep = try XCTUnwrap(NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
        ))
        rep.size = NSSize(width: 18, height: 18)
        let appearance = try XCTUnwrap(NSAppearance(named: name))
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        appearance.performAsCurrentDrawingAppearance {
            let rect = NSRect(x: 0, y: 0, width: 18, height: 18)
            if let background {
                background.setFill()
                rect.fill()
            }
            if image.isTemplate {
                // OS がテンプレートを塗るのに近い見た目（文字色で塗りつぶす）
                let tinted = NSImage(size: rect.size, flipped: false) { r in
                    image.draw(in: r)
                    NSColor.labelColor.set()
                    r.fill(using: .sourceAtop)
                    return true
                }
                tinted.draw(in: rect)
            } else {
                image.draw(in: rect)
            }
        }
        NSGraphicsContext.restoreGraphicsState()
        return rep
    }

    /// キャンバス座標（pt・y 下向き）の画素色
    private func pixel(_ rep: NSBitmapImageRep, x: CGFloat, y: CGFloat) -> NSColor {
        rep.colorAt(x: Int(x * scale), y: Int(y * scale)) ?? .clear
    }

    private func isSignal(_ color: NSColor) -> Bool {
        guard let c = color.usingColorSpace(.sRGB), c.alphaComponent > 0.8 else { return false }
        // #FF5A1F = (1.0, 0.353, 0.122)。アンチエイリアスと色空間変換の誤差を許す
        return abs(c.redComponent - 1.0) < 0.08 && abs(c.greenComponent - 0.353) < 0.08
            && abs(c.blueComponent - 0.122) < 0.08
    }

    private func assertSignal(_ color: NSColor, _ message: String, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(isSignal(color), "\(message) が #FF5A1F でない: \(color)", file: file, line: line)
    }
}
