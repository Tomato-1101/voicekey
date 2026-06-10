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

from .audio_utils import numpy_to_wav_bytes
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

    def is_available(self) -> bool:
        """API キーが設定されており利用可能かを返す。"""
        return bool(self._resolve_api_key())

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
                    headers={"Authorization": f"Bearer {api_key}"},
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

        api_start = time.perf_counter()
        try:
            resp = client.post(
                "/audio/transcriptions",
                data=data,
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            )
        except httpx.TimeoutException:
            raise TranscriptionError(
                f"{self.display_name} API がタイムアウトしました（ネットワークを確認してください）"
            )
        except httpx.HTTPError as e:
            raise TranscriptionError(f"{self.display_name} API への接続に失敗しました: {e}")
        self.last_api_time = (time.perf_counter() - api_start) * 1000

        if resp.status_code == 401:
            raise TranscriptionError(f"{self.display_name} の API キーが無効です（設定を確認してください）")
        if resp.status_code == 429:
            raise TranscriptionError(f"{self.display_name} API のレート制限に達しました（しばらく待って再試行してください）")
        if resp.status_code >= 400:
            # エラー詳細は JSON のことが多いが、先頭だけ載せれば原因特定には足りる
            raise TranscriptionError(
                f"{self.display_name} API エラー (HTTP {resp.status_code}): {resp.text[:200]}"
            )

        text = resp.text.strip()
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

    display_name = "OpenAI"
    base_url = "https://api.openai.com/v1"
    keychain_service = secrets.SERVICE_OPENAI
    env_var = "OPENAI_API_KEY"
    available_models = (
        "gpt-4o-transcribe",       # 高精度
        "gpt-4o-mini-transcribe",  # コスト効率重視
    )


class GroqTranscriber(ApiTranscriber):
    """Groq Cloud の Whisper API（OpenAI 互換エンドポイント）。"""

    display_name = "Groq"
    base_url = "https://api.groq.com/openai/v1"
    keychain_service = secrets.SERVICE_GROQ
    env_var = "GROQ_API_KEY"
    available_models = (
        "whisper-large-v3-turbo",  # 最速（リアルタイムの 200 倍超）
        "whisper-large-v3",        # 高精度
    )
