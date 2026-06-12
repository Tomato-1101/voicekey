//
//  make_dmg_background.swift
//  DMG ウィンドウの背景画像（660x400 ポイント・2x PNG）を生成する。
//  デザインを変えたいときだけ再実行する（生成物 scripts/assets/dmg_background.png はコミット対象）:
//    swift scripts/dev/make_dmg_background.swift scripts/assets/dmg_background.png
//
//  Finder のアイコンラベル色はシステム外観（ライト=黒/ダーク=白）に追従するため、
//  どちらのモードでもアイコン名が読めるよう中間色を避けた明るい背景にしている
//  （Chrome 等の主要アプリの DMG と同じ流儀）。

import AppKit

let outPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "dmg_background.png"

// 660x400 ポイントを 2x（1320x800 ピクセル）で描き、PNG の DPI で 1/2 表示にする
let W: CGFloat = 660, H: CGFloat = 400
guard let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: Int(W * 2), pixelsHigh: Int(H * 2),
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
) else { fatalError("NSBitmapImageRep の生成に失敗") }

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
NSGraphicsContext.current!.cgContext.scaleBy(x: 2, y: 2)

// AppKit は左下原点なので、デザイン上の「上からの位置」を変換するヘルパ
func fromTop(_ y: CGFloat) -> CGFloat { H - y }

// 背景: ごく薄いグレー（Finder 標準に近い・両外観でラベルが読める）
NSColor(calibratedRed: 0.957, green: 0.961, blue: 0.969, alpha: 1).setFill()
NSRect(x: 0, y: 0, width: W, height: H).fill()

// 中央揃えでテキストを描くヘルパ
func drawCentered(_ text: String, centerX: CGFloat, topY: CGFloat, font: NSFont, color: NSColor) {
    let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color]
    let s = NSAttributedString(string: text, attributes: attrs)
    let size = s.size()
    s.draw(at: NSPoint(x: centerX - size.width / 2, y: fromTop(topY) - size.height))
}

// タイトルと説明
drawCentered("voicekey", centerX: W / 2, topY: 64,
             font: .systemFont(ofSize: 30, weight: .bold),
             color: NSColor(calibratedWhite: 0.12, alpha: 1))
drawCentered("アイコンを Applications フォルダにドラッグしてインストール", centerX: W / 2, topY: 96,
             font: .systemFont(ofSize: 14, weight: .regular),
             color: NSColor(calibratedWhite: 0.45, alpha: 1))

// 矢印（アプリアイコン位置 x=165 と Applications 位置 x=495 の間、アイコン中心の高さ y=205）
let arrowY = fromTop(205)
let arrowColor = NSColor(calibratedRed: 0.04, green: 0.52, blue: 1.0, alpha: 0.85) // システムブルー寄り
arrowColor.setStroke()
arrowColor.setFill()

let shaft = NSBezierPath()
shaft.move(to: NSPoint(x: 262, y: arrowY))
shaft.line(to: NSPoint(x: 380, y: arrowY))
shaft.lineWidth = 7
shaft.lineCapStyle = .round
shaft.stroke()

let head = NSBezierPath()
head.move(to: NSPoint(x: 376, y: arrowY + 14))
head.line(to: NSPoint(x: 400, y: arrowY))
head.line(to: NSPoint(x: 376, y: arrowY - 14))
head.close()
head.fill()

// 下部の補足（Gatekeeper 回避の最重要手順だけ 1 行）
drawCentered("初回起動は voicekey を右クリック →「開く」（詳細は同梱のお読みくださいへ）",
             centerX: W / 2, topY: 376,
             font: .systemFont(ofSize: 12, weight: .regular),
             color: NSColor(calibratedWhite: 0.55, alpha: 1))

NSGraphicsContext.restoreGraphicsState()

// ポイントサイズを 660x400 にして PNG の DPI（144）を埋め込む（Finder が 2x として解釈する）
rep.size = NSSize(width: W, height: H)
guard let png = rep.representation(using: .png, properties: [:]) else { fatalError("PNG 変換に失敗") }
try! png.write(to: URL(fileURLWithPath: outPath))
print("==> 背景画像を生成: \(outPath) (\(Int(W))x\(Int(H))pt @2x)")
