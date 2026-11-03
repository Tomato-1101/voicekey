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
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

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

# 履歴同期 Worker の共有トークン。Mac の Keychain と同じサービス名
SERVICE_SYNC_TOKEN: str = "voicekey.SyncToken"

# 2026-10 に Mac へ追加したエンジン・翻訳のキー（サービス名は Mac の ApiKeyItem.appService と同一）
SERVICE_SONIOX: str = "voicekey.Soniox"
SERVICE_AZURE_SPEECH: str = "voicekey.AzureSpeech"
SERVICE_AZURE_SPEECH_ENDPOINT: str = "voicekey.AzureSpeechEndpoint"
SERVICE_GEMINI: str = "voicekey.Gemini"

# 共有のキー置き場（Mac の「中央 Keychain」に当たるもの）のアカウント名。
# サービス名は環境変数名（SONIOX_API_KEY など）で、プロバイダーごとにキーを 1 本だけ発行して
# 複数のプロジェクトで使い回すための置き場。voicekey は読むだけで書き込まない。
CENTRAL_ACCOUNT: str = "shared"


@dataclass(frozen=True)
class ApiKeyItem:
    """設定 › API キー で入力できる 1 項目（Mac の ApiKeyItem と同じ並び・同じ名前）。

    Attributes:
        id: 項目の識別子（config.backends.key_item が返す値）
        app_service: アプリ自身が資格情報マネージャーに保存するときのサービス名
        variable_name: 環境変数名（＝共有の置き場のサービス名）
    """

    id: str
    app_service: str
    variable_name: str


# 項目の一覧（表示順）。Deepgram は選択肢から外したが、保存済みの設定で使う人のために残す
API_KEY_ITEMS: Dict[str, ApiKeyItem] = {
    item.id: item
    for item in (
        ApiKeyItem("soniox", SERVICE_SONIOX, "SONIOX_API_KEY"),
        ApiKeyItem("openai", SERVICE_OPENAI, "OPENAI_API_KEY"),
        ApiKeyItem("azure_key", SERVICE_AZURE_SPEECH, "AZURE_SPEECH_KEY"),
        ApiKeyItem("azure_endpoint", SERVICE_AZURE_SPEECH_ENDPOINT, "AZURE_SPEECH_ENDPOINT"),
        ApiKeyItem("elevenlabs", SERVICE_ELEVENLABS, "ELEVENLABS_API_KEY"),
        ApiKeyItem("groq", SERVICE_GROQ, "GROQ_API_KEY"),
        ApiKeyItem("gemini", SERVICE_GEMINI, "GEMINI_API_KEY"),
        ApiKeyItem("deepgram", SERVICE_DEEPGRAM, "DEEPGRAM_API_KEY"),
    )
}

# キーの取得元（設定画面の状態表示に使う。値そのものは画面にもログにも出さない）
SOURCE_APP: str = "app"
SOURCE_ENVIRONMENT: str = "environment"
SOURCE_CENTRAL: str = "central"

SOURCE_LABELS: Dict[str, str] = {
    SOURCE_APP: "アプリに保存済み",
    SOURCE_ENVIRONMENT: "環境変数から読込",
    SOURCE_CENTRAL: "資格情報マネージャー（共有）から読込",
}

# ユーザー名は固定。アプリ単一ユーザー前提のため、エントリ識別はサービス名のみで足りる。
_USERNAME: str = "default"

# Keychain 読み出しは 1 回あたり数十 ms かかり、録音のたびに呼ばれると
# レイテンシに直結するため、プロセス内でキャッシュする。
# None（未登録）もキャッシュし、set/delete 時に無効化する。
_cache: Dict[str, Optional[str]] = {}
_cache_lock = threading.Lock()
# キャッシュの世代。set / delete のたびに進める（_cache_lock で保護）。
# 読み出しはロックの外で keyring を叩くので、その最中に保存・削除が走ると、読み出し側が
# 後から古い値でキャッシュを上書きしてしまう。読み始めの世代と一致するときだけ書き戻す（Mac と同じ）
_cache_generation: int = 0

# device_id の初回生成を直列化する（同時呼び出しで別々の ID を生成し、サーバーの
# 同時利用台数上限に誤って当たるのを防ぐ）。
_device_id_lock = threading.Lock()


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

# ビルド種別のマーカーモジュール。scripts/build/generate_embedded_keys.py が
# 生成する git 管理外モジュールで、開発環境には存在しない（ImportError で None になる）。
# 配布バイナリに長期プロバイダーキーは埋め込まない（製品版は自社サーバー経由・2026-06-28）。
# このモジュールは IS_DIST / IS_PERSONAL フラグだけを持ち、API キーの保管には一切使わない。
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


def is_personal_build() -> bool:
    """
    個人用最速版（personal）ビルドかどうかを返す（Mac の EmbeddedKeys.isPersonal と対）。

    personal は Credential Manager のキーで直叩きし、自社サーバー・ログイン・課金を一切通らない。
    判定は get_auth_session() の 1 点に集約してあり、そこが None を返すことで
    backend_client.is_logged_in() 依存のサーバー経路（プロキシ・短命トークン・warm ループ）が
    まとめて no-op になる。

    Returns:
        マーカーモジュールが存在し IS_PERSONAL が True の場合 True
    """
    return _embedded is not None and getattr(_embedded, "IS_PERSONAL", False)


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
    return _read_cached(service, _USERNAME)


def _read_cached(service: str, username: str) -> Optional[str]:
    """keyring から 1 項目を読む（プロセス内キャッシュ・世代つき）。

    Args:
        service: サービス名
        username: アカウント名（アプリ保存は "default"、共有の置き場は "shared"）

    Returns:
        値。未登録・取得失敗は None
    """
    if _keyring_module is None:
        return None
    cache_key = service if username == _USERNAME else f"{username}:{service}"
    with _cache_lock:
        if cache_key in _cache:
            return _cache[cache_key]
        generation = _cache_generation
    try:
        value = _keyring_module.get_password(service, username) or None
    except Exception as e:
        # NoKeyringError / KeyringError / OS 認証拒否などをまとめて握る。
        # 失敗はキャッシュしない（次回呼び出しで再試行させる）
        logger.warning(f"keyring 読み込みに失敗 ({service}): {e}")
        return None
    with _cache_lock:
        # 読んでいる間に保存・削除が挟まっていたら、古い値で上書きしない
        if generation == _cache_generation:
            _cache[cache_key] = value
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
    # 古い値をキャッシュし続けないよう更新する（世代を進めて、走っている読み出しの書き戻しを捨てる）
    global _cache_generation
    with _cache_lock:
        _cache_generation += 1
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
    try:
        _keyring_module.delete_password(service, _USERNAME)
    except Exception as e:
        # PasswordDeleteError は「そもそも未登録」の場合にも飛ぶので info に留める
        logger.info(f"keyring からの削除をスキップ ({service}): {e}")
    # 削除の後で世代を進めてキャッシュを捨てる。先に捨てると、削除前の項目を読んだ読み出しが
    # 古い値を書き戻し、消したはずのキーが使われ続けるため（Mac と同じ順序）
    global _cache_generation
    with _cache_lock:
        _cache_generation += 1
        _cache[service] = None
    return True


# ============================================
# 設定 › API キー（全エンジン・翻訳）の読み出し
# ============================================

def sanitize_key(raw: str) -> str:
    """貼り付けられたキーを正規化する（Mac の APIKeyStore.sanitize と同じ規則）。

    `SONIOX_API_KEY=...`・`export ...`・引用符付きで貼られても、キーの本体だけを取り出す。

    Args:
        raw: 入力欄の文字列

    Returns:
        正規化したキー（空なら空文字）
    """
    value = (raw or "").strip()
    if value.startswith("export "):
        value = value[len("export "):]
    for item in API_KEY_ITEMS.values():
        prefix = f"{item.variable_name}="
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.strip()
    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            value = value[1:-1]
    return value.strip()


def resolve(
    app: Callable[[], Optional[str]],
    env: Callable[[], Optional[str]],
    central: Callable[[], Optional[str]],
) -> Optional[Tuple[str, str]]:
    """キーの探索順を決める純関数（アプリ保存値 → 環境変数 → 共有の置き場）。

    各段は遅延評価なので、手前で見つかれば後段（keyring の読み出し）は走らない。
    空文字は未設定扱い。Mac の Keychain.resolve と同じ順。

    Returns:
        (値, 取得元) か、どこにも無ければ None
    """
    for source, reader in ((SOURCE_APP, app), (SOURCE_ENVIRONMENT, env), (SOURCE_CENTRAL, central)):
        value = reader()
        if value:
            return value, source
    return None


def lookup(item_id: str) -> Optional[Tuple[str, str]]:
    """項目の値と取得元を探す（アプリ保存値 → 環境変数 → 共有の置き場）。

    Args:
        item_id: API_KEY_ITEMS のキー（"soniox" など）

    Returns:
        (値, 取得元)。未設定・未知の項目は None
    """
    item = API_KEY_ITEMS.get(item_id)
    if item is None:
        return None
    return resolve(
        app=lambda: _read_cached(item.app_service, _USERNAME),
        env=lambda: os.environ.get(item.variable_name),
        central=lambda: _read_cached(item.variable_name, CENTRAL_ACCOUNT),
    )


def central_key(variable_name: str) -> Optional[str]:
    """共有の置き場（サービス名＝環境変数名・アカウント shared）から読む（無ければ None）。"""
    return _read_cached(variable_name, CENTRAL_ACCOUNT)


def api_key(item_id: str) -> Optional[str]:
    """項目のキーを返す（どこにも無ければ None）。文字起こし・整形・翻訳はここから読む。"""
    found = lookup(item_id)
    return found[0] if found else None


def api_key_source(item_id: str) -> Optional[str]:
    """項目がどこから読めるか（設定画面の状態表示用。値は返さない。未設定なら None）。"""
    found = lookup(item_id)
    return found[1] if found else None


def save_item(item_id: str, raw: str) -> bool:
    """項目の値をアプリ自身の保存先（資格情報マネージャーの voicekey.* 項目）へ保存する。

    共有の置き場・環境変数には書かない。値は sanitize_key で正規化してから保存し、
    空になれば保存しない。

    Returns:
        保存できたら True
    """
    item = API_KEY_ITEMS.get(item_id)
    value = sanitize_key(raw)
    if item is None or not value:
        return False
    return set_api_key(item.app_service, value)


def delete_item(item_id: str) -> bool:
    """アプリ自身が保存した値だけを消す（環境変数・共有の置き場には触らない）。"""
    item = API_KEY_ITEMS.get(item_id)
    if item is None:
        return False
    return delete_api_key(item.app_service)


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
        # ロック保持下で「読み直し → 無ければ生成」を直列化する。同時に複数スレッドが
        # 入っても、最初の1本だけが生成・保存し、後続はその値を読み直して共有する。
        with _device_id_lock:
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
        {"access_token", "refresh_token", "expires_at"} の dict。未保存・破損時は None。
        personal ビルドでは常に None
    """
    # personal はアカウント/バックエンドを一切使わない。旧 release（DIST）利用時に残った
    # 認証トークンが Credential Manager にあってもログイン扱いにせず None を返す＝
    # 起動時の利用権確認・warm ループ・短命トークン取得・プロキシ経路を根本から発生させない
    # （backend_client.is_logged_in も本関数依存なので連動して False になる。Mac の
    # Keychain.authSession() と同じ単一点）。実害: 失効済みセッションが残ったまま開発ビルドを
    # 動かすと Groq/Deepgram が応答しないサーバー経由へ送られ、文字起こしが全滅した（2026-09-02）。
    if is_personal_build():
        return None
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
