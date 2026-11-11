"""
UI プレビュー生成ツール（開発用・オフスクリーン）

設定ウィンドウの全ページ・録音中のピル（HUD）の各状態・トレイアイコンを、ライト／ダークで
PNG に書き出して目視確認する。Windows 実機が無くても見た目を確かめるためのもの
（GitHub の Windows テストでも回し、実 Windows のフォント・描画の PNG を artifact に残す）。

使い方:
    QT_QPA_PLATFORM=offscreen venv/bin/python scripts/dev/preview_ui.py [出力先] [--appearance light|dark]
    出力先の既定は /tmp/voicekey_ui_preview。--appearance を省くと両方撮る。
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# オフスクリーン描画（ウィンドウを実画面に出さない）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 実 Keychain / Credential Manager に絶対に触れない（許可ダイアログ事故防止の恒久ルール）
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

# リポジトリ直下を import パスへ（scripts/dev/ から 2 つ上）
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

# 撮影中は鍵・マイク・自動起動を実物に問い合わせない（値は見た目の確認に足る偽物）
_PATCHES = [
    mock.patch("src.utils.secrets.is_keyring_available", return_value=True),
    mock.patch("src.utils.secrets.get_api_key", return_value=None),
    mock.patch(
        "src.core.audio_recorder.AudioRecorder.list_input_devices",
        return_value=[{"id": 1, "label": "マイク (Realtek Audio)"}, {"id": 3, "label": "USB マイク"}],
    ),
    mock.patch("src.utils.autostart.is_supported", return_value=True),
    mock.patch("src.utils.autostart.is_enabled", return_value=False),
    # 本物の議事録を画像に写さない（空の一時フォルダを読ませる）
    mock.patch("src.meetbot.transcript.default_directory", return_value=Path(tempfile.mkdtemp())),
]


def grab_settings(app, mode: str, out: Path) -> int:
    """設定ウィンドウの全ページを 2 つの大きさで撮る。撮った枚数を返す。"""
    import yaml

    from src.config.config_manager import ConfigManager
    from src.ui.settings_window import SettingsWindow

    path = Path(tempfile.mkdtemp()) / "settings.yaml"
    path.write_text(yaml.dump({"language": "ja", "appearance": mode}, allow_unicode=True), encoding="utf-8")
    window = SettingsWindow(config_manager=ConfigManager(config_path=str(path)))
    window.show()
    keys = list(window._keys)  # 並び順の key（撮影用なので内部の一覧をそのまま読む）
    count = 0
    for width, height in ((1100, 760), (760, 600)):
        window.resize(width, height)
        for key in keys:
            window.select(key, animate=False)
            for _ in range(3):
                app.processEvents()
            window.grab().save(str(out / f"settings-{key}-{width}x{height}-{mode}.png"))
            count += 1
    window.close()
    return count


def grab_hud(mode: str, out: Path) -> int:
    """HUD の各状態（待機ピル／録音／ハンズフリー／自動 Enter／翻訳の印／変換中／通知）を撮る。"""
    from src.ui.hud import Hud

    # (名前, 状態, 通知文, ハンズフリーか, 翻訳の印)
    states = [
        ("idle", "idle", None, False, ""),
        ("recording", "recording", None, False, ""),
        ("recording-hands-free", "recording", None, True, ""),
        ("recording-auto-enter", "recording_auto_enter", None, False, ""),
        ("recording-translate", "recording", None, False, "→EN"),
        ("transcribing", "transcribing", None, False, ""),
        ("notice", None, "音声を検出できませんでした", False, ""),
    ]
    # 透過ピルが見えるよう、外観に合わせたデスクトップ風の下地に合成する
    backdrop = QColor("#1F1F22" if mode == "dark" else "#D9D6D0")
    for name, state, notice, hands_free, badge in states:
        hud = Hud(enabled=True)
        hud.always_visible = True  # 待機ピル（idle）を出せるようにする
        if state:
            if hands_free:
                hud.set_hands_free(True)  # 状態より先に着色情報を届ける実運用と同じ順
            hud.set_state(state)
            if badge:
                hud.set_translate_badge(badge)
            if state.startswith("recording"):
                # 波形バーに動きを付ける（発話風のレベル列）
                for i in range(hud.BAR_COUNT):
                    hud.push_level(0.15 + 0.75 * abs((i % 8) - 4) / 4)
        else:
            hud.show_notice(notice)
        hud.settle()
        canvas = QPixmap(hud.size())
        canvas.fill(backdrop)
        painter = QPainter(canvas)
        hud.render(painter, QPoint(0, 0))
        painter.end()
        canvas.save(str(out / f"hud-{name}-{mode}.png"))
        hud._notice_timer.stop()
        hud.deleteLater()
    return len(states)


def grab_tray(out: Path) -> int:
    """トレイアイコン 4 状態を 1 枚に並べて撮る。"""
    from src.config.types import AppState
    from src.ui.system_tray import SystemTray

    tray = SystemTray(platform_adapter=object())
    size, gap = 64, 16
    states = [AppState.IDLE, AppState.RECORDING, AppState.TRANSCRIBING, AppState.RECORDING_AUTO_ENTER]
    canvas = QPixmap(size * len(states) + gap * (len(states) + 1), size + gap * 2)
    for name, color in (("light", "#F3F3F3"), ("dark", "#202020")):
        canvas.fill(QColor(color))
        painter = QPainter(canvas)
        for i, state in enumerate(states):
            tray.set_status(state)
            painter.drawPixmap(gap + i * (size + gap), gap, tray.icon().pixmap(size, size))
        painter.end()
        canvas.save(str(out / f"tray-icons-on-{name}.png"))
    tray.hide()
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="voicekey の UI を PNG に書き出す")
    parser.add_argument("out", nargs="?", default="/tmp/voicekey_ui_preview")
    parser.add_argument("--appearance", choices=("light", "dark"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    for patch in _PATCHES:
        patch.start()
    from src.ui.theme import Theme

    total = 0
    for mode in [args.appearance] if args.appearance else ["light", "dark"]:
        Theme.instance().set_mode(mode)
        total += grab_settings(app, mode, out)
        total += grab_hud(mode, out)
    total += grab_tray(out)
    print(f"==> 出力: {out}（{total} 枚）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
