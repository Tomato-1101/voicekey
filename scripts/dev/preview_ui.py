"""
UI プレビュー生成ツール（開発用・オフスクリーン）

設定ウィンドウの全タブ（ダーク/ライト）・HUD の各状態・トレイアイコンを
PNG に書き出して目視確認する。Windows 実機がなくても UI 再デザインの
見た目を確認するためのもの。

使い方:
    QT_QPA_PLATFORM=offscreen venv/bin/python scripts/dev/preview_ui.py
    → /tmp/voicekey_ui_preview/ に PNG が出力される
"""

import os
import sys
from pathlib import Path

# オフスクリーン描画（ウィンドウを実画面に出さない）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# リポジトリ直下を import パスへ（scripts/dev/ から 2 つ上）
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# 実 Keychain / Credential Manager に絶対に触れない（許可ダイアログ事故防止の恒久ルール）
from src.utils import secrets  # noqa: E402

secrets._keyring_module = None
secrets._embedded = None

from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT_DIR = Path("/tmp/voicekey_ui_preview")


def grab_settings_tabs(app) -> dict:
    """設定ウィンドウをダーク/ライト × 全ページで撮影する。"""
    from src.ui.settings_window import SettingsWindow

    shots = {}
    for is_dark in (False, True):
        window = SettingsWindow()
        window._is_dark_mode = is_dark
        window._apply_theme(is_dark)
        window.resize(720, 600)
        theme = "dark" if is_dark else "light"
        for index in range(window._nav.count()):
            window._nav.setCurrentRow(index)
            app.processEvents()
            name = window._nav.item(index).text().replace(" ", "")
            shots[f"settings_{theme}_{index}_{name}"] = window.grab()
        window.deleteLater()
    return shots


def grab_hud_states() -> dict:
    """HUD の 3 状態 + 通知を、デスクトップ風背景に合成して撮影する。"""
    from src.ui.hud import Hud

    shots = {}
    states = [
        ("hud_recording", "recording", None),
        ("hud_recording_auto_enter", "recording_auto_enter", None),
        ("hud_transcribing", "transcribing", None),
        ("hud_notice", None, "音声を検出できませんでした"),
    ]
    for name, state, notice in states:
        hud = Hud(enabled=True)
        if state:
            hud.set_state(state)
            if state.startswith("recording"):
                # 波形バーに動きを付ける（発話風のレベル列）
                for i in range(hud.BAR_COUNT):
                    hud.push_level(0.15 + 0.75 * abs((i % 8) - 4) / 4)
        else:
            hud.show_notice(notice)
        # 透過ピルが見えるようデスクトップ風グレーに合成する
        canvas = QPixmap(hud.size())
        canvas.fill(QColor("#6E6E73"))
        painter = QPainter(canvas)
        hud.render(painter, QPoint(0, 0))
        painter.end()
        shots[name] = canvas
        hud._notice_timer.stop()
        hud._spin_timer.stop()
        hud.deleteLater()
    return shots


def grab_tray_icons() -> dict:
    """トレイアイコン 4 状態を 1 枚に並べて撮影する。"""
    from src.config.types import AppState
    from src.ui.system_tray import SystemTray

    tray = SystemTray()
    size = 64
    states = [AppState.IDLE, AppState.RECORDING, AppState.TRANSCRIBING, AppState.RECORDING_AUTO_ENTER]
    canvas = QPixmap(size * len(states) + 16 * (len(states) + 1), size + 32)
    canvas.fill(QColor("#FFFFFF"))
    painter = QPainter(canvas)
    for i, state in enumerate(states):
        tray.set_status(state)
        icon_pixmap = tray.icon().pixmap(size, size)
        painter.drawPixmap(16 + i * (size + 16), 16, icon_pixmap)
    painter.end()
    tray.hide()
    return {"tray_icons": canvas}


def make_sheet(shots: dict, keys: list, columns: int, path: Path) -> None:
    """複数ショットを格子状に並べた 1 枚画像を作る。"""
    pixmaps = [shots[k] for k in keys if k in shots]
    if not pixmaps:
        return
    gap = 16
    cell_w = max(p.width() for p in pixmaps)
    cell_h = max(p.height() for p in pixmaps)
    rows = (len(pixmaps) + columns - 1) // columns
    sheet = QPixmap(columns * cell_w + gap * (columns + 1), rows * cell_h + gap * (rows + 1))
    sheet.fill(QColor("#3A3A3C"))
    painter = QPainter(sheet)
    for i, pixmap in enumerate(pixmaps):
        x = gap + (i % columns) * (cell_w + gap)
        y = gap + (i // columns) * (cell_h + gap)
        painter.drawPixmap(QRect(x, y, pixmap.width(), pixmap.height()), pixmap)
    painter.end()
    sheet.save(str(path))


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    app = QApplication([])

    shots = {}
    shots.update(grab_settings_tabs(app))
    shots.update(grab_hud_states())
    shots.update(grab_tray_icons())

    for name, pixmap in shots.items():
        pixmap.save(str(OUT_DIR / f"{name}.png"))

    # まとめシート（ユーザー提示用）: ライト全タブ / ダーク全タブ / HUD+トレイ
    light_keys = sorted(k for k in shots if k.startswith("settings_light"))
    dark_keys = sorted(k for k in shots if k.startswith("settings_dark"))
    hud_keys = [k for k in shots if k.startswith("hud_")] + ["tray_icons"]
    make_sheet(shots, light_keys, 3, OUT_DIR / "sheet_light.png")
    make_sheet(shots, dark_keys, 3, OUT_DIR / "sheet_dark.png")
    make_sheet(shots, hud_keys, 1, OUT_DIR / "sheet_hud_tray.png")

    print(f"==> 出力: {OUT_DIR}（{len(shots)} ショット + まとめ 3 枚）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
