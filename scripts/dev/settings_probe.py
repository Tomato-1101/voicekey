"""
設定ウィンドウと録音中のピル（HUD）を本物の画面に出して撮る（Windows の CI 用）

オフスクリーン描画（preview_ui.py）では日本語の字形が欠けて豆腐になり、OS の文字描画（ClearType）も通らない。
フォントと文字の見え方は本物の画面でしか確かめられないので、窓を出して画面ごと撮る。
あわせて、アクセント色を「Windows のアクセント色」にしたときにレジストリの色が取れるかも見る。

使い方: python scripts/dev/settings_probe.py <出力フォルダ>
"""

import os
import sys
import tempfile
import time
from pathlib import Path

# 実 Keychain / 資格情報マネージャーに絶対に触れない（preview_ui と同じ恒久ルール）
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

# 窓を出すページ（ダッシュボード・行の多いページ・議事録）
_PAGES = ("home", "slot1", "general", "meeting")


def _pump(app: QApplication, seconds: float) -> None:
    """窓が描かれて OS に合成されるまで、イベントを回して待つ。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def _grab(app: QApplication, rect: QRect, path: Path) -> None:
    """画面の指定範囲を撮って保存する。"""
    app.primaryScreen().grabWindow(0, rect.x(), rect.y(), rect.width(), rect.height()).save(str(path))


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "ui-preview")
    out.mkdir(parents=True, exist_ok=True)
    # QApplication を先に作る（preview_ui は import 時に offscreen を既定にするが、作った後なら効かない）
    app = QApplication(sys.argv)

    import preview_ui  # noqa: E402  撮影用の偽物（鍵・マイク・自動起動・議事録フォルダ）を使い回す
    import yaml

    from src.config.config_manager import ConfigManager
    from src.ui import theme
    from src.ui.hud import Hud
    from src.ui.settings_window import SettingsWindow

    for patch in preview_ui._PATCHES:
        patch.start()

    # (外観, アクセント)。最後の 1 枚は Windows のアクセント色が実際に取れるかを見る
    for mode, accent in (("light", theme.ACCENT_BRAND), ("dark", theme.ACCENT_BRAND), ("dark", theme.ACCENT_SYSTEM)):
        path = Path(tempfile.mkdtemp()) / "settings.yaml"
        path.write_text(
            yaml.dump({"language": "ja", "appearance": mode, "accent_color": accent}, allow_unicode=True),
            encoding="utf-8",
        )
        window = SettingsWindow(config_manager=ConfigManager(config_path=str(path)))
        window.resize(1100, 760)
        window.move(40, 40)
        window.show()
        window.raise_()
        _pump(app, 1.5)
        pages = _PAGES if accent == theme.ACCENT_BRAND else ("general",)
        for key in pages:
            window.select(key, animate=False)
            _pump(app, 0.6)
            name = f"settings-real-{key}-{mode}{'-system-accent' if accent != theme.ACCENT_BRAND else ''}.png"
            _grab(app, window.frameGeometry(), out / name)
            print(f"[PROBE] {name} accent={theme.palette().accent.name()}")
        window.close()
        _pump(app, 0.3)

        if accent == theme.ACCENT_BRAND:
            hud = Hud(enabled=True)
            hud.set_state("recording")
            for i in range(hud.BAR_COUNT):
                hud.push_level(0.15 + 0.75 * abs((i % 8) - 4) / 4)
            _pump(app, 1.0)
            geo = hud.frameGeometry()
            _grab(app, geo.adjusted(-12, -12, 12, 12), out / f"hud-real-recording-{mode}.png")
            print(f"[PROBE] hud-real-recording-{mode}.png visible={hud.isVisible()} geo={geo.getRect()}")
            hud.set_state("idle")
            hud.deleteLater()
            _pump(app, 0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
