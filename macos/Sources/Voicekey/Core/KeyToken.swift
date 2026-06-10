//
//  KeyToken.swift
//  キートークン定義（キーコード変換・表示名・マッチング規則）
//
//  ホットキーは文字列トークンの集合として表現する（Python 版と同じ語彙）。
//  例: "cmd_r"（右コマンド）, "ctrl_l", "f2", "space"
//  修飾キーは CGEventFlags のデバイス依存ビットから左右を厳密に判定する。
//

import CoreGraphics
import Foundation

enum KeyToken {

    /// 修飾キーの汎用名（左右を区別しない設定用トークン）
    static let genericModifiers: Set<String> = ["cmd", "alt", "shift", "ctrl"]

    /// CGEventFlags のデバイス依存ビット → トークン。
    /// NX_DEVICE*KEYMASK 相当。左右の修飾キーを個別に検出するために使う
    /// （flagsChanged のキーコードだけでは「左右同時押し→片方離す」を判定できない）。
    static let modifierBits: [(mask: UInt64, token: String)] = [
        (0x00000001, "ctrl_l"),
        (0x00002000, "ctrl_r"),
        (0x00000002, "shift_l"),
        (0x00000004, "shift_r"),
        (0x00000008, "cmd_l"),
        (0x00000010, "cmd_r"),
        (0x00000020, "alt_l"),
        (0x00000040, "alt_r"),
    ]

    /// 現在の CGEventFlags から押下中の修飾キートークン集合を得る
    static func modifierTokens(from flags: CGEventFlags) -> Set<String> {
        var tokens: Set<String> = []
        for (mask, token) in modifierBits where flags.rawValue & mask != 0 {
            tokens.insert(token)
        }
        // fn キー（左右の区別なし）
        if flags.contains(.maskSecondaryFn) {
            tokens.insert("fn")
        }
        return tokens
    }

    /// 通常キーのキーコード → トークン（kVK_* 準拠、US 配列ベース）
    static let keyCodeTokens: [Int64: String] = [
        // 制御・記号
        53: "esc", 49: "space", 48: "tab", 36: "enter", 76: "enter",
        51: "backspace", 117: "delete",
        123: "left", 124: "right", 125: "down", 126: "up",
        115: "home", 119: "end", 116: "page_up", 121: "page_down",
        // ファンクションキー
        122: "f1", 120: "f2", 99: "f3", 118: "f4", 96: "f5", 97: "f6",
        98: "f7", 100: "f8", 101: "f9", 109: "f10", 103: "f11", 111: "f12",
        105: "f13", 107: "f14", 113: "f15", 106: "f16", 64: "f17", 79: "f18",
        80: "f19", 90: "f20",
        // 英字
        0: "a", 11: "b", 8: "c", 2: "d", 14: "e", 3: "f", 5: "g", 4: "h",
        34: "i", 38: "j", 40: "k", 37: "l", 46: "m", 45: "n", 31: "o",
        35: "p", 12: "q", 15: "r", 1: "s", 17: "t", 32: "u", 9: "v",
        13: "w", 7: "x", 16: "y", 6: "z",
        // 数字
        29: "0", 18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6",
        26: "7", 28: "8", 25: "9",
    ]

    /// キーコードからトークンを得る（未知のキーは nil）
    static func token(forKeyCode keyCode: Int64) -> String? {
        keyCodeTokens[keyCode]
    }

    /// 設定トークンに対して「押された」とみなせるトークンの集合。
    ///
    /// - 汎用指定（"cmd"）: 左右どちらでも一致
    /// - 左右指定（"cmd_r" 等）: そのキーのみ一致
    static func acceptableNames(for token: String) -> Set<String> {
        if genericModifiers.contains(token) {
            return [token, "\(token)_l", "\(token)_r"]
        }
        return [token]
    }

    /// 人間が読める表示名
    static func displayName(_ token: String) -> String {
        switch token {
        case "cmd": return "⌘"
        case "cmd_l": return "左⌘"
        case "cmd_r": return "右⌘"
        case "alt": return "⌥"
        case "alt_l": return "左⌥"
        case "alt_r": return "右⌥"
        case "shift": return "⇧"
        case "shift_l": return "左⇧"
        case "shift_r": return "右⇧"
        case "ctrl": return "⌃"
        case "ctrl_l": return "左⌃"
        case "ctrl_r": return "右⌃"
        case "fn": return "fn"
        case "space": return "Space"
        case "enter": return "Return"
        case "esc": return "Esc"
        case "tab": return "Tab"
        case "backspace": return "Delete"
        default:
            if token.count == 1 { return token.uppercased() }
            if token.hasPrefix("f"), Int(token.dropFirst()) != nil {
                return token.uppercased()
            }
            return token
        }
    }
}
