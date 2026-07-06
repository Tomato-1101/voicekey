//
//  make_app_icon.swift
//  アプリアイコンのマスター画像（1024x1024 PNG）を生成する。
//  デザインを変えたいときだけ再実行し、以下で .icns（コミット対象）に変換する:
//
//    # 1) マスター PNG を生成
//    swift scripts/dev/make_app_icon.swift scripts/assets/app_icon_1024.png
//    # 2) 各サイズへ縮小して iconset を作り .icns に固める
//    T=$(mktemp -d)/AppIcon.iconset && mkdir -p "$T"
//    for s in 16 32 128 256 512; do
//      sips -z $s $s scripts/assets/app_icon_1024.png --out "$T/icon_${s}x${s}.png"
//      sips -z $((s*2)) $((s*2)) scripts/assets/app_icon_1024.png --out "$T/icon_${s}x${s}@2x.png"
//    done
//    iconutil -c icns "$T" -o Resources/AppIcon.icns
//    # 3) Windows 用 icon.ico もこの同じマスターから再生成（Pillow で 16..256 を束ねる）
//
//  デザイン（現行 release アイコンの忠実再現・不変のモチーフ）:
//    インディゴの角丸スクエア（squircle・周囲に透過マージン＋ドロップシャドウ）に、
//    「声 → テキスト」を表す 4 要素を左から右へ高くしながら並べる。
//    ・丸端の縦棒 3 本（左が最も低く右へ高くなる／左端だけ淡ラベンダー・他 2 本は純白）
//    ・右端に I ビーム（テキストカーソル。縦棒＋上下のセリフ横棒・淡ペリウィンクル色）
//    ※ 各要素は縦中央そろえで、高さだけが右へ増えていく。
//
//  今回の刷新: 上記モチーフ・配色はそのままに「ガラス質感」を追加した。
//    (1) 上部スペキュラー: squircle 上半分に白→透明の緩い面グラデ＋放射ハイライト（レンズの反射面）
//    (2) 上縁ハイライトライン: 最外周の細いリングに縦グラデ（側面へ滑らかにフェード・継ぎ目なし）
//    (3) 内周リムライト: 内周リングに上=明るい／下=暗いの縦グラデ（ガラスの厚み）
//    (4) シンボルのガラス化: 各要素を上=明るい／下=わずかに沈む縦グラデ＋上端の細い艶
//    (5) 深度: シンボル背後の柔らかいドロップシャドウで、白系シンボルがインディゴ地に浮く
//  配色はインディゴ地＋白系シンボルを維持（紫に振らない・ネオン発光は入れない）。
//
//  色・寸法は release の実アイコン（scripts/assets/app_icon_1024.png）を
//  ピクセル計測してスポイトした実測値。
//

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

// 角丸スクエアを作るヘルパ
func squirclePath(_ rect: NSRect, _ r: CGFloat) -> NSBezierPath {
    NSBezierPath(roundedRect: rect, xRadius: r, yRadius: r)
}
// 0-255 の実測値から NSColor を作るヘルパ。
// PNG は sRGB で書き出されるため、実測（sRGB のピクセル値）と一致させるには sRGB で指定する。
// （calibratedRGB だとガンマ差で最終ピクセル値が持ち上がり、地の色が淡くなってしまう）
func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: r/255, green: g/255, blue: b/255, alpha: a)
}

// ── レイアウト（release と同一）─────────────────────────────
// コンテンツ（角丸スクエア）は 824x824 を中央配置し周囲は透過。
let content = NSRect(x: 100, y: 100, width: 824, height: 824)
let cornerR: CGFloat = 185
let squircle = squirclePath(content, cornerR)

// ── (0) 台座の影 ───────────────────────────────────────────
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -8), blur: 28,
              color: NSColor.black.withAlphaComponent(0.35).cgColor)
rgb(89, 89, 235).setFill() // 影のシルエット用（この上に背景グラデを重ねる）
squircle.fill()
ctx.restoreGState()

// ── (1) 背景グラデーション（インディゴ・上が明るい）─────────
// release 実測: 上 ≈ (97,99,240) / 下 ≈ (81,74,230)。彩度・色相は変えない。
let bg = NSGradient(
    starting: rgb(94, 95, 237),
    ending: rgb(81, 74, 230)
)!
bg.draw(in: squircle, angle: -90) // -90 = 上→下（starting が上）

// ── (2) 上部スペキュラー（ガラス面の反射）───────────────────
// インディゴ地の濃さ・彩度を保つため、白は「上端の狭い帯」だけに集中させる。
// （面全体に白を敷くと地が淡くなるので、放射ハイライトは使わない）
ctx.saveGState()
squircle.addClip()
if let sheen = NSGradient(
    colors: [NSColor.white.withAlphaComponent(0.06), NSColor.white.withAlphaComponent(0.0)],
    atLocations: [0.0, 0.28], colorSpace: NSColorSpace.deviceRGB
) {
    sheen.draw(in: squircle, angle: -90)
}
ctx.restoreGState()

// ── (3) 上縁の細いハイライトライン（最外周の細いリングに縦グラデ）─
ctx.saveGState()
let edgeOuter = squirclePath(content.insetBy(dx: 1, dy: 1), cornerR - 1)
let edgeInner = squirclePath(content.insetBy(dx: 5, dy: 5), cornerR - 5)
let edgeRing = edgeOuter.copy() as! NSBezierPath
edgeRing.append(edgeInner)
edgeRing.windingRule = .evenOdd
edgeRing.addClip()
if let edgeGrad = NSGradient(
    colors: [NSColor.white.withAlphaComponent(0.50), NSColor.white.withAlphaComponent(0.0)],
    atLocations: [0.0, 0.45], colorSpace: NSColorSpace.deviceRGB
) {
    edgeGrad.draw(in: content, angle: -90)
}
ctx.restoreGState()

// ── (4) リムライト（内周のリングに縦グラデ・上=白／下=黒）─────
ctx.saveGState()
let rimOuter = squirclePath(content.insetBy(dx: 3, dy: 3), cornerR - 3)
let rimInner = squirclePath(content.insetBy(dx: 13, dy: 13), cornerR - 13)
let ring = rimOuter.copy() as! NSBezierPath
ring.append(rimInner)
ring.windingRule = .evenOdd
ring.addClip()
if let rimGrad = NSGradient(
    colors: [
        NSColor.white.withAlphaComponent(0.22),
        NSColor.white.withAlphaComponent(0.0),
        NSColor.black.withAlphaComponent(0.0),
        NSColor.black.withAlphaComponent(0.18),
    ],
    atLocations: [0.0, 0.42, 0.58, 1.0], colorSpace: NSColorSpace.deviceRGB
) {
    rimGrad.draw(in: content, angle: -90)
}
ctx.restoreGState()

// ── (5) シンボル（release の 4 要素を忠実再現＋ガラス化）─────
let centerY = S / 2 // 全要素は縦中央そろえ

// 丸端の縦棒（幅 58・角丸 29）。実測の中心 x と高さ。
func barPath(centerX: CGFloat, height: CGFloat, width: CGFloat = 58) -> NSBezierPath {
    let r = NSRect(x: centerX - width/2, y: centerY - height/2, width: width, height: height)
    return NSBezierPath(roundedRect: r, xRadius: width/2, yRadius: width/2)
}

// I ビーム（縦棒＋上下セリフ）を 1 つの塗りにユニオンして返す。
func iBeamPath(centerX: CGFloat, totalHeight: CGFloat,
               stemW: CGFloat = 41, serifW: CGFloat = 119, serifH: CGFloat = 42) -> NSBezierPath {
    let top = centerY + totalHeight/2
    let stem = NSBezierPath(
        roundedRect: NSRect(x: centerX - stemW/2, y: centerY - totalHeight/2, width: stemW, height: totalHeight),
        xRadius: stemW/2, yRadius: stemW/2)
    let serifTop = NSBezierPath(
        roundedRect: NSRect(x: centerX - serifW/2, y: top - serifH, width: serifW, height: serifH),
        xRadius: serifH/2, yRadius: serifH/2)
    let serifBottom = NSBezierPath(
        roundedRect: NSRect(x: centerX - serifW/2, y: centerY - totalHeight/2, width: serifW, height: serifH),
        xRadius: serifH/2, yRadius: serifH/2)
    let p = stem.copy() as! NSBezierPath
    p.append(serifTop); p.append(serifBottom)
    p.windingRule = .nonZero // 重なりを 1 つのシルエットに
    return p
}

// 各要素（パスと上/下グラデ色）。上=明るい／下=わずかに沈む＝ガラスの厚み。
struct Sym { let path: NSBezierPath; let top: NSColor; let bottom: NSColor }
let symbols: [Sym] = [
    // 左端バー（淡ラベンダー・最も低い h=128）
    Sym(path: barPath(centerX: 308.5, height: 128), top: rgb(244, 244, 255), bottom: rgb(220, 220, 246)),
    // 2 本目（白・h=258）
    Sym(path: barPath(centerX: 411.5, height: 258), top: rgb(255, 255, 255), bottom: rgb(248, 249, 253)),
    // 3 本目（白・最も高い h=386）
    Sym(path: barPath(centerX: 514.5, height: 386), top: rgb(255, 255, 255), bottom: rgb(248, 249, 253)),
    // 右端 I ビーム（淡ペリウィンクル・全高 412）
    Sym(path: iBeamPath(centerX: 671, totalHeight: 412), top: rgb(216, 224, 255), bottom: rgb(188, 199, 248)),
]

// (5a) 深度: シンボル群の背後に柔らかいドロップシャドウ。
// まず影付きでソリッド塗り（＝確実に影を落とす）→ その上に本体グラデを重ねる。
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -4), blur: 18,
              color: NSColor.black.withAlphaComponent(0.15).cgColor)
rgb(235, 238, 252).setFill()
for s in symbols { s.path.fill() }
ctx.restoreGState() // 影オフ

// (5b) 本体: 各要素を上→下の縦グラデで塗る（ガラスの厚み）。
for s in symbols {
    if let g = NSGradient(starting: s.top, ending: s.bottom) {
        g.draw(in: s.path, angle: -90)
    }
}

// (5c) 上端の細い艶ハイライト（淡色要素で艶が出る／白要素ではほぼ不可視）。
for s in symbols {
    ctx.saveGState()
    s.path.addClip()
    if let cap = NSGradient(
        colors: [NSColor.white.withAlphaComponent(0.30), NSColor.white.withAlphaComponent(0.0)],
        atLocations: [0.0, 0.22], colorSpace: NSColorSpace.deviceRGB
    ) {
        cap.draw(in: s.path.bounds, angle: -90)
    }
    ctx.restoreGState()
}

NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else { fatalError("PNG 変換に失敗") }
try! png.write(to: URL(fileURLWithPath: outPath))
print("==> アプリアイコンを生成: \(outPath) (1024x1024)")
