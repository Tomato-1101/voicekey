"""
API 文字起こし統合モジュール

OpenAI / Groq の Audio Transcriptions API（OpenAI 互換 REST）を
httpx で直接呼び出す共通基盤と、各バックエンドのサブクラスを提供する。

旧実装からの主な変更点:
- openai / groq の公式 SDK を廃止し、httpx の multipart POST に統一した。
  両 API は同一のリクエスト形式（OpenAI 互換）であり、SDK のトップレベル
  import（数百 ms）が起動遅延の一因だったため。
- インスタンスごとに抱えていた VAD（VadFilter）を撤去した。
  VAD はアプリ層で共有する SileroVad（core/vad.py）が文字起こし前に行う。
- 失敗は "Error:" 文字列ではなく TranscriptionError 例外で伝える。
- prewarm() で TLS 接続を事前確立し、初回リクエストの往復を短縮できる。
"""

import threading
import time
from typing import Optional, Tuple

import httpx
import numpy as np
import numpy.typing as npt

from . import backend_client
from .audio_utils import numpy_to_wav_bytes
from .text_utils import strip_cjk_spaces
from ..config.constants import SAMPLE_RATE
from ..utils import secrets
from ..utils.logger import get_logger

logger = get_logger(__name__)

# API リクエストのタイムアウト（読み取り 20 秒 / 接続 5 秒）
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class TranscriptionError(RuntimeError):
    """文字起こしの失敗を表す例外。メッセージはそのままユーザー通知に使える日本語。"""


class ApiTranscriber:
    """
    OpenAI 互換 Audio Transcriptions API の共通クライアント。

    サブクラスはクラス属性で接続先を定義するだけでよい:
        display_name: ログ・エラーメッセージ用のサービス名
        base_url: API のベース URL（例: "https://api.openai.com/v1"）
        keychain_service: secrets モジュールのサービス識別子
        env_var: API キーのフォールバック環境変数名
        available_models: 既知のモデル名タプル（未知でも警告のみで続行）

    スレッドセーフ: クライアント生成はロックで保護し、httpx.Client 自体は
    複数スレッドからのリクエストに対応している。

    Attributes:
        model: 使用するモデル名
        language: 言語コード（'ja' 等、空なら API 側の自動判定）
        prompt: 文字起こしのヒントテキスト
        temperature: サンプリング温度（0.0=決定論的）
        last_api_time: 直近の API 呼び出し時間（ms、ログ用）
    """

    display_name: str = "API"
    base_url: str = ""
    keychain_service: str = ""
    env_var: str = ""
    available_models: Tuple[str, ...] = ()

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

        if self.available_models and model not in self.available_models:
            logger.warning(
                f"{self.display_name}: モデル '{model}' は未知です。"
                f"既知のモデル: {', '.join(self.available_models)}"
            )

    def _resolve_api_key(self) -> Optional[str]:
        """API キーを Keychain（キャッシュ付き）→ 環境変数の優先順で解決する。"""
        import os
        return secrets.get_api_key(self.keychain_service) or os.environ.get(self.env_var)

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
        if resp.status_code == 401:
            raise TranscriptionError(f"{self.display_name} の API キーが無効です（設定を確認してください）")
        if resp.status_code == 429:
            raise TranscriptionError(f"{self.display_name} API のレート制限に達しました（しばらく待って再試行してください）")
        if resp.status_code >= 400:
            # エラー詳細は JSON のことが多いが、先頭だけ載せれば原因特定には足りる
            raise TranscriptionError(
                f"{self.display_name} API エラー (HTTP {resp.status_code}): {resp.text[:200]}"
            )

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
            raise TranscriptionError(f"{self.display_name} API への接続に失敗しました: {e}")
        self.last_api_time = (time.perf_counter() - api_start) * 1000
        self._raise_for_status(resp)
        return resp

    def is_available(self) -> bool:
        """API キーが設定されており利用可能かを返す。"""
        return bool(self._resolve_api_key())

    def _dist_guard(self, server_supported: bool) -> None:
        """配布（DIST）ビルドの無料ゲート: ログイン＋アクティベーション必須にする。

        配布版はアプリに長期キーを埋め込まず、ログイン（＝アカウント）＋有効な
        アクティベーションキーが無いと文字起こしできない。サブスク/キーの有効性
        判定はサーバー側（ephemeral/proxy が 403 を返す）で行うため、ここでは
        「ログイン済みか」と「この方式がサーバー経由に対応しているか」だけを見る。

        開発（dev）ビルドでは何もしない（埋め込み/設定キー直叩きの並存を維持）。

        Args:
            server_supported: この文字起こし方式がサーバー経由に対応しているか
                              （Deepgram=短命JWT / ElevenLabs=プロキシ は True、
                               OpenAI/Groq の文字起こしは配布版で非対応＝False）

        Raises:
            TranscriptionError: 配布版で未ログイン、または非対応方式を選んだ場合
        """
        if not secrets.is_dist_build():
            return
        if not backend_client.is_logged_in():
            raise TranscriptionError(
                "利用するにはログインとアクティベーションキーが必要です（設定 → アカウント）"
            )
        if not server_supported:
            raise TranscriptionError("この文字起こし方式は配布版では利用できません")

    def _get_client(self) -> httpx.Client:
        """
        認証ヘッダー付き httpx クライアントを取得または生成する。

        Returns:
            初期化済みの httpx.Client

        Raises:
            TranscriptionError: API キーが未設定の場合
        """
        with self._client_lock:
            if self._client is None:
                api_key = self._resolve_api_key()
                if not api_key:
                    raise TranscriptionError(
                        f"{self.display_name} の API キーが未設定です"
                        f"（設定ウィンドウまたは {self.env_var} 環境変数で指定してください）"
                    )
                self._client = httpx.Client(
                    base_url=self.base_url,
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
            client.get("/models", timeout=httpx.Timeout(5.0, connect=5.0))
            logger.debug(f"{self.display_name} 接続をプリウォームしました")
        except Exception as e:
            logger.debug(f"{self.display_name} プリウォーム失敗（無視）: {e}")

    def transcribe(self, audio_data: npt.NDArray[np.float32]) -> str:
        """
        音声を文字起こしする。

        Args:
            audio_data: 音声データ（float32、モノラルの NumPy 配列）

        Returns:
            文字起こし結果のテキスト（前後空白除去済み）。空音声なら空文字

        Raises:
            TranscriptionError: API キー未設定・通信失敗・API エラー時
        """
        if len(audio_data) == 0:
            return ""

        # 配布版ゲート: OpenAI/Groq の文字起こしはサーバー非対応＝配布版では使えない
        self._dist_guard(server_supported=False)

        wav_bytes = numpy_to_wav_bytes(audio_data, self.sample_rate)
        client = self._get_client()

        # OpenAI 互換 multipart フォーム。空のオプションは送らない
        data = {
            "model": self.model,
            "response_format": "text",
            "temperature": str(self.temperature),
        }
        if self.language:
            data["language"] = self.language
        if self.prompt:
            data["prompt"] = self.prompt

        resp = self._post(
            client,
            "/audio/transcriptions",
            data=data,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        )

        # streaming/REST・Mac/Windows で同じ結果にするため共通正規化を通す（#21）。
        # OpenAI/Groq も日本語に単語間スペースが入りうるため CJK 隣接スペースを除去する。
        text = strip_cjk_spaces(resp.text.strip())
        logger.info(
            f"{self.display_name} 文字起こし完了: {self.last_api_time:.0f}ms, {len(text)} 文字"
        )
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


class OpenAITranscriber(ApiTranscriber):
    """OpenAI Audio Transcriptions API（gpt-4o-transcribe 系）。"""

    display_name = "高精度"  # ユーザー向け表示は特徴名（提供元名は伏せる）
    base_url = "https://api.openai.com/v1"
    keychain_service = secrets.SERVICE_OPENAI
    env_var = "OPENAI_API_KEY"
    available_models = (
        "gpt-4o-transcribe",       # 高精度
        "gpt-4o-mini-transcribe",  # コスト効率重視
    )


class GroqTranscriber(ApiTranscriber):
    """Groq Cloud の Whisper API（OpenAI 互換エンドポイント）。"""

    display_name = "高速"  # ユーザー向け表示は特徴名（提供元名は伏せる）
    base_url = "https://api.groq.com/openai/v1"
    keychain_service = secrets.SERVICE_GROQ
    env_var = "GROQ_API_KEY"
    available_models = (
        "whisper-large-v3-turbo",  # 最速（リアルタイムの 200 倍超）
        "whisper-large-v3",        # 高精度
    )


class ElevenLabsTranscriber(ApiTranscriber):
    """
    ElevenLabs Scribe の Speech-to-Text API（REST）。

    OpenAI 互換ではないため transcribe() と認証ヘッダーを独自実装する:
    - 認証は xi-api-key ヘッダー
    - multipart のフィールド名は model_id（model ではない）/ language_code
    - 応答は JSON で text フィールドに本文が入る

    ベンチ（2026-06-10）では短文の日本語精度が最高（scribe_v1 で CER 0.0%）。
    ただし scribe_v2 は短文0%でも長文で精度後退するため既定は scribe_v1。
    """

    display_name = "正確性"  # 製品版(release)の文字起こし 2 択名（提供元名は伏せる）
    base_url = "https://api.elevenlabs.io/v1"
    keychain_service = secrets.SERVICE_ELEVENLABS
    env_var = "ELEVENLABS_API_KEY"
    available_models = (
        "scribe_v1",               # 日本語短文・長文ともに高精度（既定）
        "scribe_v1_experimental",  # 実験版
        "scribe_v2",               # 最新。短文は0%だが日本語長文は後退する
    )

    def _auth_headers(self, api_key: str) -> dict:
        """ElevenLabs は Bearer ではなく xi-api-key ヘッダーで認証する。"""
        return {"xi-api-key": api_key}

    def transcribe(self, audio_data: npt.NDArray[np.float32]) -> str:
        """音声を ElevenLabs Scribe で文字起こしする。"""
        if len(audio_data) == 0:
            return ""

        # 配布版ゲート: ログイン＋アクティベーション必須（ElevenLabs はプロキシ対応）
        self._dist_guard(server_supported=True)

        wav_bytes = numpy_to_wav_bytes(audio_data, self.sample_rate)

        # 製品版（ログイン済み）はサーバープロキシ経由で文字起こしする。
        # ElevenLabs バッチは短命キー非対応のためプロキシを使う（並存ガード）。
        if backend_client.is_logged_in():
            try:
                # プロキシ経由でも直叩きと同じ正規化を通す（#21・CJK 隣接スペース除去）
                return strip_cjk_spaces(
                    backend_client.transcribe_elevenlabs(wav_bytes, self.language or "")
                )
            except backend_client.BackendError as e:
                raise TranscriptionError(str(e))

        client = self._get_client()

        # model ではなく model_id。言語は language_code（空なら自動判定）
        data = {"model_id": self.model}
        if self.language:
            data["language_code"] = self.language

        resp = self._post(
            client,
            "/speech-to-text",
            data=data,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        )

        try:
            text = strip_cjk_spaces((resp.json().get("text") or "").strip())
        except Exception as e:
            raise TranscriptionError(f"{self.display_name} 応答の解析に失敗しました: {e}")

        logger.info(
            f"{self.display_name} 文字起こし完了: {self.last_api_time:.0f}ms, {len(text)} 文字"
        )
        return text


class DeepgramTranscriber(ApiTranscriber):
    """
    Deepgram Listen の事前録音 API（REST）。

    OpenAI 互換ではないため transcribe() と認証を独自実装する:
    - 認証は Authorization: Token <key>
    - 音声は multipart ではなく raw バイト本文として送る（Content-Type: audio/wav）
    - モデル・言語・整形オプションはクエリパラメータで指定
    - 応答 JSON の results.channels[0].alternatives[0].transcript が本文

    日本語出力に単語間スペースが入るため strip_cjk_spaces で除去する。
    nova-3 はストリーミング既定でもあり、REST フォールバックにも使う。
    """

    display_name = "高速リアルタイム"  # 製品版(release)の文字起こし 2 択名（提供元名は伏せる）
    base_url = "https://api.deepgram.com/v1"
    keychain_service = secrets.SERVICE_DEEPGRAM
    env_var = "DEEPGRAM_API_KEY"
    available_models = (
        "nova-3",  # 多言語・最新（既定）
        "nova-2",  # 旧世代
    )

    def _auth_headers(self, api_key: str) -> dict:
        """Deepgram は Bearer ではなく Token スキームで認証する。"""
        return {"Authorization": f"Token {api_key}"}

    @property
    def _dg_language(self) -> str:
        """
        Deepgram の言語パラメータ。

        nova-3 は日本語の単言語指定に未対応のため多言語モード（multi）を使う。
        nova-2 以前は ja 等の指定をそのまま使う。
        """
        if self.model.startswith("nova-3"):
            return "multi"
        return self.language or "ja"

    def _parse_transcript(self, resp: httpx.Response) -> str:
        """Deepgram の応答 JSON から本文を取り出し CJK スペースを除去する。"""
        try:
            alternatives = resp.json()["results"]["channels"][0]["alternatives"]
            transcript = alternatives[0]["transcript"] if alternatives else ""
        except Exception as e:
            raise TranscriptionError(f"{self.display_name} 応答の解析に失敗しました: {e}")
        # 日本語の単語間スペースを除去（英単語間は残す）
        return strip_cjk_spaces(transcript.strip())

    def _transcribe_via_jwt(self, wav_bytes: bytes) -> str:
        """製品版（ログイン済み）: 短命 JWT を取得し Deepgram を直叩きする。

        低レイテンシ核心を保つため、ElevenLabs と違いプロキシではなく直叩き。
        キャッシュ済みクライアント（固定 Token ヘッダー）はバイパスし、
        リクエストごとに Bearer JWT を載せた一発の POST を投げる。
        """
        try:
            grant = backend_client.fetch_ephemeral_token()
        except backend_client.BackendError as e:
            raise TranscriptionError(str(e))

        params = {
            "model": self.model,
            "language": self._dg_language,
            "punctuate": "true",
            "smart_format": "true",
        }
        api_start = time.perf_counter()
        try:
            resp = httpx.post(
                f"{self.base_url}/listen",
                params=params,
                content=wav_bytes,
                headers={
                    "Authorization": f"Bearer {grant['token']}",
                    "Content-Type": "audio/wav",
                },
                timeout=_TIMEOUT,
            )
        except httpx.TimeoutException:
            raise TranscriptionError(
                f"{self.display_name} API がタイムアウトしました（ネットワークを確認してください）"
            )
        except httpx.HTTPError as e:
            raise TranscriptionError(f"{self.display_name} API への接続に失敗しました: {e}")
        self.last_api_time = (time.perf_counter() - api_start) * 1000
        self._raise_for_status(resp)

        text = self._parse_transcript(resp)
        logger.info(
            f"{self.display_name} 文字起こし完了: {self.last_api_time:.0f}ms, {len(text)} 文字"
        )
        return text

    def transcribe(self, audio_data: npt.NDArray[np.float32]) -> str:
        """音声を Deepgram Listen（事前録音）で文字起こしする。"""
        if len(audio_data) == 0:
            return ""

        # 配布版ゲート: ログイン＋アクティベーション必須（Deepgram は短命JWT対応）
        self._dist_guard(server_supported=True)

        wav_bytes = numpy_to_wav_bytes(audio_data, self.sample_rate)

        # 製品版（ログイン済み）は短命 JWT で直叩きする（並存ガード）。
        if backend_client.is_logged_in():
            return self._transcribe_via_jwt(wav_bytes)

        client = self._get_client()

        params = {
            "model": self.model,
            "language": self._dg_language,
            "punctuate": "true",
            "smart_format": "true",
        }
        resp = self._post(
            client,
            "/listen",
            params=params,
            content=wav_bytes,
            headers={"Content-Type": "audio/wav"},
        )

        text = self._parse_transcript(resp)
        logger.info(
            f"{self.display_name} 文字起こし完了: {self.last_api_time:.0f}ms, {len(text)} 文字"
        )
        return text
