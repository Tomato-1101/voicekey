"""
UIスタイル・テーマ定義モジュール

クロスプラットフォーム向けのUIテーマ（カラー、フォント、スタイルシート）を定義する。
ダークモード/ライトモードの切り替えに対応。

デザイン方針（2026-07 再設計 v2・「浮遊するガラス島」近似）:
- backdrop（BG_WINDOW）に対角の無彩色グラデ（紫・ネオンは信頼感優先で撤去）を敷き、その上へ
  サイドバー島・コンテンツ島・カード・ボタンを rgba 半透明＋1px の明るいトップエッジ
  （GLASS_EDGE）＋下端の沈み（EDGE_BOTTOM）で「浮いた島」として重ねる。ルートレイアウトに
  余白を持たせて四周に backdrop を覗かせ、島が浮いて見えるようにする（サイドバーとコンテンツが
  「2 画面分割」に見えないようにするのが v1 からの主眼）。QSS の rgba は同一ウィンドウ内の親背景と
  正しく合成されるため、本物のウィンドウ透過（WA_TranslucentBackground / DWM アクリル・Mica）を
  使わずに「すりガラスの重なり」の印象を出す。影は QSS に無いので、深度は明るいトップエッジ＋
  下端の暗いエッジ＋縦グラデで表現する。
- 面の色はコンテナ側で明示する（サイドバー島 / コンテンツ島 / カード の 3 層）。
  グローバルの QWidget には背景を持たせない（カード上のラベル等が背景色で潰れるため）
- QMenu / QComboBox のドロップダウンは別トップレベルのポップアップウィンドウで、rgba は
  OS 既定色と合成され意図しない色になるため、背景は不透明 hex（POPUP_BG）を必ず使う
- 矢印・チェック等のグリフは CSS ボーダートリックではなく SVG ファイルで描く
  （ボーダートリックは DPI / スタイルエンジン差で崩れ、data URI は Qt の QSS が
  サポートしないため、一時ディレクトリへ書き出したファイルを url() で参照する）
"""

import tempfile
from pathlib import Path

from ..platform import get_platform_adapter

# QSS から参照するアイコン SVG の書き出し先
_ICON_DIR = Path(tempfile.gettempdir()) / "voicekey_qss_icons"


def _icon_url(name: str, svg: str) -> str:
    """SVG をファイルへ書き出し、QSS の url() で使えるパス（スラッシュ区切り）を返す。"""
    _ICON_DIR.mkdir(parents=True, exist_ok=True)
    path = _ICON_DIR / f"{name}.svg"
    path.write_text(svg, encoding="utf-8")
    return path.as_posix()


def _chevron_svg(color: str, direction: str) -> str:
    """コンボボックス・スピンボックス用のシェブロン（山形）SVG を返す。"""
    points = {
        "down": "6 9 12 15 18 9",
        "up": "6 15 12 9 18 15",
    }[direction]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="2.5" stroke-linecap="round" '
        f'stroke-linejoin="round"><polyline points="{points}"/></svg>'
    )


def _check_svg() -> str:
    """チェックボックス用の白いチェックマーク SVG を返す。"""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        'stroke="white" stroke-width="3" stroke-linecap="round" '
        'stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>'
    )


class MacTheme:
    """
    macOS風テーマのカラーとフォント定義。

    ダークモード/ライトモードに対応したカラーパレットと、
    Qt全体に適用するスタイルシートを提供する。
    """

    # 共通フォント設定（OSごとに適切なフォールバックを使用）
    FONT_FAMILY = get_platform_adapter().font_css_stack

    # フォントサイズ体系（ここ以外でサイズを直書きしない）
    FONT_SIZE_TITLE = 19         # ページタイトル
    FONT_SIZE_NORMAL = 13        # 標準（行ラベル・コントロール）
    FONT_SIZE_CAPTION = 12       # 補足説明・ステータス

    # 角丸体系（コントロール = 8px / カード・リスト = 12px。Liquid Glass 近似で角丸をやや大きく）
    RADIUS_CONTROL = 8
    RADIUS_PANEL = 12

    class Colors:
        """
        テーマ別カラーパレット（Liquid Glass 近似: 不透明グラデ土台の上に rgba 半透明レイヤ）。

        Attributes:
            BG_WINDOW: ルート/ダイアログの対角グラデ backdrop（島が浮く下地）
            BG_SIDEBAR: 左サイドバー島の面（rgba: backdrop が透ける）
            CONTENT_BG: 右コンテンツ島の面（rgba: サイドバー島よりやや薄い）
            BG_CONTENT: QMessageBox 用の不透明フラット背景
            CARD_BG: 設定カード背景（半透明の縦グラデ・島より一段明るい）
            CARD_BORDER: 島・カードの基準リム（四辺の輪郭線）
            GLASS_EDGE: 面の上端 1px ハイライト（border-top-color に使う擬似リムライト）
            EDGE_BOTTOM: 面の下端を沈める色（border-bottom-color。影の代わり）
            POPUP_BG: QMenu / コンボのドロップダウン背景（別ウィンドウのため不透明 hex 必須）
            HAIRLINE: カード内の行区切り線
            TEXT: テキスト色
            SECONDARY_TEXT: 二次テキスト色（QColor 消費のため hex 固定）
            ACCENT / ACCENT_HOVER / ACCENT_PRESSED: アクセント（青・QColor 消費のため hex 固定）
            ACCENT_GRAD / ACCENT_GRAD_HOVER / ACCENT_GRAD_PRESSED: アクセントの縦グラデ（塗り用）
            INPUT_BG / INPUT_BORDER: 入力欄
            BTN_BG / BTN_HOVER / BTN_PRESSED / BTN_BORDER: 通常ボタン
            HOVER_BG: リスト・メニューのホバー
            SUCCESS / WARNING: ステータス文字色
            TOGGLE_OFF: トグルスイッチのオフ時トラック色
            SCROLL_HANDLE: スクロールバーのつまみ
        """

        def __init__(self, is_dark: bool):
            """
            カラーパレットを初期化する。

            Args:
                is_dark: ダークモードの場合True
            """
            if is_dark:
                # ルート土台（backdrop）。紫・ネオンは「信頼できる色に」との指摘で撤去し、
                # 無彩色（黒〜ダークグレー）の対角グラデにする。この上に半透明の「島」を浮かべる
                self.BG_WINDOW = ("qlineargradient(x1:0, y1:0, x2:1, y2:1, "
                                  "stop:0 #2A2A2D, stop:0.4 #1F1F22, stop:1 #151517)")
                self.BG_SIDEBAR = "rgba(255, 255, 255, 0.05)"   # サイドバー島の面（backdrop が透ける）
                self.CONTENT_BG = "rgba(255, 255, 255, 0.04)"   # コンテンツ島の面（島側はやや薄く）
                self.BG_CONTENT = "#1B2130"                     # QMessageBox 用の不透明フラット
                self.CARD_BG = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                                "stop:0 rgba(255,255,255,0.075), stop:1 rgba(255,255,255,0.045))")
                self.CARD_BORDER = "rgba(255, 255, 255, 0.10)"  # 島・カードの基準リム（四辺）
                self.GLASS_EDGE = "rgba(255, 255, 255, 0.30)"   # 上端の明るいリム（ガラスの厚み）
                self.EDGE_BOTTOM = "rgba(0, 0, 0, 0.35)"        # 下端を沈めて影の代わりにする
                self.POPUP_BG = "#252B3A"                       # ドロップダウンは不透明必須
                self.HAIRLINE = "rgba(255, 255, 255, 0.08)"
                self.TEXT = "#F4F5FB"
                self.SECONDARY_TEXT = "#A8AEBD"                 # 半透明カード上でも約 5:1・hex 固定
                self.ACCENT = "#0A84FF"                         # hex 維持必須（QColor 消費）
                self.ACCENT_HOVER = "#2B95FF"
                self.ACCENT_PRESSED = "#0774E0"
                self.ACCENT_GRAD = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                                    "stop:0 #4AA3FF, stop:1 #0A6FE6)")
                self.ACCENT_GRAD_HOVER = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                                          "stop:0 #63B0FF, stop:1 #1B7CF2)")
                self.ACCENT_GRAD_PRESSED = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                                            "stop:0 #2E90F5, stop:1 #085FC8)")
                self.INPUT_BG = "rgba(0, 0, 0, 0.25)"           # 沈み込み表現（v1 より透明側へ）
                self.INPUT_BORDER = "rgba(255, 255, 255, 0.14)"
                self.BTN_BG = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                               "stop:0 rgba(255,255,255,0.10), stop:1 rgba(255,255,255,0.05))")
                self.BTN_HOVER = "rgba(255, 255, 255, 0.13)"    # 基準 +0.04 相当
                self.BTN_PRESSED = "rgba(255, 255, 255, 0.04)"  # 基準 -0.02 相当
                self.BTN_BORDER = "rgba(255, 255, 255, 0.12)"
                self.HOVER_BG = "rgba(255, 255, 255, 0.06)"
                self.SUCCESS = "#32D74B"
                self.WARNING = "#FF9F0A"
                self.TOGGLE_OFF = "#48484E"
                self.SCROLL_HANDLE = "rgba(255, 255, 255, 0.25)"
            else:
                # ルート土台（backdrop）。青みを抜いた無彩色（白〜ライトグレー）の対角グラデ。
                # この上に白系半透明の「島」とカード・ボタンを浮かべる
                self.BG_WINDOW = ("qlineargradient(x1:0, y1:0, x2:1, y2:1, "
                                  "stop:0 #F1F1F3, stop:0.5 #F6F6F8, stop:1 #EAEAED)")
                self.BG_SIDEBAR = "rgba(255, 255, 255, 0.55)"   # サイドバー島の面（白すりガラス）
                self.CONTENT_BG = "rgba(255, 255, 255, 0.45)"   # コンテンツ島の面（島側はやや薄く）
                self.BG_CONTENT = "#F2F3F8"                     # QMessageBox 用の不透明フラット
                self.CARD_BG = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                                "stop:0 rgba(255,255,255,0.80), stop:1 rgba(255,255,255,0.62))")
                self.CARD_BORDER = "rgba(0, 0, 0, 0.06)"        # 島・カードの基準リム（四辺）
                self.GLASS_EDGE = "rgba(255, 255, 255, 0.95)"   # 上端の明るいリム（ガラスの厚み）
                self.EDGE_BOTTOM = "rgba(0, 0, 0, 0.05)"        # 下端をわずかに沈める
                self.POPUP_BG = "#FFFFFF"                       # ドロップダウンは不透明必須
                self.HAIRLINE = "rgba(0, 0, 0, 0.07)"
                self.TEXT = "#1C1C21"
                self.SECONDARY_TEXT = "#6C6C74"                 # 白地 約 4.9:1・hex 固定
                self.ACCENT = "#007AFF"                         # hex 維持必須（QColor 消費）
                self.ACCENT_HOVER = "#1A88FF"
                self.ACCENT_PRESSED = "#0068DA"
                self.ACCENT_GRAD = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                                    "stop:0 #2E9BFF, stop:1 #0069DC)")
                self.ACCENT_GRAD_HOVER = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                                          "stop:0 #45A6FF, stop:1 #0074F0)")
                self.ACCENT_GRAD_PRESSED = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                                            "stop:0 #0F79E8, stop:1 #005BC4)")
                self.INPUT_BG = "rgba(255, 255, 255, 0.70)"     # v1 より透明側へ
                self.INPUT_BORDER = "rgba(0, 0, 0, 0.13)"
                self.BTN_BG = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                               "stop:0 rgba(255,255,255,0.88), stop:1 rgba(255,255,255,0.60))")
                self.BTN_HOVER = "rgba(255, 255, 255, 0.98)"
                self.BTN_PRESSED = "rgba(228, 232, 242, 0.92)"
                self.BTN_BORDER = "rgba(0, 0, 0, 0.10)"
                self.HOVER_BG = "rgba(0, 0, 0, 0.05)"
                # ライトの成功/警告は白カード上で読める濃い目の値にする
                self.SUCCESS = "#248A3D"
                self.WARNING = "#C76A00"
                self.TOGGLE_OFF = "#D9D9DE"
                self.SCROLL_HANDLE = "rgba(0, 0, 0, 0.25)"

    # ------------------------------------------------------------------
    # 個別ラベル用スタイル（実行時に動的に切り替えるステータス表示のみ）
    # ------------------------------------------------------------------

    @staticmethod
    def status_ok_style(dark_mode: bool = False) -> str:
        """「設定済み」「コピーしました」など成功ステータス用スタイル。"""
        c = MacTheme.Colors(dark_mode)
        return f"color: {c.SUCCESS}; font-size: {MacTheme.FONT_SIZE_CAPTION}px; font-weight: 600;"

    @staticmethod
    def status_warn_style(dark_mode: bool = False) -> str:
        """警告ステータス用スタイル。"""
        c = MacTheme.Colors(dark_mode)
        return f"color: {c.WARNING}; font-size: {MacTheme.FONT_SIZE_CAPTION}px;"

    @staticmethod
    def status_muted_style() -> str:
        """「未設定」など弱いステータス用スタイル（両テーマで読める中間グレー）。"""
        return f"color: #98989D; font-size: {MacTheme.FONT_SIZE_CAPTION}px;"

    @staticmethod
    def get_stylesheet(dark_mode: bool = False) -> str:
        """
        アプリケーション全体に適用するスタイルシートを取得する。

        Args:
            dark_mode: ダークモードの場合True

        Returns:
            Qt用スタイルシート文字列
        """
        c = MacTheme.Colors(dark_mode)
        theme = "dark" if dark_mode else "light"
        chevron_down = _icon_url(f"chevron_down_{theme}", _chevron_svg(c.SECONDARY_TEXT, "down"))
        chevron_up = _icon_url(f"chevron_up_{theme}", _chevron_svg(c.SECONDARY_TEXT, "up"))
        check_mark = _icon_url("check_white", _check_svg())

        return f"""
        /* ベース。背景は持たせず、面の色は各コンテナで明示する */
        QWidget {{
            font-family: {MacTheme.FONT_FAMILY};
            font-size: {MacTheme.FONT_SIZE_NORMAL}px;
            color: {c.TEXT};
            background: transparent;
        }}
        QWidget#settingsRoot, QDialog#onboardingRoot {{
            background-color: {c.BG_WINDOW};
        }}
        /* 右コンテンツ島（ページ見出し + 各ページ + 保存ボタンを載せる面）。
           サイドバー島とペアで backdrop の上に浮かせる。面はやや薄めにして層を分ける */
        QFrame#contentPane {{
            background-color: {c.CONTENT_BG};
            border: 1px solid {c.CARD_BORDER};
            border-top-color: {c.GLASS_EDGE};
            border-bottom-color: {c.EDGE_BOTTOM};
            border-radius: 16px;
        }}
        /* オンボーディングの中央 1 島（ヘッダ・ページ本体・ボタンを載せる面）。
           サイドバー島と同じ様式で backdrop の上に浮かせる */
        QFrame#onboardingIsland {{
            background-color: {c.BG_SIDEBAR};
            border: 1px solid {c.CARD_BORDER};
            border-top-color: {c.GLASS_EDGE};
            border-bottom-color: {c.EDGE_BOTTOM};
            border-radius: 16px;
        }}
        QMessageBox {{
            background-color: {c.BG_CONTENT};
        }}

        /* 左サイドバー島（ブランド + ナビゲーション）。backdrop の上に浮く角丸パネル。
           border 短縮形の後に border-top-color（明るいリム）と border-bottom-color（沈み）を
           置いて上書きし、上が光り下が沈むガラスの厚みを近似する */
        QFrame#sidebarPane {{
            background-color: {c.BG_SIDEBAR};
            border: 1px solid {c.CARD_BORDER};
            border-top-color: {c.GLASS_EDGE};
            border-bottom-color: {c.EDGE_BOTTOM};
            border-radius: 16px;
        }}
        QLabel#brand {{
            font-size: 16px;
            font-weight: 700;
            padding-left: 12px;
        }}
        QLabel#brandCaption {{
            font-size: 11px;
            color: {c.SECONDARY_TEXT};
            padding-left: 12px;
        }}
        QListWidget#sidebar {{
            background: transparent;
            border: none;
            outline: none;
        }}
        QListWidget#sidebar::item {{
            padding: 7px 10px;
            border-radius: {MacTheme.RADIUS_CONTROL}px;
            margin: 1px 2px;
            color: {c.TEXT};
            border: 1px solid transparent;   /* 選択時のリム分を常に予約し、選択で高さが跳ねないようにする */
        }}
        QListWidget#sidebar::item:hover:!selected {{
            background-color: {c.HOVER_BG};
        }}
        QListWidget#sidebar::item:selected {{
            background-color: {c.ACCENT_GRAD};
            color: white;
            border-top-color: rgba(255, 255, 255, 0.45);   /* 選択チップの上縁だけ白く光らせる */
        }}

        /* サイドバー開閉トグル（☰）。控えめなアイコンボタン */
        QPushButton#sidebarToggle {{
            background: transparent;
            border: none;
            border-radius: 6px;
            padding: 0;
            font-size: 15px;
            color: {c.SECONDARY_TEXT};
            min-height: 0;
        }}
        QPushButton#sidebarToggle:hover {{
            background-color: {c.HOVER_BG};
        }}
        QPushButton#sidebarToggle:pressed {{
            background-color: {c.BTN_PRESSED};
        }}

        /* セグメント風の期間切替ボタン（週 / 月 / 年）。選択でアクセント塗り */
        QPushButton[class="seg"] {{
            background-color: {c.BTN_BG};
            border: 1px solid {c.BTN_BORDER};
            border-radius: {MacTheme.RADIUS_CONTROL}px;
            padding: 2px 10px;
            margin-left: 4px;
            font-weight: 500;
            min-height: 0;
        }}
        QPushButton[class="seg"]:hover {{
            background-color: {c.BTN_HOVER};
        }}
        QPushButton[class="seg"]:checked {{
            background-color: {c.ACCENT_GRAD};
            color: white;
            border-color: {c.ACCENT};
        }}

        /* 見出し・補足 */
        QLabel#pageTitle {{
            font-size: {MacTheme.FONT_SIZE_TITLE}px;
            font-weight: 700;
        }}
        QLabel#caption {{
            color: {c.SECONDARY_TEXT};
            font-size: {MacTheme.FONT_SIZE_CAPTION}px;
        }}

        /* 設定カード（島より一段明るいガラス面）と行区切りヘアライン */
        QFrame#card {{
            background-color: {c.CARD_BG};
            border: 1px solid {c.CARD_BORDER};
            border-top-color: {c.GLASS_EDGE};   /* 1px の光る上縁。border 短縮形の「後」に置いて上書き */
            border-bottom-color: {c.EDGE_BOTTOM};   /* 下端は沈めて島の上に浮かせる */
            border-radius: {MacTheme.RADIUS_PANEL}px;
        }}
        QFrame#hairline {{
            background-color: {c.HAIRLINE};
            border: none;
        }}

        /* ボタン（透けるガラス面: 半透明グラデ土台＋1px トップリムで浮きを出す。
           半径はカプセル寄りにして「透けている軽いピル」に見せる） */
        QPushButton {{
            background-color: {c.BTN_BG};
            border: 1px solid {c.BTN_BORDER};
            border-top-color: {c.GLASS_EDGE};   /* 上縁リム。border 短縮形の後に置く */
            border-radius: 14px;
            padding: 5px 14px;
            min-height: 20px;
            font-weight: 500;
        }}
        QPushButton:hover {{
            background-color: {c.BTN_HOVER};
        }}
        QPushButton:pressed {{
            background-color: {c.BTN_PRESSED};
            border-top-color: {c.BTN_BORDER};   /* 押下でトップリムを消す＝沈み込み表現 */
        }}
        QPushButton:disabled {{
            color: {c.SECONDARY_TEXT};
            background-color: transparent;
            border-color: {c.HAIRLINE};
        }}
        QPushButton[class="primary"] {{
            background-color: {c.ACCENT_GRAD};
            color: white;
            border: 1px solid rgba(255, 255, 255, 0.28);   /* 青地上なので両テーマ共通の直書き */
            border-top-color: rgba(255, 255, 255, 0.50);   /* 上縁の白リムを強めてガラスの厚みを出す */
            font-weight: 600;
            padding: 5px 17px;   /* border:none→1px 化に伴う -1px 補正（旧 6px 18px） */
        }}
        QPushButton[class="primary"]:hover {{
            background-color: {c.ACCENT_GRAD_HOVER};
        }}
        QPushButton[class="primary"]:pressed {{
            background-color: {c.ACCENT_GRAD_PRESSED};
        }}

        /* 入力欄・コンボボックス・スピンボックス */
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
            background-color: {c.INPUT_BG};
            border: 1px solid {c.INPUT_BORDER};
            border-radius: {MacTheme.RADIUS_CONTROL}px;
            padding: 4px 10px;
            selection-background-color: {c.ACCENT};
            color: {c.TEXT};
            min-height: 22px;
        }}
        QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
            border-color: {c.SECONDARY_TEXT};
        }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
            border: 2px solid {c.ACCENT};
            padding: 3px 9px;
        }}

        /* コンボボックスの矢印は SVG で描く（ボーダートリックは崩れるため不使用） */
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: center right;
            width: 24px;
            border: none;
        }}
        QComboBox::down-arrow {{
            image: url("{chevron_down}");
            width: 12px;
            height: 12px;
            margin-right: 6px;
        }}
        QComboBox QAbstractItemView {{
            border: 1px solid {c.INPUT_BORDER};
            border-radius: {MacTheme.RADIUS_CONTROL}px;
            background-color: {c.POPUP_BG};   /* 別トップレベルのため不透明 hex 必須（rgba/グラデ禁止） */
            selection-background-color: {c.ACCENT};
            selection-color: white;
            padding: 4px;
            outline: none;
            color: {c.TEXT};
        }}
        QComboBox QAbstractItemView::item {{
            padding: 5px 10px;
            border-radius: 5px;
            min-height: 20px;
        }}
        QComboBox QAbstractItemView::item:hover {{
            background-color: {c.HOVER_BG};
            color: {c.TEXT};
        }}
        QComboBox QAbstractItemView::item:selected {{
            background-color: {c.ACCENT};
            color: white;
        }}

        /* スピンボックスの上下ボタンも SVG シェブロンで統一する */
        QSpinBox::up-button, QSpinBox::down-button,
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
            subcontrol-origin: border;
            width: 18px;
            border: none;
            background: transparent;
        }}
        QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
            image: url("{chevron_up}");
            width: 9px;
            height: 9px;
        }}
        QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
            image: url("{chevron_down}");
            width: 9px;
            height: 9px;
        }}

        /* メニュー */
        QMenu {{
            background-color: {c.POPUP_BG};   /* 別トップレベルのため不透明 hex 必須（rgba/グラデ禁止） */
            border: 1px solid {c.CARD_BORDER};
            border-radius: {MacTheme.RADIUS_CONTROL}px;
            padding: 4px;
        }}
        QMenu::item {{
            padding: 4px 20px;
            border-radius: 5px;
            color: {c.TEXT};
        }}
        QMenu::item:selected {{
            background-color: {c.ACCENT};
            color: white;
        }}

        /* チェックボックス（トグル化していない箇所のフォールバック） */
        QCheckBox {{
            spacing: 8px;
        }}
        QCheckBox::indicator {{
            width: 18px;
            height: 18px;
            background: {c.INPUT_BG};
            border: 1px solid {c.INPUT_BORDER};
            border-radius: 5px;
        }}
        QCheckBox::indicator:hover {{
            border-color: {c.ACCENT};
        }}
        QCheckBox::indicator:checked {{
            background-color: {c.ACCENT};
            border-color: {c.ACCENT};
            image: url("{check_mark}");
        }}

        /* テキストエリア */
        QTextEdit, QPlainTextEdit {{
            background-color: {c.INPUT_BG};
            color: {c.TEXT};
            border: 1px solid {c.INPUT_BORDER};
            border-radius: {MacTheme.RADIUS_CONTROL}px;
            padding: 8px;
            selection-background-color: {c.ACCENT};
        }}
        QTextEdit:focus, QPlainTextEdit:focus {{
            border: 2px solid {c.ACCENT};
            padding: 7px;
        }}

        /* スライダー */
        QSlider::groove:horizontal {{
            height: 4px;
            background: {c.TOGGLE_OFF};
            border-radius: 2px;
        }}
        QSlider::sub-page:horizontal {{
            background: {c.ACCENT};
            border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            width: 18px;
            height: 18px;
            margin: -7px 0;
            border-radius: 9px;
            background: #FFFFFF;
            border: 1px solid rgba(0, 0, 0, 0.18);
        }}
        QSlider::handle:horizontal:hover {{
            border-color: {c.ACCENT};
        }}

        /* スクロールエリア（ページの内容） */
        QScrollArea {{
            border: none;
            background: transparent;
        }}

        /* リスト（履歴ページ） */
        QListWidget {{
            background-color: {c.CARD_BG};
            border: 1px solid {c.CARD_BORDER};
            border-top-color: {c.GLASS_EDGE};   /* カードと同じ上縁ハイライト（ウィンドウ内なので半透明可） */
            border-radius: {MacTheme.RADIUS_PANEL}px;
            padding: 4px;
            outline: none;
        }}
        QListWidget::item {{
            padding: 8px 10px;
            border-radius: 6px;
            color: {c.TEXT};
        }}
        QListWidget::item:hover {{
            background-color: {c.HOVER_BG};
        }}
        QListWidget::item:selected {{
            background-color: {c.ACCENT};
            color: white;
        }}

        /* スクロールバー */
        QScrollBar:vertical {{
            border: none;
            background: transparent;
            width: 10px;
            margin: 2px;
        }}
        QScrollBar::handle:vertical {{
            background: {c.SCROLL_HANDLE};
            min-height: 30px;
            border-radius: 4px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
            border: none;
            background: none;
        }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: none;
        }}
        """
