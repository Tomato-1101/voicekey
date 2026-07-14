"""
Windows アクリル/ブラー背景ヘルパ（実ブラー背景・Mac の NSVisualEffectView と対）

Mac 版は NSVisualEffectView(.behindWindow) で背後をライブに磨りガラス化する。Windows は
非公開 API `SetWindowCompositionAttribute` の ACCENT_POLICY で背後をブラーする:
- Windows 10 1803+（build 17134）: ACCENT_ENABLE_ACRYLICBLURBEHIND（アクリル・磨りガラス）
- Windows 10 1507+（build 10240）: ACCENT_ENABLE_BLURBEHIND（旧世代ブラー）
- それ未満 / 非 Windows / 失敗時: 何もしない（呼び出し側は QSS の疑似ガラスへフォールバック）

適用対象は矩形サーフェス（サイドノッチの履歴パネル・ホームダッシュボード等）。HUD ピルは
角丸・サイズがモーフするため、矩形全体をブラーするこの方式ではピル外周に矩形フロストが
出てしまう。HUD は現行の描画ベース疑似ガラス（ui/hud.py の paintEvent）を維持する。

テスト容易性: OS ビルド番号取得（`_windows_build`）・実 API 呼び出し（`_default_applier`）を
隔離し、accent state の選択は純粋関数 `_accent_state_for_build` に分けてある。バージョン
ゲートとフォールバック分岐を fake でユニットテストする（SetWindowCompositionAttribute
呼び出し自体はモック境界の外）。
"""

import sys
from typing import Callable, Optional

# ACCENT_STATE（DWM の未公開列挙）
ACCENT_DISABLED = 0
ACCENT_ENABLE_BLURBEHIND = 3
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4

# ブラーが使える最小ビルド
_MIN_BUILD_BLUR = 10240      # Windows 10 1507
_MIN_BUILD_ACRYLIC = 17134   # Windows 10 1803

# 既定のグラデーション色（AABBGGRR の 32bit）。ダーク=黒 60%・ライト=白 70%。
# アクリルは GradientColor のアルファでフロストの濃さが決まる（下地の色味と不透明度）。
DEFAULT_TINT_DARK = 0x99202024   # A=0x99, B=0x20, G=0x20, R=0x24（ダークグレー）
DEFAULT_TINT_LIGHT = 0xB3F4F4F6  # A=0xB3, B=0xF4, G=0xF4, R=0xF6（ライトグレー）


def _windows_build() -> Optional[int]:
    """実行中 Windows のビルド番号（非 Windows なら None）。"""
    if not sys.platform.startswith("win"):
        return None
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return None


def _accent_state_for_build(build: Optional[int]) -> Optional[int]:
    """OS ビルドに応じて使える ACCENT_STATE を返す（使えないなら None）。"""
    if build is None or build < _MIN_BUILD_BLUR:
        return None
    if build >= _MIN_BUILD_ACRYLIC:
        return ACCENT_ENABLE_ACRYLICBLURBEHIND
    return ACCENT_ENABLE_BLURBEHIND


def blur_supported() -> bool:
    """このプラットフォームでアクリル/ブラーが使えるか（Windows 10+）。"""
    return _accent_state_for_build(_windows_build()) is not None


def _default_applier(hwnd: int, accent_state: int, gradient_argb: int) -> None:
    """SetWindowCompositionAttribute で ACCENT_POLICY を適用する（Windows 実機専用）。"""
    import ctypes
    from ctypes import wintypes

    class ACCENT_POLICY(ctypes.Structure):
        _fields_ = [
            ("AccentState", ctypes.c_int),
            ("AccentFlags", ctypes.c_int),
            ("GradientColor", ctypes.c_uint),
            ("AnimationId", ctypes.c_int),
        ]

    class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
        _fields_ = [
            ("Attribute", ctypes.c_int),
            ("Data", ctypes.POINTER(ACCENT_POLICY)),
            ("SizeOfData", ctypes.c_size_t),
        ]

    # WCA_ACCENT_POLICY = 19。AccentFlags=2 は四辺にブラーを敷く指定。
    WCA_ACCENT_POLICY = 19
    accent = ACCENT_POLICY(accent_state, 2, gradient_argb, 0)
    data = WINDOWCOMPOSITIONATTRIBDATA(
        WCA_ACCENT_POLICY, ctypes.pointer(accent), ctypes.sizeof(accent)
    )
    set_wca = ctypes.windll.user32.SetWindowCompositionAttribute
    set_wca.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWCOMPOSITIONATTRIBDATA)]
    set_wca(int(hwnd), ctypes.byref(data))


def apply_blur(
    widget,
    dark: bool = True,
    gradient_argb: Optional[int] = None,
    applier: Optional[Callable[[int, int, int], None]] = None,
) -> str:
    """widget（QWidget）にアクリル/ブラー背景を適用する。

    Args:
        widget: 対象ウィジェット（`winId()` で HWND を取り出す）。
        dark: ダークテーマか（既定のフロスト色の選択に使う）。
        gradient_argb: フロスト色（AABBGGRR）。None ならテーマ既定。
        applier: 実 API 呼び出しの差し替え（テスト用）。

    Returns:
        "acrylic" / "blur" / "none"。"none" は未対応・失敗＝呼び出し側は QSS 疑似ガラスへ。
    """
    state = _accent_state_for_build(_windows_build())
    if state is None:
        return "none"
    tint = gradient_argb if gradient_argb is not None else (
        DEFAULT_TINT_DARK if dark else DEFAULT_TINT_LIGHT
    )
    try:
        hwnd = int(widget.winId())
        (applier or _default_applier)(hwnd, state, tint)
    except Exception:
        # 適用に失敗したら疑似ガラスへフォールバックさせる
        return "none"
    return "acrylic" if state == ACCENT_ENABLE_ACRYLICBLURBEHIND else "blur"


def clear_blur(widget, applier: Optional[Callable[[int, int, int], None]] = None) -> None:
    """widget のアクリル/ブラーを解除する（ACCENT_DISABLED）。"""
    if _windows_build() is None:
        return
    try:
        hwnd = int(widget.winId())
        (applier or _default_applier)(hwnd, ACCENT_DISABLED, 0)
    except Exception:
        pass
