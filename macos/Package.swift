// swift-tools-version: 6.0
// voicekey for Mac - メニューバー常駐の音声入力アプリ
import PackageDescription

let package = Package(
    name: "voicekey",
    platforms: [
        .macOS(.v14)
    ],
    targets: [
        .executableTarget(
            name: "voicekey",
            path: "Sources/Voicekey",
            swiftSettings: [
                // Swift 6 の strict concurrency は段階導入とし、まず v5 モードで安定させる
                .swiftLanguageMode(.v5)
            ]
        )
    ]
)
