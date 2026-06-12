//
//  make_app_icon.swift
//  アプリアイコンのマスター画像（1024x1024 PNG）を生成する。
//  デザインを変えたいときだけ再実行し、以下で .icns（コミット対象）に変換する:
//    swift scripts/dev/make_app_icon.swift scripts/assets/app_icon_1024.png
//    T=$(mktemp -d)/AppIcon.iconset && mkdir -p "$T"
//    for s in 16 32 128 256 512; do
//      sips -z $s $s scripts/assets/app_icon_1024.png --out "$T/icon_${s}x${s}.png"
//      sips -z $((s*2)) $((s*2)) scripts/assets/app_icon_1024.png --out "$T/icon_${s}x${s}@2x.png"
//    done
//    iconutil -c icns "$T" -o Resources/AppIcon.icns
//
//  デザイン: ダークな角丸スクエア（Big Sur スタイル・周囲に透過マージン）+
//  ブランドの青い波形バー（配布サイトの HUD モックと同じモチーフ）。

import AppKit

let outPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "app_icon_1024.png"

let S: CGFloat = 1024
guard let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: Int(S), pixelsHigh: Int(S),
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
) else { fatalError("NSBitmapImageRep の生成に失敗") }

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext

// macOS の HIG どおり、コンテンツ（角丸スクエア）は 824x824 を中央配置し周囲は透過。
// 影は Dock 上で他アプリと並んだときの立体感のため軽く敷く
let content = NSRect(x: 100, y: 100, width: 824, height: 824)
let squircle = NSBezierPath(roundedRect: content, xRadius: 185, yRadius: 185)

ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -8), blur: 28,
              color: NSColor.black.withAlphaComponent(0.35).cgColor)
NSColor(calibratedRed: 0.082, green: 0.090, blue: 0.110, alpha: 1).setFill()
squircle.fill()
ctx.restoreGState()

// 背景: 上が少し明るいダークグラデーション（サイトのダークテーマと同系色）
let bg = NSGradient(
    starting: NSColor(calibratedRed: 0.165, green: 0.180, blue: 0.220, alpha: 1),
    ending: NSColor(calibratedRed: 0.066, green: 0.072, blue: 0.090, alpha: 1)
)!
bg.draw(in: squircle, angle: -90)

// 波形バー 5 本（録音中インジケーターのモチーフ）。中央が最も高い左右対称
let heights: [CGFloat] = [220, 390, 540, 390, 220]
let barW: CGFloat = 76
let gap: CGFloat = 56
let totalW = barW * CGFloat(heights.count) + gap * CGFloat(heights.count - 1)
var x = (S - totalW) / 2
let centerY = S / 2

let barGradient = NSGradient(
    starting: NSColor(calibratedRed: 0.353, green: 0.784, blue: 0.980, alpha: 1), // 明るい水色
    ending: NSColor(calibratedRed: 0.039, green: 0.518, blue: 1.000, alpha: 1)    // システムブルー
)!
for h in heights {
    let bar = NSBezierPath(
        roundedRect: NSRect(x: x, y: centerY - h / 2, width: barW, height: h),
        xRadius: barW / 2, yRadius: barW / 2
    )
    barGradient.draw(in: bar, angle: -90)
    x += barW + gap
}

NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else { fatalError("PNG 変換に失敗") }
try! png.write(to: URL(fileURLWithPath: outPath))
print("==> アプリアイコンを生成: \(outPath) (1024x1024)")
