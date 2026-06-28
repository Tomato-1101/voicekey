"""
シークレット保管モジュール

API キーを OS 標準のシークレットストア（macOS は Keychain、Windows は Credential
Manager）へ保存・読み出しするための薄いラッパー。`keyring` ライブラリで OS 差を
吸収し、未導入やバックエンド利用不可の場合は警告ログを出して None / False を返す
（呼び出し側で環境変数フォールバックを使う想定）。
"""

import json
import os
import threading
import uuid
from typing import Dict, Optional

from .logger import get_logger

logger = get_logger(__name__)

# サービス識別子（macOS Keychain や Windows Credential Manager 上のエントリ名）。
# Hotkey1/2 が同じバックエンドを使う場合は同一エントリを共有する。
SERVICE_GROQ: str = "voicekey.Groq"
SERVICE_OPENAI: str = "voicekey.OpenAI"
SERVICE_ELEVENLABS: str = "voicekey.ElevenLabs"
SERVICE_DEEPGRAM: str = "voicekey.Deepgram"

# 製品版の認証基盤用エントリ。
# DeviceId は「端末固有の識別子」（認証子ではない）。台数制限・悪用検知のために使う。
# Auth は Supabase の認証セッション（access_token / refresh_token / expires_at）を
# JSON 文字列で 1 エントリに保存する。
SERVICE_DEVICE_ID: str = "voicekey.DeviceId"
SERVICE_AUTH: str = "voicekey.Auth"

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

# 配布（DIST）ビルドのマーカーモジュール。scripts/build/generate_embedded_keys.py が
# 生成する git 管理外モジュールで、開発環境には存在しない（ImportError で None になる）。
# 配布バイナリに長期プロバイダーキーは埋め込まない（製品版は自社サーバー経由・2026-06-28）。
# このモジュールは IS_DIST フラグだけを持ち、API キーの保管には一切使わない。
_embedded = None
try:
    from ..config import embedded_keys as _embedded  # type: ignore
except Exception:
    _embedded = None


def is_dist_build() -> bool:
    """
    配布（DIST）ビルドかどうかを返す。

    DIST マーカーモジュールの有無で判定する。設定画面の API キータブ表示や
    自動アップデートの有効化判定に使う。

    Returns:
        マーカーモジュールが存在し IS_DIST が True の場合 True
    """
    return _embedded is not None and getattr(_embedded, "IS_DIST", False)


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
    # プロバイダーキーは OS のシークレットストア（keyring）からのみ取得する。
    # 配布ビルドにキーは埋め込まないため、未登録なら None（製品版はサーバー経由で動く）。
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


# ============================================
# 製品版バックエンド接続・認証（device_id / Supabase セッション）
# ============================================

def get_server_base_url() -> str:
    """
    自社バックエンド（短命キー発行・プロキシ）のベース URL を返す。

    配布ビルドは本番固定（環境変数 override は無視）。開発ビルドは localhost を
    既定とし、preview 検証などで環境変数 VOICEKEY_SERVER_URL による上書きを許可する。

    Returns:
        末尾スラッシュを除いたベース URL（例 "https://voicekey.vercel.app"）
    """
    # 配布ビルドでは override を一切受け付けない。作業ディレクトリや環境変数を汚染されても
    # Bearer token・音声・フィードバックを任意サーバーへ向けられないようにするため。
    # override は開発ビルドの preview 検証用に限定する。
    override = os.environ.get("VOICEKEY_SERVER_URL")
    if override and not is_dist_build():
        return override.rstrip("/")
    # 循環 import を避けるため遅延 import（constants は secrets に依存しない）
    from ..config.constants import LOCAL_SERVER_URL, PRODUCT_SERVER_URL

    return PRODUCT_SERVER_URL if is_dist_build() else LOCAL_SERVER_URL


def get_device_id() -> str:
    """
    端末固有 ID を取得する（無ければ生成して保存）。

    これは識別子であって認証子ではない（認証は Supabase JWT で行う）。
    サーバー側の同時台数上限・悪用検知のために使う。keyring が使えない環境
    （テスト/ヘッドレス）では永続化できないため、その都度ランダム値を返す。

    Returns:
        端末固有 ID（UUID4 の 32 桁 hex）
    """
    if _keyring_module is not None:
        try:
            existing = _keyring_module.get_password(SERVICE_DEVICE_ID, _USERNAME)
            if existing:
                return existing
        except Exception as e:
            logger.warning(f"device_id 読み込みに失敗: {e}")
        new_id = uuid.uuid4().hex
        try:
            _keyring_module.set_password(SERVICE_DEVICE_ID, _USERNAME, new_id)
        except Exception as e:
            logger.warning(f"device_id 保存に失敗（今回限りの値を使用）: {e}")
        return new_id
    # keyring 不在: 永続化できないので毎回生成（台数制限に当たり得るが致命ではない）
    return uuid.uuid4().hex


def get_auth_session() -> Optional[dict]:
    """
    保存済みの認証セッションを取得する。

    Returns:
        {"access_token", "refresh_token", "expires_at"} の dict。未保存・破損時は None
    """
    if _keyring_module is None:
        return None
    try:
        raw = _keyring_module.get_password(SERVICE_AUTH, _USERNAME)
    except Exception as e:
        logger.warning(f"認証セッション読み込みに失敗: {e}")
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        # 壊れた JSON は未ログイン扱い（再ログインで上書きされる）
        return None


def save_auth_session(access_token: str, refresh_token: str, expires_at: float) -> bool:
    """
    認証セッションを保存する。

    Args:
        access_token: Supabase の access_token（短命）
        refresh_token: 失効後の再発行に使う refresh_token
        expires_at: access_token の失効時刻（UNIX エポック秒）

    Returns:
        保存に成功した場合 True
    """
    if _keyring_module is None:
        logger.warning("keyring が利用できないため認証セッションを保存できません")
        return False
    payload = json.dumps(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        }
    )
    try:
        _keyring_module.set_password(SERVICE_AUTH, _USERNAME, payload)
    except Exception as e:
        logger.warning(f"認証セッション保存に失敗: {e}")
        return False
    return True


def clear_auth_session() -> bool:
    """
    認証セッションを削除する（ログアウト時）。device_id は識別子なので残す。

    Returns:
        削除成功または既に未保存の場合 True
    """
    if _keyring_module is None:
        return False
    try:
        _keyring_module.delete_password(SERVICE_AUTH, _USERNAME)
    except Exception as e:
        logger.info(f"認証セッション削除をスキップ: {e}")
    return True
