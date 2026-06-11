"""
UIスタイル・テーマ定義モジュール

クロスプラットフォーム向けのUIテーマ（カラー、フォント、スタイルシート）を定義する。
ダークモード/ライトモードの切り替えに対応。
"""

from ..platform import get_platform_adapter


class MacTheme:
    """
    macOS風テーマのカラーとフォント定義。
    
    ダークモード/ライトモードに対応したカラーパレットと、
    Qt全体に適用するスタイルシートを提供する。
    """
    
    # 共通フォント設定（OSごとに適切なフォールバックを使用）
    FONT_FAMILY = get_platform_adapter().font_css_stack
    FONT_SIZE_NORMAL = 13        # 標準フォントサイズ

    class Colors:
        """
        テーマ別カラーパレット。
        
        Attributes:
            BACKGROUND: 全体背景色
            WINDOW_BG: ウィンドウ背景色
            TEXT: テキスト色
            ACCENT: アクセントカラー
            ACCENT_HOVER: アクセントカラー（ホバー時）
            SECONDARY_TEXT: 二次テキスト色
            BORDER: ボーダー色
            INPUT_BG: 入力欄背景色
            HOVER_BG: ホバー時背景色
        """
        
        def __init__(self, is_dark: bool):
            """
            カラーパレットを初期化する。
            
            Args:
                is_dark: ダークモードの場合True
            """
            if is_dark:
                # ダークモードカラー
                self.BACKGROUND = "#1E1E1E"
                self.WINDOW_BG = "#2D2D2D"
                self.TEXT = "#FFFFFF"
                self.ACCENT = "#0A84FF"  # ダークモード用明るい青
                self.ACCENT_HOVER = "#409CFF"
                self.SECONDARY_TEXT = "#A1A1A6"
                self.BORDER = "#424242"
                self.INPUT_BG = "#1E1E1E"
                self.HOVER_BG = "rgba(255, 255, 255, 0.1)"
            else:
                # ライトモードカラー
                self.BACKGROUND = "#F5F5F7"
                self.WINDOW_BG = "#FFFFFF"
                self.TEXT = "#1D1D1F"
                self.ACCENT = "#007AFF"
                self.ACCENT_HOVER = "#0062CC"
                self.SECONDARY_TEXT = "#86868B"
                self.BORDER = "#D1D1D1"
                self.INPUT_BG = "#FFFFFF"
                self.HOVER_BG = "rgba(0, 0, 0, 0.05)"

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
        
        return f"""
        /* 基本ウィジェット */
        QWidget {{
            font-family: {MacTheme.FONT_FAMILY};
            font-size: {MacTheme.FONT_SIZE_NORMAL}px;
            color: {c.TEXT};
            background-color: {c.BACKGROUND};
        }}
        
        /* ボタン */
        QPushButton {{
            background-color: {c.WINDOW_BG};
            border: 1px solid {c.BORDER};
            border-radius: 6px;
            padding: 5px 14px;
            min-height: 24px;
            font-weight: 500;
        }}
        QPushButton:hover {{
            background-color: {c.HOVER_BG};
            border-color: {c.SECONDARY_TEXT};
        }}
        QPushButton:pressed {{
            background-color: {c.ACCENT};
            color: white;
            border-color: {c.ACCENT};
        }}
        QPushButton[class="primary"] {{
            background-color: {c.ACCENT};
            color: white;
            border: none;
            font-weight: 600;
        }}
        QPushButton[class="primary"]:hover {{
            background-color: {c.ACCENT_HOVER};
        }}
        
        /* 入力欄・コンボボックス */
        QLineEdit, QComboBox, QSpinBox {{
            background-color: {c.INPUT_BG};
            border: 1px solid {c.BORDER};
            border-radius: 6px;
            padding: 5px 10px;
            selection-background-color: {c.ACCENT};
            color: {c.TEXT};
            min-height: 22px;
        }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
            border: 2px solid {c.ACCENT};
            padding: 4px 9px;
        }}
        
        /* コンボボックス ドロップダウン */
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 20px;
            border-left-width: 0px;
            border-top-right-radius: 6px;
            border-bottom-right-radius: 6px;
        }}
        QComboBox::down-arrow {{
            image: none;
            border-left: 2px solid {c.SECONDARY_TEXT};
            border-bottom: 2px solid {c.SECONDARY_TEXT};
            width: 6px;
            height: 6px;
            margin-right: 8px;
            margin-top: -2px;
        }}
        QComboBox QAbstractItemView {{
            border: 1px solid {c.BORDER};
            border-radius: 6px;
            background-color: {c.WINDOW_BG};
            selection-background-color: {c.ACCENT};
            selection-color: white;
            padding: 4px;
            outline: none;
            color: {c.TEXT};
        }}
        QComboBox QAbstractItemView::item {{
            padding: 6px 10px;
            border-radius: 4px;
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

        /* メニュー */
        QMenu {{
            background-color: {c.WINDOW_BG};
            border: 1px solid {c.BORDER};
            border-radius: 6px;
            padding: 4px;
        }}
        QMenu::item {{
            padding: 4px 20px;
            border-radius: 4px;
            color: {c.TEXT};
        }}
        QMenu::item:selected {{
            background-color: {c.ACCENT};
            color: white;
        }}

        /* チェックボックス */
        QCheckBox {{
            spacing: 8px;
        }}
        QCheckBox::indicator {{
            width: 18px;
            height: 18px;
            background: {c.INPUT_BG};
            border: 1px solid {c.BORDER};
            border-radius: 4px;
        }}
        QCheckBox::indicator:checked {{
            background-color: {c.ACCENT};
            border-color: {c.ACCENT};
            image: url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJ3aGl0ZSIgc3Ryb2tlLXdpZHRoPSIzIiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiPjxwb2x5bGluZSBwb2ludHM9IjIwIDYgOSAxNyA0IDEyIi8+PC9zdmc+);
        }}
        
        /* テキストエリア */
        QTextEdit, QPlainTextEdit {{
            background-color: {c.INPUT_BG};
            color: {c.TEXT};
            border: 1px solid {c.BORDER};
            border-radius: 6px;
            padding: 8px;
            font-family: {MacTheme.FONT_FAMILY};
            font-size: {MacTheme.FONT_SIZE_NORMAL}px;
            selection-background-color: {c.ACCENT};
        }}
        QTextEdit:focus, QPlainTextEdit:focus {{
            border: 2px solid {c.ACCENT};
            padding: 7px;
        }}

        /* ダブルスピンボックス */
        QDoubleSpinBox {{
            background-color: {c.INPUT_BG};
            border: 1px solid {c.BORDER};
            border-radius: 6px;
            padding: 5px 10px;
            selection-background-color: {c.ACCENT};
            color: {c.TEXT};
            min-height: 22px;
        }}
        QDoubleSpinBox:focus {{
            border: 2px solid {c.ACCENT};
            padding: 4px 9px;
        }}

        /* プレースホルダーテキスト */
        QLineEdit::placeholder, QTextEdit::placeholder {{
            color: {c.SECONDARY_TEXT};
            font-style: italic;
        }}

        /* グループボックス */
        QGroupBox {{
            background-color: {c.WINDOW_BG};
            border: 1px solid {c.BORDER};
            border-radius: 8px;
            margin-top: 16px;
            padding-top: 20px;
            padding-bottom: 16px;
            padding-left: 16px;
            padding-right: 16px;
            font-size: 14px;
            font-weight: 600;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 0 5px;
            color: {c.TEXT};
            font-weight: 600;
            left: 12px;
        }}
        
        /* タブ（設定ウィンドウの 一般 / ホットキー / API キー） */
        QTabWidget::pane {{
            border: 1px solid {c.BORDER};
            border-radius: 8px;
            top: 4px;
        }}
        QTabBar::tab {{
            background: transparent;
            border: none;
            border-radius: 6px;
            padding: 6px 16px;
            margin: 0 2px;
            color: {c.TEXT};
        }}
        QTabBar::tab:selected {{
            background-color: {c.ACCENT};
            color: white;
            font-weight: 600;
        }}
        QTabBar::tab:hover:!selected {{
            background-color: {c.HOVER_BG};
        }}

        /* スクロールエリア（タブページの内容） */
        QScrollArea {{
            border: none;
            background: transparent;
        }}
        
        /* スクロールバー */
        QScrollBar:vertical {{
            border: none;
            background: transparent;
            width: 8px;
            margin: 0px;
        }}
        QScrollBar::handle:vertical {{
            background: {c.SECONDARY_TEXT};
            min-height: 20px;
            border-radius: 4px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            border: none;
            background: none;
        }}
        """
