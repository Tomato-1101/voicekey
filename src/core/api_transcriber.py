"""
API 文字起こしモジュール（録音後にまとめて送るエンジン）

Groq / OpenAI / ElevenLabs / Microsoft MAI の文字起こし API を httpx で直接呼び出す。
エンジンの名前・鍵の置き場所は engines.py のカタログから引く（ここには書かない）。
録音しながら送るエンジン（Soniox・OpenAI ライブ）は streaming_transcriber.py にある。

設計の要点:
- 公式 SDK は使わず httpx の multipart POST に統一している（SDK の import が起動を遅くするため）。
- 失敗は TranscriptionError 例外で伝える。メッセージは通知にそのまま出せる短い日本語にし、
  API が返したエラー本文や例外の詳細はログにだけ残す（通知に長い英語や内部情報を出さないため）。
- prewarm() で TLS 接続を先に張り、初回リクエストの往復を短くできる。
"""

import json
import threading
import time
from typing import Optional, Tuple
from urllib.parse import urlsplit

import httpx
import numpy as np
import numpy.typing as npt

from . import engines
from .audio_utils import numpy_to_wav_bytes
from .text_utils import strip_cjk_spaces
from ..config.constants import SAMPLE_RATE
from ..utils.logger import get_logger

logger = get_logger(__name__)

# API リクエストのタイムアウト（読み取り 20 秒 / 接続 5 秒）
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

# Whisper 系（Groq）へ送る数字表記の style プロンプト。Whisper は prompt の表記
# スタイルに追従する性質があるため、半角数字を含む短い例文を与えて漢数字化を抑制する
# （Mac 版 Transcriber.numeralStyleHint と文言を一致させること）。
_NUMERAL_STYLE_PROMPT = "数字は半角で表記します。例: 2026年7月15日、3人で1200円、成功率98.5%。"

# Microsoft MAI の文字起こし API のパス（接続先のホストはリソースごとに違う）
_MAI_PATH = "/speechtotext/transcriptions:transcribe"
_MAI_API_VERSION = "2025-10-15"


class TranscriptionError(RuntimeError):
    """文字起こしの失敗を表す例外。メッセージはそのままユーザー通知に使える日本語。"""


class ApiTranscriber:
    """
    文字起こし API の共通クライアント（既定の transcribe は Whisper 形式）。

    サブクラスはクラス属性 engine_id と base_url を決めるだけでよい。表示名・鍵の置き場所・
    既知のモデルは engines.py のカタログから引く。

    スレッドセーフ: クライアント生成はロックで保護し、httpx.Client 自体は
    複数スレッドからのリクエストに対応している。

    Attributes:
        model: 使用するモデル名
        language: 言語コード（'ja' 等、空なら API 側の自動判定）
        prompt: 文字起こしのヒントテキスト
        temperature: サンプリング温度（0.0=決定論的）
        last_api_time: 直近の API 呼び出し時間（ms、ログ用）
        allows_split: 長い録音を無音で分けて並列に送ってよいか
    """

    engine_id: str = ""
    base_url: str = ""
    allows_split: bool = True

    def __init__(
        self,
        model: str,
        language: str = "ja",
        prompt: str = "",
        temperature: float = 0.0,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        """
        ApiTranscriber を初期化する（ネットワークアクセスはしない）。

        Args:
            model: モデル名
            language: 言語コード（'ja', 'en' 等。空文字で自動判定）
            prompt: 文字起こしのヒントテキスト
            temperature: サンプリング温度
            sample_rate: 音声データのサンプリングレート（Hz）
        """
        self.model = model
        self.language = language
        self.prompt = prompt
        self.temperature = temperature
        self.sample_rate = sample_rate
        self.last_api_time: float = 0.0

        self._client: Optional[httpx.Client] = None
        self._client_lock = threading.Lock()

    @property
    def _engine(self) -> engines.Engine:
        """このクライアントが使うエンジンの定義。"""
        return engines.engine(self.engine_id)

    @property
    def display_name(self) -> str:
        """通知・ログの主語に使うエンジン名（例: "Groq"）。"""
        return self._engine.name

    @property
    def keychain_service(self) -> str:
        """API キーを置く資格情報ストアのサービス名。"""
        return self._engine.keyring_service

    @property
    def env_var(self) -> str:
        """API キーの環境変数名。"""
        return self._engine.env_var

    @property
    def available_models(self) -> Tuple[str, ...]:
        """このエンジンで選べるモデル。"""
        return self._engine.models

    def _resolve_api_key(self) -> Optional[str]:
        """API キーを資格情報ストア → 環境変数の順で解決する。"""
        return engines.api_key(self.engine_id)

    def _resolve_base_url(self) -> str:
        """
        API のベース URL を返す（接続先が利用者ごとに違うエンジンはサブクラスで上書きする）。

        Raises:
            TranscriptionError: 接続先が決まらないとき
        """
        return self.base_url

    def _whisper_prompt(self) -> str:
        """Whisper へ送る文字起こしプロンプトを組み立てる。

        数字を半角で出させる固定 style ヒントを先頭に置き、ユーザー設定のプロンプト（あれば）を
        続ける（未設定なら style ヒントのみになる）。
        """
        user = self.prompt.strip()
        return f"{_NUMERAL_STYLE_PROMPT} {user}" if user else _NUMERAL_STYLE_PROMPT

    def _auth_headers(self, api_key: str) -> dict:
        """
        認証ヘッダーを返す（OpenAI 互換は Bearer。サブクラスで上書き可能）。

        Args:
            api_key: 解決済み API キー

        Returns:
            httpx.Client に渡すヘッダー辞書
        """
        return {"Authorization": f"Bearer {api_key}"}

    def _raise_for_status(self, resp: httpx.Response) -> None:
        """
        HTTP ステータスを検査し、エラーなら日本語の TranscriptionError を送出する。

        Raises:
            TranscriptionError: 認証失敗・レート制限・その他 4xx/5xx
        """
        if resp.status_code < 400:
            return
        # エラー本文は原因調査のためログにだけ残す（通知には出さない。長い英語や内部情報が混じるため）
        logger.warning(f"{self.display_name} API エラー (HTTP {resp.status_code}): {resp.text[:300]}")
        if resp.status_code == 401:
            raise TranscriptionError(
                f"{self.display_name} の API キーが無効です（設定 › API キー を確認してください）"
            )
        if resp.status_code == 429:
            raise TranscriptionError(
                f"{self.display_name} API のレート制限に達しました（しばらく待って再試行してください）"
            )
        raise TranscriptionError(f"{self.display_name} API エラー (HTTP {resp.status_code})")

    def _post(self, client: httpx.Client, path: str, **kwargs) -> httpx.Response:
        """
        POST を実行し、通信例外を日本語の TranscriptionError へ変換して応答を返す。

        タイミング計測（last_api_time）とステータス検査もまとめて行う。
        サブクラスの transcribe() から共通利用する。

        Raises:
            TranscriptionError: タイムアウト・接続失敗・HTTP エラー時
        """
        api_start = time.perf_counter()
        try:
            resp = client.post(path, **kwargs)
        except httpx.TimeoutException:
            raise TranscriptionError(
                f"{self.display_name} API がタイムアウトしました（ネットワークを確認してください）"
            )
        except httpx.HTTPError as e:
            logger.warning(f"{self.display_name} API への接続に失敗: {type(e).__name__}: {e}")
            raise TranscriptionError(
                f"{self.display_name} API に接続できませんでした（ネットワークを確認してください）"
            )
        self.last_api_time = (time.perf_counter() - api_start) * 1000
        self._raise_for_status(resp)
        return resp

    def is_available(self) -> bool:
        """API キーが設定されており利用可能かを返す。"""
        return bool(self._resolve_api_key())

    def _get_client(self) -> httpx.Client:
        """
        認証ヘッダー付き httpx クライアントを取得または生成する。

        Returns:
            初期化済みの httpx.Client

        Raises:
            TranscriptionError: API キー（や接続先）が未設定の場合
        """
        with self._client_lock:
            if self._client is None:
                api_key = self._resolve_api_key()
                if not api_key:
                    raise TranscriptionError(
                        f"{self.display_name} の API キーが未設定です"
                        "（設定 › API キー で入力してください）"
                    )
                self._client = httpx.Client(
                    base_url=self._resolve_base_url(),
                    headers=self._auth_headers(api_key),
                    timeout=_TIMEOUT,
                    # keepalive を長めに取り、録音中に prewarm した接続を再利用する
                    limits=httpx.Limits(
                        max_connections=5,
                        max_keepalive_connections=2,
                        keepalive_expiry=60.0,
                    ),
                    # 接続確立時のみの自動リトライ（リクエスト自体は再送しない）
                    transport=httpx.HTTPTransport(retries=2),
                )
            return self._client

    def prewarm(self) -> None:
        """
        API への TLS 接続を事前確立して初回リクエストを高速化する。

        録音開始時にバックグラウンドスレッドから呼ぶ想定。
        失敗しても文字起こし自体には影響しないため、すべて握ってログのみ。
        """
        try:
            client = self._get_client()
            # 応答の中身は使わない（404 でも TLS 接続は張られるので目的は果たせる）
            client.get("/models", timeout=httpx.Timeout(5.0, connect=5.0))
            logger.debug(f"{self.display_name} 接続をプリウォームしました")
        except Exception as e:
            logger.debug(f"{self.display_name} プリウォーム失敗（無視）: {type(e).__name__}")

    def _log_done(self, text: str) -> None:
        """文字起こしの所要時間と文字数をログに残す（本文は出さない）。"""
        logger.info(
            f"{self.display_name} 文字起こし完了: {self.last_api_time:.0f}ms, {len(text)} 文字"
        )

    def _json_text(self, resp: httpx.Response) -> str:
        """
        応答 JSON の text フィールドを取り出す。

        Raises:
            TranscriptionError: 応答が JSON でない・形が違うとき
        """
        try:
            return (resp.json().get("text") or "").strip()
        except Exception as e:
            logger.warning(f"{self.display_name} 応答の解析に失敗: {type(e).__name__}")
            raise TranscriptionError(f"{self.display_name} の応答を読み取れませんでした")

    def transcribe(self, audio_data: npt.NDArray[np.float32]) -> str:
        """
        音声を文字起こしする（Whisper 形式）。

        Args:
            audio_data: 音声データ（float32、モノラルの NumPy 配列）

        Returns:
            文字起こし結果のテキスト（前後空白除去済み）。空音声なら空文字

        Raises:
            TranscriptionError: API キー未設定・通信失敗・API エラー時
        """
        if len(audio_data) == 0:
            return ""

        wav_bytes = numpy_to_wav_bytes(audio_data, self.sample_rate)
        client = self._get_client()

        # OpenAI 互換 multipart フォーム。空のオプションは送らない
        data = {
            "model": self.model,
            "response_format": "text",
            "temperature": str(self.temperature),
            # 数字を半角で出させる style プロンプト（＋ユーザー設定プロンプト）を常に付ける
            "prompt": self._whisper_prompt(),
        }
        if self.language:
            data["language"] = self.language

        resp = self._post(
            client,
            "/audio/transcriptions",
            data=data,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        )

        # 日本語に単語間スペースが入りうるため CJK 隣接スペースを除去する（Mac 版と同じ正規化）
        text = strip_cjk_spaces(resp.text.strip())
        self._log_done(text)
        return text

    def close(self) -> None:
        """
        HTTP コネクションプールを閉じる。

        ホットリロードで設定が変わりインスタンスを破棄する際や、
        アプリ終了時に呼んで接続リークを防ぐ。
        """
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception as e:
                logger.warning(f"{self.display_name} クライアント close に失敗: {e}")


class GroqTranscriber(ApiTranscriber):
    """Groq Cloud の Whisper API（OpenAI 互換エンドポイント）。基底の Whisper 形式をそのまま使う。"""

    engine_id = "groq"
    base_url = "https://api.groq.com/openai/v1"


class OpenAITranscriber(ApiTranscriber):
    """
    OpenAI の gpt-transcribe（REST）。

    Whisper とはリクエストの形が違う:
    - 応答形式は json だけ（text 形式は受け付けない）
    - 言語は languages[] で渡す
    - temperature は送らない
    - prompt は利用者が書いたものだけ送る（Whisper 用の数字 style ヒントは効かないので付けない）
    """

    engine_id = "openai"
    base_url = "https://api.openai.com/v1"

    def transcribe(self, audio_data: npt.NDArray[np.float32]) -> str:
        """音声を OpenAI の gpt-transcribe で文字起こしする。"""
        if len(audio_data) == 0:
            return ""

        wav_bytes = numpy_to_wav_bytes(audio_data, self.sample_rate)
        client = self._get_client()

        data = {"model": self.model, "response_format": "json"}
        if self.language:
            data["languages[]"] = self.language
        user_prompt = self.prompt.strip()
        if user_prompt:
            data["prompt"] = user_prompt

        resp = self._post(
            client,
            "/audio/transcriptions",
            data=data,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        )
        text = strip_cjk_spaces(self._json_text(resp))
        self._log_done(text)
        return text


class ElevenLabsTranscriber(ApiTranscriber):
    """
    ElevenLabs Scribe の Speech-to-Text API（REST）。

    OpenAI 互換ではないため transcribe() と認証ヘッダーを独自実装する:
    - 認証は xi-api-key ヘッダー
    - multipart のフィールド名は model_id（model ではない）/ language_code
    - 応答は JSON で text フィールドに本文が入る
    """

    engine_id = "elevenlabs"
    base_url = "https://api.elevenlabs.io/v1"

    def _auth_headers(self, api_key: str) -> dict:
        """ElevenLabs は Bearer ではなく xi-api-key ヘッダーで認証する。"""
        return {"xi-api-key": api_key}

    def transcribe(self, audio_data: npt.NDArray[np.float32]) -> str:
        """音声を ElevenLabs Scribe で文字起こしする。"""
        if len(audio_data) == 0:
            return ""

        wav_bytes = numpy_to_wav_bytes(audio_data, self.sample_rate)
        client = self._get_client()

        # 「(笑い)」のような音のタグは入力文字に要らないので切る
        data = {"model_id": self.model, "tag_audio_events": "false"}
        if self.language:
            data["language_code"] = self.language

        resp = self._post(
            client,
            "/speech-to-text",
            data=data,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        )
        text = strip_cjk_spaces(self._json_text(resp))
        self._log_done(text)
        return text


def normalize_mai_endpoint(value: str) -> Optional[str]:
    """
    Microsoft の接続先（エンドポイント）を「スキーム + ホスト（+ ポート）」だけに整える。

    Azure ポータルからコピーした値には末尾のスラッシュやパスが付いていたり、https:// が
    抜けていたりするので、ここで吸収する。

    Args:
        value: 利用者が入力した接続先

    Returns:
        整えた URL。空・空白入り・ホストが読み取れない値は None
    """
    text = (value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


class AzureMAITranscriber(ApiTranscriber):
    """
    Microsoft MAI-Transcribe（Azure AI Speech の文字起こし API・REST）。

    他のエンジンと違う点:
    - 接続先のホストがリソースごとに違う（キーとは別に利用者が入力する）
    - 認証は Ocp-Apim-Subscription-Key ヘッダー
    - multipart は audio（音声）と definition（JSON の指定）の 2 つ
    - 応答は combinedPhrases[0].text に全文が入る
    """

    engine_id = "azure_mai"

    def _resolve_endpoint(self) -> Optional[str]:
        """接続先を資格情報ストア → 環境変数の順で読み、整えて返す（未設定・不正なら None）。"""
        eng = self._engine
        raw = engines.read_secret(eng.endpoint_service, eng.endpoint_env_var)
        return normalize_mai_endpoint(raw or "")

    def _resolve_base_url(self) -> str:
        """接続先を返す。未設定なら通知用のメッセージ付きで失敗させる。"""
        endpoint = self._resolve_endpoint()
        if not endpoint:
            raise TranscriptionError(
                "Microsoft の接続先（エンドポイント）が未設定です（設定 › API キー で入力してください）"
            )
        return endpoint

    def _auth_headers(self, api_key: str) -> dict:
        """Azure はサブスクリプションキーのヘッダーで認証する。"""
        return {"Ocp-Apim-Subscription-Key": api_key}

    def is_available(self) -> bool:
        """キーと接続先の両方がそろっているかを返す。"""
        return bool(self._resolve_api_key()) and self._resolve_endpoint() is not None

    def _parse_text(self, resp: httpx.Response) -> str:
        """
        応答から本文を取り出す。

        全文をつないだ combinedPhrases を優先し、無ければ phrases を空白でつなぐ。
        combinedPhrases が空の配列なら「何も聞き取れなかった」なので空文字を返す。

        Raises:
            TranscriptionError: 応答の形が想定と違うとき
        """
        try:
            body = resp.json()
            combined = body.get("combinedPhrases")
            if isinstance(combined, list):
                return str(combined[0].get("text") or "").strip() if combined else ""
            phrases = body["phrases"]
            return " ".join(str(p["text"]) for p in phrases).strip()
        except Exception as e:
            logger.warning(f"{self.display_name} 応答の解析に失敗: {type(e).__name__}")
            raise TranscriptionError(f"{self.display_name} の応答を読み取れませんでした")

    def transcribe(self, audio_data: npt.NDArray[np.float32]) -> str:
        """音声を Microsoft MAI-Transcribe で文字起こしする。"""
        if len(audio_data) == 0:
            return ""

        wav_bytes = numpy_to_wav_bytes(audio_data, self.sample_rate)
        client = self._get_client()

        definition = {"enhancedMode": {"enabled": True, "model": self.model}}
        if self.language:
            definition["locales"] = [self.language]

        resp = self._post(
            client,
            _MAI_PATH,
            params={"api-version": _MAI_API_VERSION},
            files={
                "audio": ("audio.wav", wav_bytes, "audio/wav"),
                # ファイル名を付けない欄として送る（JSON の指定）
                "definition": (None, json.dumps(definition), "application/json"),
            },
        )
        text = strip_cjk_spaces(self._parse_text(resp))
        self._log_done(text)
        return text


# 録音後にまとめて送るエンジンの id → クラス
_REST_CLASSES = {
    cls.engine_id: cls
    for cls in (GroqTranscriber, OpenAITranscriber, ElevenLabsTranscriber, AzureMAITranscriber)
}

# OpenAI ライブがつながらなかったときに録音を送り直す先のモデル
_OPENAI_LIVE_FALLBACK_MODEL = "gpt-transcribe"


def create_transcriber(engine_id: str, model: str, language: str = "ja", prompt: str = ""):
    """
    エンジン id から、録音をまとめて文字起こしするクライアントを作る。

    録音しながら送るエンジンでも、ライブで文字が取れなかったときに録音を送り直す先が要る。
    OpenAI ライブは gpt-transcribe（REST）へ、Soniox は Soniox 自身へ流し直す。

    Args:
        engine_id: エンジン id（engines.normalize_id を通した値）
        model: モデル名
        language: 言語コード
        prompt: 文字起こしのヒントテキスト

    Returns:
        transcribe / prewarm / is_available / close を持つクライアント
    """
    if engine_id == "soniox":
        # 循環 import を避けるためここで読む（streaming_transcriber はこのモジュールを使う）
        from .streaming_transcriber import SonioxTranscriber
        return SonioxTranscriber(model, language, prompt)
    if engine_id == "openai_live":
        return OpenAITranscriber(_OPENAI_LIVE_FALLBACK_MODEL, language, prompt)
    return _REST_CLASSES[engine_id](model, language, prompt)
