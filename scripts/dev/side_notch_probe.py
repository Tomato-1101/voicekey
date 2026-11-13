"""
履歴パネル（画面左端のサイドノッチ）を本物の画面に出して撮る（Windows の CI 用）

オフスクリーン描画（preview_ui.py）では、OS が窓を合成する段（半透明窓・背景のぼかし）を通らない。
「実機で中身が出ない」の類いはその段で起きるので、本物の画面に出して画面ごと撮る。

使い方: python scripts/dev/side_notch_probe.py <出力フォルダ>
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


class _FakeHistory:
    """撮影用の履歴（本物の履歴ファイルに触れない）。"""

    def items(self):
        return [
            {"text": "明日の会議は 10 時からに変更になりました。資料は共有フォルダに置いてあります。", "date": "2026-11-13T09:00:00+09:00"},
            {"text": "了解です。", "date": "2026-11-13T08:30:00+09:00"},
            {"text": "Please review the pull request when you have a moment.", "date": "2026-11-12T20:00:00+09:00"},
        ]


def _pump(app: QApplication, seconds: float) -> None:
    """窓が描かれて OS に合成されるまで、イベントを回して待つ。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "ui-preview")
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)

    from src.ui.side_notch import SideNotch
    from src.ui.theme import Theme

    for dark in (False, True):
        Theme.instance().set_mode("dark" if dark else "light")
        notch = SideNotch(history=_FakeHistory(), enabled=True)
        notch.open_panel()
        _pump(app, 1.5)
        panel = notch._panel
        geo = panel.frameGeometry()
        # 少し外側まで撮って、角丸の外が透けているかも見る
        area = QRect(geo.x() - 4, geo.y() - 12, geo.width() + 24, geo.height() + 24)
        shot = app.primaryScreen().grabWindow(0, area.x(), area.y(), area.width(), area.height())
        name = f"sidenotch-real-{'dark' if dark else 'light'}.png"
        shot.save(str(out / name))
        print(f"[PROBE] {name} visible={panel.isVisible()} geo={geo.getRect()}")
        notch.set_enabled(False)
        _pump(app, 0.3)

    # 比べるための 1 枚: 2.4.0 と同じく背景のぼかし（アクリル）を重ねたときの見え方
    from src.platform.windows import acrylic

    notch = SideNotch(history=_FakeHistory(), enabled=True)
    notch.open_panel()
    applied = acrylic.apply_blur(notch._panel, dark=True)
    _pump(app, 1.5)
    geo = notch._panel.frameGeometry()
    shot = app.primaryScreen().grabWindow(0, geo.x(), geo.y(), geo.width(), geo.height())
    shot.save(str(out / "sidenotch-real-with-acrylic.png"))
    print(f"[PROBE] sidenotch-real-with-acrylic.png applied={applied}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
