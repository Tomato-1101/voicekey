"""
UIスタイル・テーマ定義モジュール

クロスプラットフォーム向けのUIテーマ（カラー、フォント、スタイルシート）を定義する。
ダークモード/ライトモードの切り替えに対応。

デザイン方針（2026-06 再設計）:
- グラデーションを使わないフラットモダン（Linear / Raycast / macOS System Settings 系）
- 面の色はコンテナ側で明示する（サイドバー / コンテンツ / カード の 3 層）。
  グローバルの QWidget には背景を持たせない（カード上のラベル等が背景色で潰れるため）
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

    # 角丸体系（コントロール = 7px / カード・リスト = 10px）
    RADIUS_CONTROL = 7
    RADIUS_PANEL = 10

    class Colors:
        """
        テーマ別カラーパレット（フラット 3 層: サイドバー / コンテンツ / カード）。

        Attributes:
            BG_SIDEBAR: 左サイドバー背景
            BG_CONTENT: コンテンツ領域背景
            CARD_BG: 設定カード背景
            CARD_BORDER: カードの輪郭線
            HAIRLINE: カード内の行区切り線
            TEXT: テキスト色
            SECONDARY_TEXT: 二次テキスト色
            ACCENT / ACCENT_HOVER / ACCENT_PRESSED: アクセント（青）
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
                self.BG_SIDEBAR = "#19191B"
                self.BG_CONTENT = "#212124"
                self.CARD_BG = "#2A2A2E"
                self.CARD_BORDER = "rgba(255, 255, 255, 0.07)"
                self.HAIRLINE = "rgba(255, 255, 255, 0.08)"
                self.TEXT = "#F2F2F7"
                self.SECONDARY_TEXT = "#9A9AA2"
                self.ACCENT = "#0A84FF"
                self.ACCENT_HOVER = "#2B95FF"
                self.ACCENT_PRESSED = "#0774E0"
                self.INPUT_BG = "#1D1D20"
                self.INPUT_BORDER = "rgba(255, 255, 255, 0.13)"
                self.BTN_BG = "#3A3A3F"
                self.BTN_HOVER = "#46464C"
                self.BTN_PRESSED = "#313136"
                self.BTN_BORDER = "rgba(255, 255, 255, 0.10)"
                self.HOVER_BG = "rgba(255, 255, 255, 0.07)"
                self.SUCCESS = "#32D74B"
                self.WARNING = "#FF9F0A"
                self.TOGGLE_OFF = "#48484E"
                self.SCROLL_HANDLE = "rgba(255, 255, 255, 0.25)"
            else:
                self.BG_SIDEBAR = "#EBEBEE"
                self.BG_CONTENT = "#F5F5F7"
                self.CARD_BG = "#FFFFFF"
                self.CARD_BORDER = "rgba(0, 0, 0, 0.06)"
                self.HAIRLINE = "rgba(0, 0, 0, 0.07)"
                self.TEXT = "#1D1D1F"
                self.SECONDARY_TEXT = "#828287"
                self.ACCENT = "#007AFF"
                self.ACCENT_HOVER = "#1A88FF"
                self.ACCENT_PRESSED = "#0068DA"
                self.INPUT_BG = "#FFFFFF"
                self.INPUT_BORDER = "rgba(0, 0, 0, 0.14)"
                self.BTN_BG = "#FFFFFF"
                self.BTN_HOVER = "#F2F2F5"
                self.BTN_PRESSED = "#E5E5EA"
                self.BTN_BORDER = "rgba(0, 0, 0, 0.12)"
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
        QWidget#settingsRoot {{
            background-color: {c.BG_CONTENT};
        }}
        QMessageBox {{
            background-color: {c.BG_CONTENT};
        }}

        /* 左サイドバー（ブランド + ナビゲーション） */
        QFrame#sidebarPane {{
            background-color: {c.BG_SIDEBAR};
            border: none;
            border-right: 1px solid {c.HAIRLINE};
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
        }}
        QListWidget#sidebar::item:hover:!selected {{
            background-color: {c.HOVER_BG};
        }}
        QListWidget#sidebar::item:selected {{
            background-color: {c.ACCENT};
            color: white;
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
            background-color: {c.ACCENT};
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

        /* 設定カードと行区切りヘアライン */
        QFrame#card {{
            background-color: {c.CARD_BG};
            border: 1px solid {c.CARD_BORDER};
            border-radius: {MacTheme.RADIUS_PANEL}px;
        }}
        QFrame#hairline {{
            background-color: {c.HAIRLINE};
            border: none;
        }}

        /* ボタン（フラット。グラデーションは使わない） */
        QPushButton {{
            background-color: {c.BTN_BG};
            border: 1px solid {c.BTN_BORDER};
            border-radius: {MacTheme.RADIUS_CONTROL}px;
            padding: 5px 14px;
            min-height: 20px;
            font-weight: 500;
        }}
        QPushButton:hover {{
            background-color: {c.BTN_HOVER};
        }}
        QPushButton:pressed {{
            background-color: {c.BTN_PRESSED};
        }}
        QPushButton:disabled {{
            color: {c.SECONDARY_TEXT};
            background-color: transparent;
            border-color: {c.HAIRLINE};
        }}
        QPushButton[class="primary"] {{
            background-color: {c.ACCENT};
            color: white;
            border: none;
            font-weight: 600;
            padding: 6px 18px;
        }}
        QPushButton[class="primary"]:hover {{
            background-color: {c.ACCENT_HOVER};
        }}
        QPushButton[class="primary"]:pressed {{
            background-color: {c.ACCENT_PRESSED};
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
            background-color: {c.CARD_BG};
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
            background-color: {c.CARD_BG};
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
