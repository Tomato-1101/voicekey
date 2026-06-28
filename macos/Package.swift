// swift-tools-version: 6.0
// voicekey for Mac - メニューバー常駐の音声入力アプリ
import PackageDescription

let package = Package(
    name: "voicekey",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        // 自動アップデート（ベータ配布用）。appcast は voicekey-releases リポジトリで配信
        .package(url: "https://github.com/sparkle-project/Sparkle", from: "2.6.0")
    ],
    targets: [
        .executableTarget(
            name: "voicekey",
            dependencies: [
                .product(name: "Sparkle", package: "Sparkle")
            ],
            path: "Sources/Voicekey",
            swiftSettings: [
                // Swift 6 の strict concurrency は段階導入とし、まず v5 モードで安定させる
                .swiftLanguageMode(.v5)
            ]
        ),
        // ロジック単体のテスト（Keychain 書き込み手順など）。実 Keychain には触れない
        .testTarget(
            name: "voicekeyTests",
            dependencies: ["voicekey"],
            path: "Tests/VoicekeyTests",
            swiftSettings: [
                .swiftLanguageMode(.v5)
            ]
        )
    ]
)
