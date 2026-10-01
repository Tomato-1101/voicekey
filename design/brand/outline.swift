// 文字列を SVG パスへ（CoreText）。引数: <font file | name:PostScriptName> <size> <kern> <text>
// 出力 JSON: {"d": ..., "advance": ..., "bbox": [x,y,w,h]}（y は下向き・ベースライン 0）
import Foundation
import CoreText
let a = CommandLine.arguments
let size = CGFloat(Double(a[2])!), kern = CGFloat(Double(a[3])!), text = a[4]
var font: CTFont
if a[1].hasPrefix("name:") {
    font = CTFontCreateWithName(String(a[1].dropFirst(5)) as CFString, size, nil)
} else {
    let descs = CTFontManagerCreateFontDescriptorsFromURL(URL(fileURLWithPath: a[1]) as CFURL) as! [CTFontDescriptor]
    font = CTFontCreateWithFontDescriptor(descs[0], size, nil)
}
let attr = NSAttributedString(string: text, attributes: [kCTFontAttributeName as NSAttributedString.Key: font,
                                                       kCTKernAttributeName as NSAttributedString.Key: kern])
let line = CTLineCreateWithAttributedString(attr)
var d = ""
func f(_ v: CGFloat) -> String { String(format: "%.2f", Double(v)) }
let flip = CGAffineTransform(scaleX: 1, y: -1)
var minX = CGFloat.greatestFiniteMagnitude, minY = minX, maxX = -minX, maxY = -minX
for run in CTLineGetGlyphRuns(line) as! [CTRun] {
    let n = CTRunGetGlyphCount(run)
    var glyphs = [CGGlyph](repeating: 0, count: n), pos = [CGPoint](repeating: .zero, count: n)
    CTRunGetGlyphs(run, CFRange(location: 0, length: n), &glyphs)
    CTRunGetPositions(run, CFRange(location: 0, length: n), &pos)
    let rf = (CTRunGetAttributes(run) as NSDictionary)[kCTFontAttributeName] as! CTFont
    for i in 0..<n {
        var t = CGAffineTransform(translationX: pos[i].x, y: pos[i].y).concatenating(flip)
        guard let p = CTFontCreatePathForGlyph(rf, glyphs[i], &t) else { continue }
        let b = p.boundingBoxOfPath
        minX = min(minX, b.minX); minY = min(minY, b.minY); maxX = max(maxX, b.maxX); maxY = max(maxY, b.maxY)
        p.applyWithBlock { e in
            let pt = e.pointee.points
            switch e.pointee.type {
            case .moveToPoint: d += "M\(f(pt[0].x)) \(f(pt[0].y))"
            case .addLineToPoint: d += "L\(f(pt[0].x)) \(f(pt[0].y))"
            case .addQuadCurveToPoint: d += "Q\(f(pt[0].x)) \(f(pt[0].y)) \(f(pt[1].x)) \(f(pt[1].y))"
            case .addCurveToPoint: d += "C\(f(pt[0].x)) \(f(pt[0].y)) \(f(pt[1].x)) \(f(pt[1].y)) \(f(pt[2].x)) \(f(pt[2].y))"
            case .closeSubpath: d += "Z"
            @unknown default: break
            }
        }
    }
}
let adv = CTLineGetTypographicBounds(line, nil, nil, nil)
let j: [String: Any] = ["d": d, "advance": adv, "bbox": [minX, minY, maxX - minX, maxY - minY]]
print(String(data: try! JSONSerialization.data(withJSONObject: j), encoding: .utf8)!)
