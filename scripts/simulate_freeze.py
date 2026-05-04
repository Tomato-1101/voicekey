"""
PortAudio フリーズ再現スクリプト（macOS デバッグ用）

sounddevice.InputStream.stop / close を「指定秒数だけ眠るだけ」に
モンキーパッチしてから voicekey 本体を起動する。これで実機の Bluetooth
切断や CoreAudio の HAL デッドロックを待たずに、確実にフリーズ状況を
再現できる。

期待される観測結果:
1. ホットキーを離すと "[simulate] InputStream.stop() called" が出る
2. 約 2 秒後に audio_recorder のログ
   "ストリームの close が 2.0s 以内に完了しませんでした" が出る
3. それでも文字起こし結果がアクティブウィンドウへ挿入される
4. メニューから Force Reset を押すと録音状態が即時クリアされる

実行例:
    venv/bin/python scripts/simulate_freeze.py            # 既定 30 秒スリープ
    FREEZE_SEC=10 venv/bin/python scripts/simulate_freeze.py
"""

import os
import sys
import time

# プロジェクトルートを import path に追加（scripts/ から実行する想定）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import sounddevice as sd

_FREEZE_SEC = float(os.environ.get("FREEZE_SEC", "30"))

_orig_stop = sd.InputStream.stop
_orig_close = sd.InputStream.close


def _slow_stop(self, *args, **kwargs):
    print(
        f"[simulate] InputStream.stop() called — {_FREEZE_SEC}s スリープして CoreAudio ハングを偽装",
        flush=True,
    )
    time.sleep(_FREEZE_SEC)
    return _orig_stop(self, *args, **kwargs)


def _slow_close(self, *args, **kwargs):
    print(
        f"[simulate] InputStream.close() called — {_FREEZE_SEC}s スリープして CoreAudio ハングを偽装",
        flush=True,
    )
    time.sleep(_FREEZE_SEC)
    return _orig_close(self, *args, **kwargs)


sd.InputStream.stop = _slow_stop
sd.InputStream.close = _slow_close

print(
    f"[simulate] sounddevice.InputStream.stop/close を {_FREEZE_SEC}s スリープに差し替えました",
    flush=True,
)

from src.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
