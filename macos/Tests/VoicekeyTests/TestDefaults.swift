//
//  TestDefaults.swift
//  テスト用の隔離 UserDefaults
//
//  suite 名を "voicekey.test.<UUID>" にすると ~/Library/Preferences に plist ができ、
//  removePersistentDomain で中身を消しても cfprefsd が空のファイルを後から書き戻すので、
//  実行のたびに数十個ずつ溜まっていた（2026-10-02 時点で 2141 個）。
//  suite 名に絶対パスを渡すと CFPreferences はそのパスの plist を使うので、
//  一時フォルダに置いて Preferences を汚さない（一時フォルダは OS が掃除する）。
//

import Foundation

/// テスト 1 件ぶんの使い捨て suite 名（一時フォルダ内の絶対パス）
func testDefaultsSuite() -> String {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent("voicekey-tests")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir.appendingPathComponent(UUID().uuidString).path
}

/// テスト後の後始末（中身を消し、一時フォルダの plist も消す）
func removeTestDefaults(_ defaults: UserDefaults, _ suite: String) {
    defaults.removePersistentDomain(forName: suite)
    try? FileManager.default.removeItem(atPath: suite + ".plist")
}
