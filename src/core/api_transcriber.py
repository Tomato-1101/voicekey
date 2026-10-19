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

2026-10-19（Mac に追いつかせる作業）で Mac と同じエンジンをそろえた:
- OpenAI は gpt-transcribe（json 応答・languages[]）。Whisper 用の数字の例文は送らない
- ElevenLabs は scribe_v2 を既定にし、tag_audio_events=false を送る
- Microsoft MAI-Transcribe-2（Azure Speech の LLM Speech API）を追加
- Soniox はライブ専用で REST を持たない。ここに来るのはライブで文字が取れなかったときだけで、
  録音全体を新しいライブセッションへ流し直して救う（SonioxTranscriber）
- OpenAI ライブの REST フォールバックは同世代の一括用モデル gpt-transcribe で送る
- 失敗の通知文にサーバー応答の本文を混ぜない（本文は detail に分けてログにだけ出す）
- キーが未設定・無効の失敗は needs_api_key を立て、アプリが設定 › API キー へ案内する
"""

import json
import threading
import time
from typing import Optional, Tuple
from urllib.parse import urlsplit

import httpx
import numpy as np
import numpy.typing as npt

from . import backend_client
from .audio_utils import numpy_to_wav_bytes
from .text_utils import strip_cjk_spaces
from ..config import backends
from ..config.constants import SAMPLE_RATE
from ..utils import secrets
from ..utils.logger import get_logger

logger = get_logger(__name__)

# API リクエストのタイムアウト（読み取り 20 秒 / 接続 5 秒）
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

# Whisper 系（Groq / OpenAI）へ送る数字表記の style プロンプト。Whisper は prompt の表記
# スタイルに追従する性質があるため、半角数字を含む短い例文を与えて漢数字化を抑制する
# （Mac 版 Transcriber.numeralStyleHint と文言を一致させること）。
_NUMERAL_STYLE_PROMPT = "数字は半角で表記します。例: 2026年7月15日、3人で1200円、成功率98.5%。"


class TranscriptionError(RuntimeError):
    """文字起こしの失敗を表す例外。メッセージはそのままユーザー通知に使える日本語。

    Attributes:
        needs_api_key: キーが未設定・無効・接続先未設定など「設定 › API キー」で直せる失敗か。
            True のときアプリが API キーの設定画面へ案内する（Mac の needsApiKey と同じ）
        detail: サーバー応答本文（1 行化・切り詰め済み）。原因の切り分けに要るが、生 JSON を
            通知に出すと読めないので message とは分け、ログにだけ出す
    """

    def __init__(self, message: str, needs_api_key: bool = False, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.needs_api_key = needs_api_key
        self.detail = detail


def response_detail(body: str, limit: int = 200) -> str:
    """応答本文をログ 1 行に収まる形へ整える（改行・制御文字を空白にして詰め、limit 文字で切る）。

    HTML のエラーページのような巨大な本文でも全体を処理しないよう、先に頭だけ取る（Mac と同じ）。
    """
    head = (body or "")[: limit * 4]
    flattened = "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in head)
    one_line = " ".join(flattened.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[:limit] + "…"


def missing_key_message(backend: str) -> str:
    """キー未設定の案内文。入力先（設定 › API キー）をそのまま案内する（Mac の missingKeyMessage と同じ）。"""
    return f"{backends.label(backend)} の API キーが未設定です（設定 › API キー で入力してください）"


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
    # 使う API キーの項目（utils.secrets.API_KEY_ITEMS のキー）。読み順はアプリ保存値 → 環境変数 → 共有の置き場
    key_item: str = ""
    # エンジン識別子（エラー文・キー未設定の案内に使う）
    backend: str = ""
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
        """API キーをアプリ保存値 → 環境変数 → 共有の置き場の順で解決する（Mac の Keychain.apiKey と同じ）。"""
        if self.key_item:
            return secrets.api_key(self.key_item)
        import os
        return secrets.get_api_key(self.keychain_service) or os.environ.get(self.env_var)

    def _missing_key_error(self) -> TranscriptionError:
        """キー未設定の失敗（設定 › API キー へ案内する）。"""
        if self.backend:
            return TranscriptionError(missing_key_message(self.backend), needs_api_key=True)
        return TranscriptionError(
            f"{self.display_name} の API キーが未設定です（設定 › API キー で入力してください）",
            needs_api_key=True,
        )

    def _whisper_prompt(self) -> str:
        """Whisper（Groq/OpenAI）へ送る文字起こしプロンプトを組み立てる。

        数字を半角で出させる固定 style ヒントを先頭に置き、ユーザー設定のプロンプト（あれば）を
        続ける（release はプロンプト非選択＝self.prompt は空なので style ヒントのみになる）。
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
        if resp.status_code == 401:
            raise TranscriptionError(
                f"{self.display_name} の API キーが無効です（設定 › API キー を確認してください）",
                needs_api_key=True,
            )
        if resp.status_code == 429:
            raise TranscriptionError(f"{self.display_name} API のレート制限に達しました（しばらく待って再試行してください）")
        if resp.status_code >= 400:
            # 本文は通知に出さない（detail に分けてログにだけ残す。生 JSON は読めないうえ通知が長くなる）
            try:
                body = resp.text
            except Exception:
                body = ""
            raise TranscriptionError(
                f"{self.display_name} API エラー (HTTP {resp.status_code})",
                detail=response_detail(body),
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
        """配布（DIST）ビルドのゲート: 文字起こしにはログインを必須にする。

        配布版はアプリに長期キーを埋め込まない。ログイン後はまず無料体験枠で使え、
        枠を使い切るとアクティベーションキーの登録が必要になる。無料枠/キー/サブスクの
        有効性判定はサーバー側（ephemeral/proxy が 402/403 を返す）で行うため、ここでは
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
                "ログインすると無料体験で使えます（設定 → アカウント）"
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
                    raise self._missing_key_error()
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

    def transcribe(
        self, audio_data: npt.NDArray[np.float32], server_format: bool = False
    ) -> str:
        """
        音声を文字起こしする。

        Args:
            audio_data: 音声データ（float32、モノラルの NumPy 配列）
            server_format: サーバー統合整形（STT と整形を 1 リクエストで）を依頼するか。
                基底実装（直叩き）では無視する。groq × ログイン済みプロキシ経路のみが実効
                （GroqTranscriber が backend_client へ伝搬する）。

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

        # 送信形式と応答の解析形式は同じ判定を使う（途中でモデル設定が変わっても食い違わないよう 1 回だけ評価）
        rest_model = self.rest_model
        gpt_format = self.uses_gpt_transcribe_format(rest_model)
        data = self.build_form(rest_model, gpt_format)

        resp = self._post(
            client,
            "/audio/transcriptions",
            data=data,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        )

        if gpt_format:
            # gpt-transcribe は json 応答（text に本文）
            try:
                raw = str(resp.json().get("text") or "")
            except Exception:
                raise TranscriptionError(f"{self.display_name} API の応答を解析できませんでした")
        else:
            raw = resp.text
        self._record_usage(rest_model, len(audio_data) / float(self.sample_rate))

        # streaming/REST・Mac/Windows で同じ結果にするため共通正規化を通す（#21）。
        # OpenAI/Groq も日本語に単語間スペースが入りうるため CJK 隣接スペースを除去する。
        text = strip_cjk_spaces(raw.strip())
        logger.info(
            f"{self.display_name} 文字起こし完了: {self.last_api_time:.0f}ms, {len(text)} 文字"
        )
        return text

    # --- OpenAI 互換フォームの組み立て（テストから送信内容を確かめるため公開している） ---

    @property
    def rest_model(self) -> str:
        """REST（音声ファイル一括）に投げるモデル名。サブクラスで差し替える（OpenAI ライブ）。"""
        return self.model

    def uses_gpt_transcribe_format(self, model: Optional[str] = None) -> bool:
        """gpt-transcribe の送信形式（json 応答・languages[]）で送るか。Whisper 系は従来の text 応答。"""
        name = self.rest_model if model is None else model
        return self.backend != backends.GROQ and str(name).startswith("gpt-transcribe")

    def build_form(self, model: str, gpt_format: bool) -> dict:
        """OpenAI 互換 Audio Transcriptions API の multipart フォーム（空のオプションは送らない）。"""
        data = {"model": model}
        if gpt_format:
            # gpt-transcribe は json 応答で、言語は複数指定の languages[] で渡す
            # （単数の language と両方送らない。temperature は仕様に無いので送らない）
            data["response_format"] = "json"
            if self.language:
                data["languages[]"] = self.language
            # prompt は自由形式の「文脈」として読まれるので、Whisper 用の数字の例文を混ぜると
            # 例文そのものが出力に漏れうる。ユーザーのプロンプトだけを送る（空なら送らない）。
            # 数字の表記はアプリ側の numeral_normalizer が半角へ直す
            user = self.prompt.strip()
            if user:
                data["prompt"] = user
        else:
            data["response_format"] = "text"
            data["temperature"] = str(self.temperature)
            if self.language:
                data["language"] = self.language
            # Whisper 系は prompt の表記に追従するので、数字を半角で出させる style プロンプト
            # （＋ユーザー設定プロンプト）を常に付与する
            data["prompt"] = self._whisper_prompt()
        return data

    def _record_usage(self, model: str, seconds: float) -> None:
        """API 使用量（音声秒数）を記録する（数量だけ・本文は渡さない）。失敗しても文字起こしは止めない。"""
        try:
            from . import api_usage
            api_usage.record_audio(self.backend or self.display_name, model, seconds)
        except Exception:
            pass

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
    """OpenAI Audio Transcriptions API（gpt-transcribe。2026-10-01 に旧 gpt-4o 系から一本化）。"""

    display_name = "OpenAI"  # personal は実プロバイダー名で表示する（Mac と同じ）
    base_url = "https://api.openai.com/v1"
    keychain_service = secrets.SERVICE_OPENAI
    env_var = "OPENAI_API_KEY"
    key_item = "openai"
    backend = backends.OPENAI
    available_models = backends.known_models(backends.OPENAI)


class OpenAILiveRestTranscriber(OpenAITranscriber):
    """OpenAI ライブ（gpt-live-transcribe）の REST フォールバック。

    gpt-live-transcribe は Realtime WS 専用で REST に投げると 404 になるため、ライブ接続が
    張れなかったとき（キー無し・WS 失敗）だけ、同世代の一括用モデル gpt-transcribe で送る（Mac と同じ）。
    """

    display_name = "OpenAI ライブ"
    backend = backends.OPENAI_LIVE
    available_models = backends.known_models(backends.OPENAI_LIVE)

    @property
    def rest_model(self) -> str:
        return "gpt-transcribe"


class GroqTranscriber(ApiTranscriber):
    """Groq Cloud の Whisper API（OpenAI 互換エンドポイント）。

    製品版(release)の「高速」＝普通入力。Groq は Deepgram のような短命トークンが無いため、
    ログイン済み(製品版)ではサーバープロキシ(/api/v1/transcribe/groq)経由で文字起こしする。
    未ログイン(開発直叩き)では OpenAI 互換 REST を親クラスで直叩きする（並存ガード）。
    """

    display_name = "Groq"  # personal は実プロバイダー名で表示する（Mac と同じ）
    base_url = "https://api.groq.com/openai/v1"
    keychain_service = secrets.SERVICE_GROQ
    env_var = "GROQ_API_KEY"
    key_item = "groq"
    backend = backends.GROQ
    # turbo が REST 最速で精度も良好。whisper-large-v3 は 2026-10-01 に選択肢から外した（Mac と同じ）
    available_models = backends.known_models(backends.GROQ)

    def transcribe(
        self, audio_data: npt.NDArray[np.float32], server_format: bool = False
    ) -> str:
        """音声を Groq で文字起こしする（製品版はプロキシ経由・開発は直叩き）。

        server_format=True かつプロキシ経路のときは、STT→整形をサーバーで統合実行させ
        整形済みテキストを受け取る（呼び出し側は再整形しない＝単発送信時のみ使う）。
        """
        if len(audio_data) == 0:
            return ""

        # 配布版ゲート: ログイン＋アクティベーション必須（Groq はプロキシ対応＝True）
        self._dist_guard(server_supported=True)

        # 製品版（ログイン済み）はサーバープロキシ経由で文字起こしする。
        # Groq バッチも短命キー非対応のためプロキシを使う（EL と同じ並存ガード）。
        if backend_client.is_logged_in():
            wav_bytes = numpy_to_wav_bytes(audio_data, self.sample_rate)
            try:
                # プロキシ経由でも直叩きと同じ正規化を通す（#21・CJK 隣接スペース除去）。
                # 数字を半角で出させる Whisper style プロンプトを渡す（サーバーが Groq へ転送する）。
                return strip_cjk_spaces(
                    backend_client.transcribe_groq(
                        wav_bytes, self.language or "", server_format=server_format,
                        prompt=self._whisper_prompt(),
                    )
                )
            except backend_client.BackendError as e:
                raise TranscriptionError(str(e))

        # 未ログイン（開発直叩き）: OpenAI 互換 REST を親クラスで叩く（server_format は無視）。
        # 親の _dist_guard(server_supported=False) は dev ビルドでは no-op なので安全。
        return super().transcribe(audio_data)


class ElevenLabsTranscriber(ApiTranscriber):
    """
    ElevenLabs Scribe の Speech-to-Text API（REST）。

    OpenAI 互換ではないため transcribe() と認証ヘッダーを独自実装する:
    - 認証は xi-api-key ヘッダー
    - multipart のフィールド名は model_id（model ではない）/ language_code
    - 応答は JSON で text フィールドに本文が入る

    2026-10-01 から scribe_v2 を既定にした（公開情報で日本語精度が上位・Mac と同じ）。
    scribe_v1 はハンズフリー録音の内部利用（長時間録音の実績側）と比較用に残す。
    """

    display_name = "ElevenLabs"  # personal は実プロバイダー名で表示する（Mac と同じ）
    base_url = "https://api.elevenlabs.io/v1"
    keychain_service = secrets.SERVICE_ELEVENLABS
    env_var = "ELEVENLABS_API_KEY"
    key_item = "elevenlabs"
    backend = backends.ELEVENLABS
    available_models = backends.known_models(backends.ELEVENLABS)

    def _auth_headers(self, api_key: str) -> dict:
        """ElevenLabs は Bearer ではなく xi-api-key ヘッダーで認証する。"""
        return {"xi-api-key": api_key}

    def transcribe(
        self, audio_data: npt.NDArray[np.float32], server_format: bool = False
    ) -> str:
        """音声を ElevenLabs Scribe で文字起こしする（server_format は無視＝整形統合は Groq のみ）。"""
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

        # model ではなく model_id。言語は language_code（空なら自動判定）。
        # (笑い) などの音声イベントタグは音声入力には不要。scribe_v2 は既定で付けるので必ず明示する
        data = {"model_id": self.model, "tag_audio_events": "false"}
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
        except Exception:
            raise TranscriptionError(f"{self.display_name} API の応答を解析できませんでした")
        self._record_usage(self.model, len(audio_data) / float(self.sample_rate))

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

    display_name = "Deepgram"  # personal は実プロバイダー名で表示する（選択肢からは外した旧エンジン）
    base_url = "https://api.deepgram.com/v1"
    keychain_service = secrets.SERVICE_DEEPGRAM
    env_var = "DEEPGRAM_API_KEY"
    key_item = "deepgram"
    backend = backends.DEEPGRAM
    available_models = (
        "nova-3",  # 多言語・最新（既定）
        "nova-2",  # 旧世代
    )

    def __init__(self, *args, **kwargs) -> None:
        """基底の初期化に加えて、短命 JWT 直叩き用クライアントの器を用意する。"""
        super().__init__(*args, **kwargs)
        # 固定の認証ヘッダーを持たない共有クライアント（Bearer はリクエスト毎に渡す）
        self._jwt_client: Optional[httpx.Client] = None
        self._jwt_client_lock = threading.Lock()

    def _auth_headers(self, api_key: str) -> dict:
        """Deepgram は Bearer ではなく Token スキームで認証する。"""
        return {"Authorization": f"Token {api_key}"}

    def _get_jwt_client(self) -> httpx.Client:
        """短命 JWT 直叩き用の共有 httpx クライアントを取得または生成する。

        録音ごとにモジュール関数 httpx.post を使うと毎回 TCP+TLS を張り直し、確定
        レイテンシにハンドシェイク分が乗る。API キーを持たない配布版でも使えるよう
        認証ヘッダーは固定せず（リクエスト毎に Bearer を渡す）、接続プールと prewarm の
        暖機だけを共有する。httpx.Client はスレッドセーフなので並列送信でも共有できる。
        """
        with self._jwt_client_lock:
            if self._jwt_client is None:
                self._jwt_client = httpx.Client(
                    base_url=self.base_url,
                    timeout=_TIMEOUT,
                    # keepalive を長めに取り、録音中に prewarm した接続を再利用する
                    limits=httpx.Limits(
                        max_connections=5,
                        max_keepalive_connections=2,
                        keepalive_expiry=60.0,
                    ),
                    transport=httpx.HTTPTransport(retries=2),
                )
            return self._jwt_client

    def prewarm(self) -> None:
        """製品版（ログイン済み）は JWT 直叩き用クライアントの接続を温める。

        基底の prewarm は API キー必須の共有クライアントを温めるため、キーを持たない
        配布版では何も温まらず、_transcribe_via_jwt の毎回のハンドシェイクが残っていた。
        """
        if not backend_client.is_logged_in():
            super().prewarm()
            return
        try:
            client = self._get_jwt_client()
            # 認証なしなので 401 が返るが、TCP+TLS の確立という目的は達せられる
            client.get("/models", timeout=httpx.Timeout(5.0, connect=5.0))
            logger.debug(f"{self.display_name} 接続をプリウォームしました")
        except Exception as e:
            logger.debug(f"{self.display_name} プリウォーム失敗（無視）: {e}")

    def close(self) -> None:
        """基底の共有クライアントに加えて JWT 直叩き用クライアントも閉じる。"""
        super().close()
        with self._jwt_client_lock:
            client = self._jwt_client
            self._jwt_client = None
        if client is not None:
            try:
                client.close()
            except Exception as e:
                logger.warning(f"{self.display_name} クライアント close に失敗: {e}")

    @property
    def _dg_language(self) -> str:
        """
        Deepgram の言語パラメータ。

        nova-3 は当初 ja 単言語指定に未対応で多言語モード（multi）を使っていたが、
        multi は日本語発話を韓国語等に誤判定することがあり、現在は ja がサポート済み
        （2026-07 Deepgram ドキュメント確認）のため単言語をそのまま送る。
        """
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
        キャッシュ済みクライアント（固定 Token ヘッダー）はバイパスし、認証ヘッダーを
        持たない共有クライアント（prewarm 済み）へリクエスト毎に Bearer JWT を載せて送る。
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
        resp = self._post(
            self._get_jwt_client(),
            "/listen",
            params=params,
            content=wav_bytes,
            headers={
                "Authorization": f"Bearer {grant['token']}",
                "Content-Type": "audio/wav",
            },
        )

        text = self._parse_transcript(resp)
        logger.info(
            f"{self.display_name} 文字起こし完了: {self.last_api_time:.0f}ms, {len(text)} 文字"
        )
        # 文字起こしが成立したら無料体験の消費を確定する（ベストエフォート・非ブロッキング）。
        # ストリーミングが空文字で REST へフォールバックしたとき、その 1 録音を数えるのはこの経路。
        # 段階1=保留 ID(jti) を確定 / 段階3=再利用トークンの回数を +1（jti なし）。
        if text:
            jti = grant.get("jti")
            if jti:
                threading.Thread(
                    target=backend_client.confirm_usage, args=(jti,), daemon=True
                ).start()
            elif grant.get("meter"):
                threading.Thread(
                    target=backend_client.confirm_usage_count, daemon=True
                ).start()
        return text

    def transcribe(
        self, audio_data: npt.NDArray[np.float32], server_format: bool = False
    ) -> str:
        """音声を Deepgram Listen（事前録音）で文字起こしする（server_format は無視＝整形統合は Groq のみ）。"""
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


# Azure の API バージョン（LLM Speech API の MAI-Transcribe 対応版）
_AZURE_API_VERSION = "2025-10-15"


def azure_base_url(endpoint: Optional[str]) -> Optional[str]:
    """AZURE_SPEECH_ENDPOINT を正規化したベース URL（scheme + host + port だけを使う）。

    Azure ポータルからコピーした値に余計なパス・クエリが付いていても壊れないよう、それらは捨てる。
    scheme の無い値（xxx.cognitiveservices.azure.com）は https を補う。
    未設定・空白を含む値・ホストを持たない値は None（Mac の azureBaseURL と同じ規則）。
    """
    text = (endpoint or "").strip()
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
    if not host:
        return None
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}"


def azure_transcribe_url(endpoint: Optional[str]) -> Optional[str]:
    """文字起こしの送信先 URL（未設定なら None）。"""
    base = azure_base_url(endpoint)
    if base is None:
        return None
    return f"{base}/speechtotext/transcriptions:transcribe?api-version={_AZURE_API_VERSION}"


def azure_definition_json(model: str, language: str) -> str:
    """multipart の definition（LLM Speech の enhancedMode で MAI モデルを指定する）。

    言語が空なら locales を付けない＝自動判定に任せる。
    """
    definition: dict = {"enhancedMode": {"enabled": True, "model": model}}
    lang = (language or "").strip()
    if lang:
        definition["locales"] = [lang]
    return json.dumps(definition, ensure_ascii=False, sort_keys=True)


def parse_azure_response(payload) -> Optional[str]:
    """Azure の応答から本文を取り出す（combinedPhrases[0].text を優先し、無ければ phrases を連結）。

    無音（どちらも空配列）は空文字、形が違えば None（解析失敗）。
    """
    if not isinstance(payload, dict):
        return None
    combined = payload.get("combinedPhrases")
    if isinstance(combined, list) and combined and isinstance(combined[0], dict):
        text = combined[0].get("text")
        if isinstance(text, str):
            return text
    phrases = payload.get("phrases")
    if isinstance(phrases, list):
        # 区切りの空白は strip_cjk_spaces が日本語の間だけ取り除く
        return " ".join(p.get("text", "") for p in phrases if isinstance(p, dict) and isinstance(p.get("text"), str))
    return "" if isinstance(combined, list) else None


class AzureMAITranscriber(ApiTranscriber):
    """Microsoft MAI-Transcribe-2（Azure Speech の LLM Speech API・REST）。

    Azure はリソースごとに接続先が違うので、キー（AZURE_SPEECH_KEY）とは別に
    接続先（AZURE_SPEECH_ENDPOINT）を設定 › API キー で入れてもらう。
    認証は Ocp-Apim-Subscription-Key ヘッダー、multipart は audio + definition（JSON）。
    """

    display_name = "Microsoft MAI"
    key_item = "azure_key"
    backend = backends.AZURE_MAI
    available_models = backends.known_models(backends.AZURE_MAI)

    def _auth_headers(self, api_key: str) -> dict:
        """Azure は Ocp-Apim-Subscription-Key ヘッダーで認証する。"""
        return {"Ocp-Apim-Subscription-Key": api_key}

    def _get_client(self) -> httpx.Client:
        """認証ヘッダー付きクライアント（接続先はリクエストごとに完全 URL で渡す）。"""
        with self._client_lock:
            if self._client is None:
                api_key = self._resolve_api_key()
                if not api_key:
                    raise self._missing_key_error()
                self._client = httpx.Client(
                    headers=self._auth_headers(api_key),
                    timeout=_TIMEOUT,
                    limits=httpx.Limits(
                        max_connections=5, max_keepalive_connections=2, keepalive_expiry=60.0,
                    ),
                    transport=httpx.HTTPTransport(retries=2),
                )
            return self._client

    def prewarm(self) -> None:
        """TLS を張るだけ（認証なしの GET で足りる）。失敗は無視する。"""
        base = azure_base_url(secrets.api_key("azure_endpoint"))
        if base is None or not self._resolve_api_key():
            return
        try:
            self._get_client().get(base, timeout=httpx.Timeout(5.0, connect=5.0))
        except Exception as e:
            logger.debug(f"{self.display_name} プリウォーム失敗（無視）: {e}")

    def transcribe(
        self, audio_data: npt.NDArray[np.float32], server_format: bool = False
    ) -> str:
        """音声を MAI-Transcribe で文字起こしする（server_format は無視）。"""
        if len(audio_data) == 0:
            return ""
        # 配布版（製品版）には提供しない（personal 限定・Mac と同じ）
        if secrets.is_dist_build():
            raise TranscriptionError("この文字起こし方式は配布版では利用できません")
        client = self._get_client()
        url = azure_transcribe_url(secrets.api_key("azure_endpoint"))
        if url is None:
            raise TranscriptionError(
                "Microsoft の接続先（エンドポイント）が未設定です（設定 › API キー で入力してください）",
                needs_api_key=True,
            )
        wav_bytes = numpy_to_wav_bytes(audio_data, self.sample_rate)
        resp = self._post(
            client,
            url,
            data={"definition": azure_definition_json(self.model, self.language)},
            files={"audio": ("audio.wav", wav_bytes, "audio/wav")},
        )
        try:
            parsed = parse_azure_response(resp.json())
        except Exception:
            parsed = None
        if parsed is None:
            raise TranscriptionError(f"{self.display_name} API の応答を解析できませんでした")
        self._record_usage(self.model, len(audio_data) / float(self.sample_rate))
        text = strip_cjk_spaces(parsed.strip())
        logger.info(f"{self.display_name} 文字起こし完了: {self.last_api_time:.0f}ms, {len(text)} 文字")
        return text


class SonioxTranscriber(ApiTranscriber):
    """Soniox（stt-rt-v5）の「REST の代わり」＝録音全体を新しいライブセッションへ流し直す。

    Soniox はライブ（WebSocket）専用で REST を持たない。ここに来るのは、録音中のライブ接続で
    文字が取れなかったとき（接続失敗・キー無し）と、長文の分割送信のときだけ。
    録音はもう終わっているので実時間は待たず、100ms 分ずつ続けて送信キューへ積む（Mac と同じ）。
    """

    display_name = "Soniox"
    key_item = "soniox"
    backend = backends.SONIOX
    available_models = backends.known_models(backends.SONIOX)

    def prewarm(self) -> None:
        """REST を叩かないので温める接続が無い。"""
        return None

    def transcribe(
        self, audio_data: npt.NDArray[np.float32], server_format: bool = False
    ) -> str:
        """録音全体を新しいライブセッションへ流し直して文字起こしする。

        正常に終わって空なら空文字（本当に無言）。失敗して何も取れなければ原因別の文言で投げる。
        失敗でも取れた分があれば返す（喋った内容を捨てない）。
        """
        if len(audio_data) == 0:
            return ""
        if secrets.is_dist_build():
            raise TranscriptionError("この文字起こし方式は配布版では利用できません")
        api_key = self._resolve_api_key()
        if not api_key:
            raise self._missing_key_error()

        from .live_transcriber import (
            SonioxLiveSession, is_invalid_key_failure, soniox_failure_message,
        )

        session = SonioxLiveSession(model=self.model, language=self.language, prompt=self.prompt)
        session.on_usage = lambda sec: self._record_usage(self.model, sec)
        if not session.start(api_key=api_key):
            raise self._missing_key_error()
        start = time.perf_counter()
        chunk = self.sample_rate // 10
        for index in range(0, len(audio_data), chunk):
            session.send(audio_data[index:index + chunk])
        audio_seconds = len(audio_data) / float(self.sample_rate)
        text = session.finish(timeout=SonioxLiveSession.replay_finish_timeout(audio_seconds))
        ending = session.ending
        # 実時間より速く送ったときの処理時間は公式に明記が無い。待ち上限を実測で詰めるために必ず残す
        logger.info(
            f"Soniox 再送: 音声 {audio_seconds:.1f}s → {(time.perf_counter() - start) * 1000:.0f}ms, "
            f"{len(text)} 文字, 終わり方={ending.log_label}"
        )
        if ending.is_failed and not text:
            raise TranscriptionError(
                soniox_failure_message(ending), needs_api_key=is_invalid_key_failure(ending),
            )
        return text
