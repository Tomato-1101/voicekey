"""
文字起こしエンジンのカタログ

選べるエンジン・モデル・表示名・鍵の置き場所を 1 か所にまとめる。設定画面・設定の移行・
文字起こし本体（api_transcriber / streaming_transcriber）・アプリ本体はすべてここを読む
（同じ一覧を複数のファイルに書くと、エンジンを足すたびにどこかが食い違うため）。

エンジン id は Mac 版 `Backend` の rawValue と同じ文字列にしてある。履歴同期や設定の読み替えで
両 OS の値をそのまま突き合わせられるようにするため。Apple のオンデバイス認識は Mac 専用なので
ここには無い。Deepgram は選択肢から外した（保存済みの値は Soniox へ読み替える）。
"""

import os
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from ..utils import secrets


@dataclass(frozen=True)
class Engine:
    """
    文字起こしエンジン 1 つ分の定義。

    Attributes:
        id: 設定ファイルに保存する識別子（Mac 版 Backend の rawValue と同じ）
        provider_name: 提供元の名前（API キー欄の見出しに使う）
        name: エンジンの短い名前（通知・ログの主語に使う）
        label: 設定画面の選択肢に出す表示名（Mac の設定画面と同じ「提供元 + 既定モデル」）
        models: 選べるモデル（先頭が既定＝推奨）
        streaming: 録音しながら送るエンジンか（False は録音後にまとめて送る）
        default_format_enabled: 「文章を自動で整える」の既定
        keyring_service: API キーを置く資格情報ストアのサービス名
        env_var: API キーの環境変数名（資格情報ストアに無いときに読む）
        caption: 設定画面で選択肢の下に出す説明文
        endpoint_service: 接続先を置くサービス名（Microsoft だけが使う。他は空）
        endpoint_env_var: 接続先の環境変数名（Microsoft だけが使う。他は空）
    """

    id: str
    provider_name: str
    name: str
    label: str
    models: Tuple[str, ...]
    streaming: bool
    default_format_enabled: bool
    keyring_service: str
    env_var: str
    caption: str
    endpoint_service: str = ""
    endpoint_env_var: str = ""

    @property
    def default_model(self) -> str:
        """既定（推奨）のモデル。models の先頭。"""
        return self.models[0]


# 並び順は設定画面の表示順（速いおすすめを先頭に置く）。
# 説明文は Mac 版 SlotSettingsTab.backendCaption と同じ文言にしてある。
ENGINES: Tuple[Engine, ...] = (
    Engine(
        id="soniox",
        provider_name="Soniox",
        name="Soniox",
        label="Soniox stt-rt-v5",
        models=("stt-rt-v5",),
        streaming=True,
        # しゃべり終わった瞬間に入れる速さが売りなので、整形の往復は既定では足さない
        default_format_enabled=False,
        keyring_service=secrets.SERVICE_SONIOX,
        env_var="SONIOX_API_KEY",
        caption=(
            "しゃべりながら文字起こしし、離した瞬間に入力します（速さ重視のおすすめ）。\n"
            "日本語・英語・中国語の混ざった話にも強いエンジンです（鍵: SONIOX_API_KEY）。"
        ),
    ),
    Engine(
        id="elevenlabs",
        provider_name="ElevenLabs",
        name="ElevenLabs",
        label="ElevenLabs scribe_v2",
        models=("scribe_v2", "scribe_v1"),
        streaming=False,
        default_format_enabled=True,
        keyring_service=secrets.SERVICE_ELEVENLABS,
        env_var="ELEVENLABS_API_KEY",
        caption=(
            "録音後に ElevenLabs の Scribe v2 で文字起こしします（日本語の精度重視）。\n"
            "数字が漢数字で出やすいので、気になるときは「数字入力」の設定と併用してください。"
        ),
    ),
    Engine(
        id="openai",
        provider_name="OpenAI",
        name="OpenAI",
        label="OpenAI gpt-transcribe",
        models=("gpt-transcribe",),
        streaming=False,
        default_format_enabled=True,
        keyring_service=secrets.SERVICE_OPENAI,
        env_var="OPENAI_API_KEY",
        caption="録音後に OpenAI の gpt-transcribe で文字起こしします（日本語の精度重視）。",
    ),
    Engine(
        id="azure_mai",
        provider_name="Microsoft",
        name="Microsoft MAI",
        label="Microsoft MAI-Transcribe-2",
        models=("MAI-Transcribe-2",),
        streaming=False,
        default_format_enabled=True,
        keyring_service=secrets.SERVICE_AZURE_SPEECH,
        env_var="AZURE_SPEECH_KEY",
        caption=(
            "録音後に Microsoft の MAI-Transcribe-2 で文字起こしします（精度が高く単価も安め・プレビュー）。\n"
            "鍵: AZURE_SPEECH_KEY と AZURE_SPEECH_ENDPOINT。"
            "リソースは対応リージョンに作ってください（japaneast は非対応）。"
        ),
        endpoint_service=secrets.SERVICE_AZURE_SPEECH_ENDPOINT,
        endpoint_env_var="AZURE_SPEECH_ENDPOINT",
    ),
    Engine(
        id="openai_live",
        provider_name="OpenAI",
        name="OpenAI ライブ",
        label="OpenAI gpt-live-transcribe",
        models=("gpt-live-transcribe",),
        streaming=True,
        default_format_enabled=False,
        keyring_service=secrets.SERVICE_OPENAI,
        env_var="OPENAI_API_KEY",
        caption=(
            "OpenAI の新しいライブ文字起こしで入力します。\n"
            "確定はやや遅めですが（実測 0.7 秒）、固有名詞や数字に強いエンジンです。"
        ),
    ),
    Engine(
        id="groq",
        provider_name="Groq",
        name="Groq",
        label="Groq whisper-large-v3-turbo",
        models=("whisper-large-v3-turbo",),
        streaming=False,
        default_format_enabled=True,
        keyring_service=secrets.SERVICE_GROQ,
        env_var="GROQ_API_KEY",
        caption=(
            "録音後にきれいな文章にして入力します（録音後に送る方式で最速）\n"
            "ハンズフリー録音のときは、長い録音に強いエンジンへ自動で切り替わります。"
        ),
    ),
)

# 設定画面で選べるエンジン id（表示順）
SELECTABLE_IDS: Tuple[str, ...] = tuple(e.id for e in ENGINES)

# 知らない値が保存されていたときの移行先（Mac 版と同じく Groq）
FALLBACK_ID: str = "groq"

# 選択肢から外したエンジン → 読み替え先
_RENAMED_IDS = {"deepgram": "soniox"}

# 提供が終わった・選択肢から外したモデル名。保存されていたら、そのエンジンの既定へ読み替える。
# ここに無い名前（利用者が手で書いた新しいモデル等）は触らない。
RETIRED_MODELS = frozenset({
    "nova-3",
    "nova-2",
    "gpt-realtime-whisper",
    "whisper-large-v3",
    "gpt-4o-mini-transcribe",
    "gpt-4o-transcribe",
    "scribe_v1_experimental",
})

_BY_ID = {e.id: e for e in ENGINES}


def engine(engine_id: str) -> Engine:
    """
    id からエンジン定義を返す。

    Args:
        engine_id: エンジン id（normalize_id を通した値を渡す）

    Returns:
        エンジン定義

    Raises:
        KeyError: 一覧に無い id のとき
    """
    return _BY_ID[engine_id]


def normalize_id(value: Any) -> str:
    """
    保存されていたエンジンの値を、いま選べる id に読み替える。

    Deepgram は Soniox へ（どちらも「話しながら送る速いエンジン」なので使い心地が近い）、
    それ以外の知らない値は Groq へ寄せる。何度通しても結果は変わらない。

    Args:
        value: 設定ファイルに保存されていた値

    Returns:
        一覧にあるエンジン id
    """
    text = str(value).lower()
    text = _RENAMED_IDS.get(text, text)
    return text if text in _BY_ID else FALLBACK_ID


def normalize_model(engine_id: str, model: Any) -> str:
    """
    保存されていたモデル名を、そのエンジンで使える名前に読み替える。

    空・提供の終わった名前・別のエンジンのモデル名（エンジンを読み替えた後に残った値）は既定へ寄せる。
    一覧に無いだけの名前は残す（新しいモデルを手で書いて試せるようにするため）。

    Args:
        engine_id: エンジン id
        model: 保存されていたモデル名

    Returns:
        使うモデル名
    """
    eng = engine(engine_id)
    text = str(model or "").strip()
    if not text or text in RETIRED_MODELS:
        return eng.default_model
    if text not in eng.models and any(text in other.models for other in ENGINES):
        return eng.default_model
    return text


def read_secret(service: str, env_var: str) -> Optional[str]:
    """
    資格情報ストア → 環境変数の順に値を読む。

    Args:
        service: 資格情報ストアのサービス名
        env_var: 環境変数名

    Returns:
        見つかった値。どちらにも無ければ None
    """
    return secrets.get_api_key(service) or os.environ.get(env_var) or None


def api_key(engine_id: str) -> Optional[str]:
    """エンジンの API キーを返す（未設定なら None）。"""
    eng = engine(engine_id)
    return read_secret(eng.keyring_service, eng.env_var)


def has_any_api_key() -> bool:
    """どれか 1 つでも文字起こしに使える API キーが入っているかを返す。"""
    return any(api_key(e.id) for e in ENGINES)
