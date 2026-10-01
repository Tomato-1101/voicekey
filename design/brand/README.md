# voicekey ブランド（ロゴ「17 LEGEND」）

キートップに刻印「voice」と灯り（橙の LED）を載せたアイコンと、ワードマーク「voicekey」のマスター。
ここにある `svg/` が正本で、アプリ・Windows・サイトの画像はすべてここから書き出す。

## 色

| 名前 | 値 | 用途 |
|---|---|---|
| signal（灯り） | `#FF5A1F` | 唯一のアクセント |
| signalDeep | `#D9480F` | ライト地の小さい文字だけ |
| bone | `#F4F1EB` | ライトの地 |
| stone | `#CFC9BE` | 線・弱い面 |
| legend（刻印） | `#7F796F` | 補助文字 |
| carbon | `#2B2A27` | ダークの地・ライトの濃い文字 |

Mac アプリのコード側は `macos/Sources/Voicekey/UI/Brand.swift` に同じ値がある（色を変えるときは両方直す）。

## 何がどこへ行くか

| マスター | 展開先 |
|---|---|
| `icon-bone.svg`（16/32px は `icon-bone-small.svg`） | `macos/Resources/AppIcon.icns`（旧形式・ボーン）、`macos/Resources/AppIconLight.png`（512）、`macos/scripts/assets/app_icon_1024.png` |
| `icon-carbon.svg` | `macos/Resources/AppIconDark.png`（512） |
| `icon-bone-bleed.svg` / `icon-carbon-bleed.svg` | `macos/Resources/AppIcon.icon/Assets/*-bleed-1024.png`（macOS 26 以降の外観追従アイコン。build_app.sh が actool で Assets.car にする） |
| `wordmark-carbon.svg` | `macos/scripts/assets/wordmark.svg`（DMG 背景） |
| `icon-bone.svg`（16/24/32px は small） | リポジトリ直下 `icon.ico`（Windows） |
| `icon-auto-small.svg` / `lockup-*.svg` / `og-light.svg` / `social-dark.svg` | voicekey-site（別リポジトリ）の favicon・ロゴ・OG 画像、GitHub の README・ソーシャルプレビュー |

メニューバーのアイコンは画像ではなくコードで描いている（`macos/Sources/Voicekey/VoicekeyApp.swift` の `StatusIcon`）。

## 再生成

```bash
cd design/brand
# 1) フォント: Google Fonts の Geist（500 / 600）の TTF を fonts/Geist-500.ttf / fonts/Geist-600.ttf に置く（コミットしない）
# 2) 文字をパスにする小道具をビルド（CoreText）
swiftc -O outline.swift -o outline
# 3) svg/ を上書き生成
python3 gen_brand.py
# 4) PNG 化（sharp は ~/Project/voicekey-site/node_modules のものを使う。先にあちらで npm install しておく）
node render.cjs svg/icon-bone.svg /tmp/icon-bone-1024.png 1024
```

- icns: `AppIcon.iconset/` に `icon_16x16.png`〜`icon_512x512@2x.png` を render.cjs で書き出し（16/32px は `icon-bone-small.svg`）、
  `iconutil -c icns AppIcon.iconset -o AppIcon.icns`。
- ico: 16/24/32px（small）と 48/64/128/256px を書き出して `python3 make_ico.py icon.ico w16.png w24.png …`。
- `*-bleed-1024.png` は `*-bleed.svg` を 1024 で書き出したもの（角丸なしの全面塗り。角丸と余白は OS が付ける）。
