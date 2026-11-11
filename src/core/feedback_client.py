"""
フィードバック送信モジュール

トレイメニューの「フィードバックを送る…」で入力された本文を、voicekey のサイト
（/api/v1/feedback）へ匿名で送る。ログイン・アカウントは使わない（製品版の運用終了に
伴い認証層は撤去済み）。送るのは本文・アプリのバージョン・端末識別子・OS 種別だけで、
API キーや音声・文字起こし結果は一切含めない。
"""

import httpx

from ..config import constants
from ..utils import secrets
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 接続は短め・読み取りも短め（本文 1 件の POST なので長く待つ意味がない）。
# 有限のタイムアウトは feedback_dialog.closeEvent の wait() が必ず返る前提でもある。
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class FeedbackError(Exception):
    """フィードバック送信の失敗（メッセージはそのままダイアログに出せる日本語）。"""


def submit_feedback(message: str) -> None:
    """
    フィードバック本文を送信する（匿名・ブロッキング）。

    通信を伴うため UI スレッドから直接呼ばず、ワーカースレッドから呼ぶこと。

    Args:
        message: フィードバック本文

    Raises:
        FeedbackError: 通信失敗・非 200（呼び出し側でユーザーに表示する）
    """
    headers = {
        "Content-Type": "application/json",
        # 同じ端末からの重複・連投をサーバー側で見分けるための識別子（認証子ではない）
        "x-device-id": secrets.get_device_id(),
        "x-platform": "windows",
    }
    try:
        resp = httpx.post(
            constants.FEEDBACK_URL,
            headers=headers,
            json={"message": message, "app_version": constants.APP_VERSION},
            timeout=_TIMEOUT,
        )
    except httpx.TimeoutException:
        raise FeedbackError("サーバーへの接続がタイムアウトしました")
    except httpx.HTTPError as e:
        raise FeedbackError(f"サーバーへの接続に失敗しました: {e}")
    if resp.status_code == 429:
        raise FeedbackError("リクエストが多すぎます。少し待ってからお試しください")
    if resp.status_code != 200:
        raise FeedbackError(f"サーバーエラー (HTTP {resp.status_code})")
