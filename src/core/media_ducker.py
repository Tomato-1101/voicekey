"""
メディア音量ダッキングモジュール（Windows・録音中に既定出力の音量を一時的に下げる）

録音開始時に既定出力デバイス（スピーカー）のマスター音量を保存して 12% へ下げ、
停止時に元へ戻す。Mac 版 MediaDucker.swift（CoreAudio の VirtualMainVolume）と対で、
Windows は Core Audio の IAudioEndpointVolume（マスター音量）を pycaw 経由で操作する。

Mac と同じ安全策:
- 現在音量が既にターゲット(12%)以下なら何もしない（音量を「引き上げて」しまう逆転を防ぐ）。
- 既にダッキング中なら二重に下げない（元音量を上書きしないため）。
- クラッシュ耐性: 下げる前に「元音量・ダッキング中フラグ」を JSON 状態ファイルへ保存し、
  復元後にクリアする。録音中に落ちても、次回起動時に restore() が残存フラグを見つけて元へ戻す。
- 音量制御を持たない/取得できないデバイス（pycaw 無し・仮想デバイス等）では何もしない
  （失敗は無害にスキップし、録音は止めない）。

CoreAudio/COM 呼び出しはブロッキングになりうるため、録音のクリティカルパスに乗せず
専用のシリアルワーカー（max_workers=1 の Executor）で撃ちっぱなしに行う（Mac の serial queue と対）。

テスト容易性: 実際の COM 呼び出しは `_default_provider()` に隔離し、`_duck_impl` / `_restore_impl`
は音量コントローラ（provider）と状態ファイルパスを差し替え可能にしてある。判定・永続化・
クラッシュ復元のロジックは fake provider で検証する（win32/COM 呼び出し自体はモック境界の外）。
"""

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from ..utils.logger import get_logger

logger = get_logger(__name__)

# ダッキング時に下げる音量（元音量に対する係数ではなく絶対値 0..1）。Mac の duckedVolume と同値。
DUCK_VOLUME = 0.12

STATE_FILE_NAME = "media_duck_state.json"

# CoreAudio/COM を逃がす専用シリアルワーカー（録音の開始/停止を待たせない）。
# 遅延生成（初回 submit 時にスレッドが立つ）＝import だけではスレッドを起こさない。
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """シリアルワーカーを遅延生成して返す。"""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="MediaDucker")
        return _executor


def _default_state_path() -> Path:
    """状態ファイルの既定パス（settings.yaml / history.json と同じ配置ロジック）。"""
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).parent.parent.parent
    return base_dir / STATE_FILE_NAME


# ------------------------------------------------------------------
# 音量コントローラ（COM を隔離する薄いラッパ）
# ------------------------------------------------------------------


class _PycawController:
    """pycaw の IAudioEndpointVolume を get/set の 2 メソッドに包む。"""

    def __init__(self, endpoint) -> None:
        self._endpoint = endpoint

    def get_volume(self) -> Optional[float]:
        try:
            return float(self._endpoint.GetMasterVolumeLevelScalar())
        except Exception:
            return None

    def set_volume(self, value: float) -> bool:
        try:
            self._endpoint.SetMasterVolumeLevelScalar(
                max(0.0, min(1.0, float(value))), None
            )
            return True
        except Exception:
            return False


def _default_provider() -> Optional[_PycawController]:
    """既定出力デバイスのマスター音量コントローラを返す（取得不可なら None）。

    pycaw が無い・COM 初期化失敗・音量制御を持たないデバイスでは None を返し、
    呼び出し側はダッキングを無害にスキップする。
    """
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        endpoint = cast(interface, POINTER(IAudioEndpointVolume))
        return _PycawController(endpoint)
    except Exception as e:
        logger.debug(f"既定出力の音量コントローラを取得できませんでした（ダッキングをスキップ）: {e}")
        return None


# ------------------------------------------------------------------
# 状態ファイル（クラッシュ耐性）
# ------------------------------------------------------------------


def _load_state(path: Path) -> dict:
    """ダッキング状態（active/saved_volume）を読み込む。壊れていれば空扱い。"""
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception as e:
        logger.debug(f"ダッキング状態の読み込みに失敗（無視）: {e}")
    return {}


def _save_state(path: Path, active: bool, saved_volume: float) -> None:
    """ダッキング状態を保存する（一時ファイル経由で置換）。"""
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"active": bool(active), "saved_volume": float(saved_volume)}),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception as e:
        logger.debug(f"ダッキング状態の保存に失敗（無視）: {e}")


def _clear_state(path: Path) -> None:
    """ダッキング状態ファイルを削除する（復元完了後）。"""
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        logger.debug(f"ダッキング状態の削除に失敗（無視）: {e}")


# ------------------------------------------------------------------
# 実処理（テストから直接呼べるよう provider / state_path を注入可能にする）
# ------------------------------------------------------------------

ProviderFn = Callable[[], Optional["_PycawController"]]


def _duck_impl(provider: Optional[ProviderFn] = None, state_path: Optional[Path] = None) -> None:
    """既定出力の音量を保存して下げる（撃ちっぱなし本体）。

    既にダッキング中なら二重に下げない。現在音量がターゲット以下なら何もしない。
    """
    path = state_path or _default_state_path()
    # 二重ダッキング防止（元音量を DUCK_VOLUME で上書きしてしまうのを防ぐ）
    if _load_state(path).get("active"):
        return
    controller = (provider or _default_provider)()
    if controller is None:
        return
    current = controller.get_volume()
    if current is None:
        return
    # 現在音量がターゲットより大きいときだけ下げる。ターゲット以下（既に十分小さい・消音中など）
    # なら下げず、フラグも立てない＝停止時に音量を「上げて」しまわない。
    if current <= DUCK_VOLUME:
        return
    # 復元用に元音量とフラグを先に永続化してから下げる（この間に落ちても復元できる）
    _save_state(path, active=True, saved_volume=current)
    controller.set_volume(DUCK_VOLUME)
    logger.debug(f"メディア音量をダッキング: {current:.3f} → {DUCK_VOLUME}")


def _restore_impl(provider: Optional[ProviderFn] = None, state_path: Optional[Path] = None) -> None:
    """ダッキング中なら元音量へ戻す（撃ちっぱなし本体）。していなければ何もしない。

    起動時の残存フラグ復元（前回異常終了の巻き戻し）にもこの関数を使う。
    """
    path = state_path or _default_state_path()
    state = _load_state(path)
    if not state.get("active"):
        return
    saved = float(state.get("saved_volume", 0.0))
    controller = (provider or _default_provider)()
    if controller is not None:
        controller.set_volume(saved)
        logger.debug(f"メディア音量を復元: {saved:.3f}")
    _clear_state(path)


# ------------------------------------------------------------------
# 公開 API（呼び出し側は即座に戻る）
# ------------------------------------------------------------------


def duck() -> None:
    """録音中に既定出力の音量を下げる（撃ちっぱなし・シリアルワーカーで実行）。"""
    try:
        _get_executor().submit(_duck_impl)
    except Exception as e:
        logger.debug(f"ダッキングの投入に失敗（無視）: {e}")


def restore() -> None:
    """ダッキングを解除して元音量へ戻す（撃ちっぱなし・シリアルワーカーで実行）。

    起動時のクラッシュ復元にも使う（残存フラグがあれば元へ戻す）。
    """
    try:
        _get_executor().submit(_restore_impl)
    except Exception as e:
        logger.debug(f"ダッキング解除の投入に失敗（無視）: {e}")
