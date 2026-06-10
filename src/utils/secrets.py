"""
シークレット保管モジュール

API キーを OS 標準のシークレットストア（macOS は Keychain、Windows は Credential
Manager）へ保存・読み出しするための薄いラッパー。`keyring` ライブラリで OS 差を
吸収し、未導入やバックエンド利用不可の場合は警告ログを出して None / False を返す
（呼び出し側で環境変数フォールバックを使う想定）。
"""

import threading
from typing import Dict, Optional

from .logger import get_logger

logger = get_logger(__name__)

# サービス識別子（macOS Keychain や Windows Credential Manager 上のエントリ名）。
# Hotkey1/2 が同じバックエンドを使う場合は同一エントリを共有する。
SERVICE_GROQ: str = "voicekey.Groq"
SERVICE_OPENAI: str = "voicekey.OpenAI"
SERVICE_ELEVENLABS: str = "voicekey.ElevenLabs"

# ユーザー名は固定。アプリ単一ユーザー前提のため、エントリ識別はサービス名のみで足りる。
_USERNAME: str = "default"

# Keychain 読み出しは 1 回あたり数十 ms かかり、録音のたびに呼ばれると
# レイテンシに直結するため、プロセス内でキャッシュする。
# None（未登録）もキャッシュし、set/delete 時に無効化する。
_cache: Dict[str, Optional[str]] = {}
_cache_lock = threading.Lock()


# keyring 遅延インポート。テスト環境やヘッドレス Linux で keyring が無いケースでも
# アプリ起動を阻害しないよう、ImportError は握って機能を縮退させる。
_keyring_module = None  # type: ignore[var-annotated]
_keyring_import_error: Optional[Exception] = None
try:
    import keyring as _keyring_module  # type: ignore
except Exception as e:  # ImportError 以外（DBus 不在等）も含めて握る
    _keyring_import_error = e
    logger.warning(
        f"keyring ライブラリが利用できません ({e})。"
        "API キーは環境変数からのフォールバック読み込みのみ可能です。"
    )


def is_keyring_available() -> bool:
    """
    keyring バックエンドが利用可能かを返す。

    Returns:
        keyring モジュールが import 済みの場合 True
    """
    return _keyring_module is not None


def get_api_key(service: str) -> Optional[str]:
    """
    シークレットストアから API キーを取得する（プロセス内キャッシュ付き）。

    Args:
        service: サービス識別子（SERVICE_GROQ / SERVICE_OPENAI / SERVICE_ELEVENLABS）

    Returns:
        保存済み API キー、未保存または取得失敗時は None
    """
    if _keyring_module is None:
        return None
    with _cache_lock:
        if service in _cache:
            return _cache[service]
    try:
        value = _keyring_module.get_password(service, _USERNAME) or None
    except Exception as e:
        # NoKeyringError / KeyringError / OS 認証拒否などをまとめて握る。
        # 失敗はキャッシュしない（次回呼び出しで再試行させる）
        logger.warning(f"keyring 読み込みに失敗 ({service}): {e}")
        return None
    with _cache_lock:
        _cache[service] = value
    return value


def set_api_key(service: str, key: str) -> bool:
    """
    シークレットストアに API キーを保存する。

    Args:
        service: サービス識別子
        key: 保存する API キー

    Returns:
        保存に成功した場合 True
    """
    if _keyring_module is None:
        logger.warning("keyring が利用できないため API キーを保存できません")
        return False
    try:
        _keyring_module.set_password(service, _USERNAME, key)
    except Exception as e:
        logger.warning(f"keyring 書き込みに失敗 ({service}): {e}")
        return False
    # 古い値をキャッシュし続けないよう更新する
    with _cache_lock:
        _cache[service] = key
    return True


def delete_api_key(service: str) -> bool:
    """
    シークレットストアから API キーを削除する。

    Args:
        service: サービス識別子

    Returns:
        削除成功または既に未登録の場合 True
    """
    if _keyring_module is None:
        return False
    with _cache_lock:
        _cache[service] = None
    try:
        _keyring_module.delete_password(service, _USERNAME)
    except Exception as e:
        # PasswordDeleteError は「そもそも未登録」の場合にも飛ぶので info に留める
        logger.info(f"keyring からの削除をスキップ ({service}): {e}")
        return True
    return True
