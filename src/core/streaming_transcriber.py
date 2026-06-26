"""
Deepgram リアルタイム文字起こしモジュール（WebSocket ストリーミング）

録音中の 16kHz PCM チャンクを WebSocket で逐次送り、暫定（interim）と確定
（is_final）のテキストをリアルタイムに受け取る。HUD のライブ字幕に使う。
ベンチマーク（2026-06-10）で Deepgram nova-3 が「離した瞬間にほぼ確定
（94〜109ms）」かつ最高精度だったため、ストリーミングの既定バックエンドに採用。

Mac 版 StreamingTranscriber.swift（URLSessionWebSocketTask）の Python 移植。
アプリ全体がスレッドベース設計（asyncio 不使用）のため、ここでも websockets の
同期クライアント（websockets.sync.client）を使い、受信は専用スレッドで回す:

- 送信（send）: audio コールバックスレッドから呼ばれる
- 受信ループ（_run）: 接続確立 → 暫定/確定の受信 → 終端検出
- 開始/終了（start/finish）: アプリのワーカースレッドから呼ばれる
共有状態は _lock（threading.Lock）で保護する。

websockets が未導入の環境では start() が False を返し、呼び出し側は REST
文字起こしへ自動フォールバックする（keyring 不在時と同じ縮退方針）。
"""

import json
import os
import threading
import time
from typing import Callable, List, Optional
from urllib.parse import urlencode

import numpy as np
import numpy.typing as npt

from . import backend_client
from .text_utils import strip_cjk_spaces
from ..utils import secrets
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 接続確立のタイムアウト（秒）
_OPEN_TIMEOUT: float = 5.0
# finish() で確定を待つ最大時間（秒）。接続ハング時の保険
_FINISH_TIMEOUT: float = 3.0
# 受信ループのポーリング間隔（秒）。closed フラグを定期確認するため
_RECV_POLL_SEC: float = 0.5
# finish() が接続確立前に呼ばれた時、接続を待つ猶予（秒）。
# 短い発話（接続確立 ~0.2s より短いキータップ）でも取りこぼさないため
_CONNECT_GRACE_SEC: float = 1.0


def _float32_to_pcm16le(samples: npt.NDArray[np.float32]) -> bytes:
    """
    float32[-1,1] のモノラル音声を 16bit リトルエンディアン PCM のバイト列に変換する。

    範囲外サンプルはクリップして int16 ラップアラウンドによる轟音化を防ぐ。
    """
    clipped = np.clip(samples, -1.0, 1.0)
    # '<i2' で明示的にリトルエンディアン int16（Deepgram の encoding=linear16 用）
    return (clipped * 32767).astype("<i2").tobytes()


class StreamingTranscriber:
    """
    Deepgram WebSocket による逐次文字起こしセッション（1 録音 = 1 インスタンス）。

    Attributes:
        on_interim: 全文（確定 + 暫定）の更新通知コールバック。HUD 用。
                    受信スレッドから呼ばれるため、UI 反映側はメインスレッドへホップすること。
    """

    def __init__(
        self,
        model: str,
        language: str = "ja",
        on_interim: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        StreamingTranscriber を初期化する（ネットワークアクセスはしない）。

        Args:
            model: Deepgram モデル名（nova-3 / nova-2）
            language: 言語コード（nova-3 では内部的に multi に変換される）
            on_interim: 暫定テキスト更新コールバック
        """
        self.model = model
        self.language = language
        self.on_interim = on_interim

        self._lock = threading.Lock()
        self._ws = None                       # 接続確立後にセット（websockets ClientConnection）
        self._finals: List[str] = []          # 確定済みセグメント
        self._interim: str = ""               # 直近の暫定セグメント
        self._pending: List[bytes] = []        # 接続前に届いた PCM の退避
        self._cancelled = False               # finish/cancel が接続前に呼ばれた
        self._closed = False                  # 受信ループ停止フラグ
        self._done_event = threading.Event()  # 確定完了/切断の通知
        self._thread: Optional[threading.Thread] = None

    @property
    def _stream_language(self) -> str:
        """
        Deepgram の言語パラメータ（REST 側 DeepgramTranscriber と同じ規則）。

        nova-3 は日本語の単言語指定に未対応のため多言語モード（multi）を使う。
        """
        if self.model.startswith("nova-3"):
            return "multi"
        return self.language or "ja"

    def _resolve_api_key(self) -> Optional[str]:
        """Deepgram の API キーを Keychain → 環境変数の順で解決する。"""
        return secrets.get_api_key(secrets.SERVICE_DEEPGRAM) or os.environ.get("DEEPGRAM_API_KEY")

    def start(self) -> bool:
        """
        WebSocket 受信スレッドを起動する（即座に返る）。

        実際の接続確立は受信スレッド側で行うため、ここではブロックしない。
        接続前に届いた PCM は send() がバッファし、接続確立後に順序を保って送出する。

        Returns:
            開始できれば True。キー未設定かつ未ログイン / websockets 未導入なら False
            （呼び出し側は REST にフォールバックする）
        """
        # 製品版（ログイン済み）はサーバーから短命 JWT を取得して接続する。
        # 開発ビルドは従来どおり埋め込み/設定キーを使う（並存ガード）。
        # 配布版は埋め込みキーへフォールバックしない（無料ゲート＝ログイン必須）。
        # 配布版で未ログインなら key=None のまま False を返し、呼び出し側の REST
        # フォールバックへ落とす（そこで _dist_guard がログイン必須メッセージを出す）。
        logged_in = backend_client.is_logged_in()
        key = None if secrets.is_dist_build() else self._resolve_api_key()
        if not logged_in and not key:
            logger.warning("Deepgram 未ログイン（配布版）またはキー未設定のためストリーミングを開始できません")
            return False

        try:
            # websockets は遅延 import（未導入環境でもアプリ起動を阻害しない）
            from websockets.sync.client import connect  # noqa: F401
        except Exception as e:
            logger.warning(f"websockets が利用できないためストリーミングを無効化します: {e}")
            return False

        self._thread = threading.Thread(
            target=self._run, args=(key, logged_in), daemon=True, name="DeepgramStream"
        )
        self._thread.start()
        return True

    def send(self, samples: npt.NDArray[np.float32]) -> None:
        """
        16kHz モノラル Float32 チャンクを送信する（audio スレッドから呼ばれる）。

        接続確立前のチャンクはバッファし、確立時に順序を保ってまとめて送る。
        """
        if samples is None or len(samples) == 0:
            return
        pcm = _float32_to_pcm16le(samples)
        with self._lock:
            if self._cancelled or self._closed:
                return
            ws = self._ws
            if ws is None:
                # まだ接続前 → 退避（接続確立時に _run がフラッシュする）
                self._pending.append(pcm)
                return
            try:
                ws.send(pcm)
            except Exception as e:
                logger.debug(f"ストリーミング送信エラー（無視）: {e}")

    def finish(self, timeout: float = _FINISH_TIMEOUT) -> str:
        """
        送信を打ち切り、確定テキストを返す（ホットキーを離したときに呼ぶ）。

        CloseStream を送ると Deepgram は残りを確定して Metadata を返し接続を閉じる。
        取りこぼし対策で最大 timeout 秒だけ待つ。アプリのワーカースレッドから
        呼ばれるためブロックしてよい（UI/リスナーは塞がない）。

        Returns:
            確定テキスト（CJK 隣接スペース除去済み）。接続失敗・無音なら空文字
        """
        with self._lock:
            ws = self._ws

        # 接続確立前に呼ばれた場合、短時間だけ接続（または終了）を待つ。
        # これがないと短い発話で「接続前に finish → セッション破棄 → 空文字」になり、
        # 毎回 REST フォールバックへ落ちてストリーミングの利点が消える
        if ws is None:
            grace_end = time.monotonic() + _CONNECT_GRACE_SEC
            while time.monotonic() < grace_end and not self._done_event.is_set():
                with self._lock:
                    ws = self._ws
                    closed = self._closed
                if ws is not None or closed:
                    break
                time.sleep(0.02)

        with self._lock:
            ws = self._ws
            if ws is None:
                # 猶予内に接続できなかった → 受信スレッドに後始末させる
                self._cancelled = True

        if ws is not None:
            try:
                ws.send(json.dumps({"type": "CloseStream"}))
            except Exception as e:
                logger.debug(f"CloseStream 送信エラー（無視）: {e}")

        # 確定（Metadata 受信）または切断まで待つ
        self._done_event.wait(timeout)
        self._shutdown_socket()
        return strip_cjk_spaces(self._current_text())

    def cancel(self) -> None:
        """結果を使わずに接続を破棄する（録音破棄時など）。"""
        with self._lock:
            self._cancelled = True
        self._shutdown_socket()
        self._resolve_finish()

    def _shutdown_socket(self) -> None:
        """受信ループを止め、WebSocket を閉じる（多重呼び出し安全）。"""
        with self._lock:
            self._closed = True
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception as e:
                logger.debug(f"WebSocket close エラー（無視）: {e}")

    def _current_text(self) -> str:
        """確定 + 暫定を結合した現在の全文（スペース除去前）。"""
        with self._lock:
            parts = self._finals + ([self._interim] if self._interim else [])
        return "".join(parts)

    # ------------------------------------------------------------------
    # 受信スレッド
    # ------------------------------------------------------------------

    def _run(self, key: Optional[str], logged_in: bool) -> None:
        """接続 → 退避済み PCM のフラッシュ → 受信ループ（専用スレッド上）。

        Args:
            key: 直叩き用の Deepgram API キー（未ログイン時に使う。ログイン時は None でも可）
            logged_in: True なら自社サーバーから短命 JWT を取得して Bearer で接続する
        """
        from websockets.sync.client import connect

        # 製品版（ログイン済み）: サーバーから短命 JWT を取得し Bearer で接続。
        # 未ログイン: 従来どおり埋め込み/設定キーを Token で使う（並存ガード）。
        if logged_in:
            try:
                grant = backend_client.fetch_ephemeral_token()
                auth_header = f"Bearer {grant['token']}"
            except backend_client.BackendError as e:
                # 短命トークン取得失敗（未ログイン扱い・サブスク無効・通信失敗など）
                # → finish() を解決し、REST フォールバックに委ねる
                logger.warning(f"Deepgram 短命トークンの取得に失敗: {e}")
                self._resolve_finish()
                return
        else:
            auth_header = f"Token {key}"

        params = {
            "model": self.model,
            "language": self._stream_language,
            "encoding": "linear16",
            "sample_rate": "16000",
            "channels": "1",
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
        }
        url = "wss://api.deepgram.com/v1/listen?" + urlencode(params)

        try:
            ws = connect(
                url,
                additional_headers={"Authorization": auth_header},
                open_timeout=_OPEN_TIMEOUT,
            )
        except Exception as e:
            # 接続失敗 → finish() を解決して REST フォールバックに委ねる
            logger.warning(f"Deepgram ストリーミング接続に失敗: {e}")
            self._resolve_finish()
            return

        logger.info(
            f"Deepgram ストリーミング開始 (model={self.model}, lang={self._stream_language})"
        )

        # 退避済み PCM を順序保証のためロック下でフラッシュしてから ws を公開する
        close_now = False
        with self._lock:
            if self._cancelled or self._closed:
                close_now = True
            else:
                self._ws = ws
                for chunk in self._pending:
                    try:
                        ws.send(chunk)
                    except Exception as e:
                        logger.debug(f"退避 PCM の送信エラー（無視）: {e}")
                self._pending = []
        if close_now:
            try:
                ws.close()
            except Exception:
                pass
            self._resolve_finish()
            return

        self._receive_loop(ws)
        # ループ脱出（切断・終端）→ finish() を解決
        self._resolve_finish()

    def _receive_loop(self, ws) -> None:
        """確定/暫定メッセージを受信し続けるループ。"""
        # ConnectionClosed は websockets の例外。型に依存せずクラス名で握る
        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                message = ws.recv(timeout=_RECV_POLL_SEC)
            except TimeoutError:
                continue  # closed フラグの再確認のためループ継続
            except Exception:
                # ConnectionClosed（正常終端含む）・その他切断
                return
            try:
                self._handle(message)
            except Exception as e:
                logger.debug(f"ストリーミング応答の処理でエラー（無視）: {e}")

    def _handle(self, message) -> None:
        """Deepgram のストリーミング応答 1 件を処理する。"""
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8", errors="ignore")
        msg = json.loads(message)

        # Metadata は CloseStream 後の終端通知 → 確定完了とみなす
        if msg.get("type") == "Metadata":
            self._resolve_finish()
            return

        channels = (msg.get("channel") or {}).get("alternatives") or []
        if not channels:
            return
        transcript = (channels[0].get("transcript") or "").strip()

        with self._lock:
            if msg.get("is_final"):
                if transcript:
                    self._finals.append(transcript)
                self._interim = ""
            else:
                self._interim = transcript
            snapshot = "".join(self._finals + ([self._interim] if self._interim else []))

        # HUD へは日本語スペースを除去した見た目で渡す
        cb = self.on_interim
        if cb is not None:
            try:
                cb(strip_cjk_spaces(snapshot))
            except Exception:
                pass  # HUD 側の例外でストリーミングを止めない

    def _resolve_finish(self) -> None:
        """finish() の待機を一度だけ解除する（終端 / 切断 / タイムアウトのいずれか）。"""
        self._done_event.set()
