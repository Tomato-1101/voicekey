"""
メインアプリケーションコントローラーモジュール

音声録音、文字起こし、UI、ホットキー処理を統合するコントローラー。

設計方針（全面刷新後）:

1. **リスナーハンドラは絶対にブロックしない**: macOS では pynput のハンドラが
   CGEventTap のコールバックスレッドで実行され、ブロックすると OS がタップを
   無効化し、ホットキーが永久に効かなくなる（旧実装の最重要バグ）。
   ハンドラはキー集合の更新と AudioRecorder へのコマンド投函のみを行う。

2. **単一の状態発信点**: UI 状態（idle/recording/transcribing）は
   `_emit_state()` だけが内部状態から計算して通知する。状態の二重管理や
   通知漏れによる「アイコンが録音中のまま」を構造的に防ぐ。

3. **常駐ワーカー 1 本**: 文字起こしタスクは常駐スレッドが直列処理する。
   音量正規化 → VAD（発話なし即スキップ + 無音トリム）→ API →
   テキスト挿入まですべてワーカー上で行い、UI スレッドを塞がない。

4. **ウォッチドッグ**: 設定監視スレッドが PortAudio ハング検知（recover）、
   録音時間上限、リスナースレッド生存確認を兼任する。
"""

import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox
from pynput import keyboard

from .config import ConfigManager, HotkeyMode, TranscriptionBackend
from .config.constants import CONFIG_CHECK_INTERVAL_SEC, SAMPLE_RATE
from .config.types import TranscriptionTask
from .core import (
    ApiTranscriber,
    AudioRecorder,
    DeepgramTranscriber,
    ElevenLabsTranscriber,
    GroqTranscriber,
    InputHandler,
    OpenAITranscriber,
    SileroVad,
    StreamingTranscriber,
    TranscriptionError,
)
from .core import text_formatter
from .core.audio_preprocess import preprocess as preprocess_audio
from .platform import get_platform_adapter
from .ui import Hud, SettingsWindow, SystemTray
from .utils.logger import get_logger

logger = get_logger(__name__)

# バックエンド識別子 → トランスクライバクラスの対応
_BACKEND_CLASSES = {
    TranscriptionBackend.GROQ.value: GroqTranscriber,
    TranscriptionBackend.OPENAI.value: OpenAITranscriber,
    TranscriptionBackend.ELEVENLABS.value: ElevenLabsTranscriber,
    TranscriptionBackend.DEEPGRAM.value: DeepgramTranscriber,
}

# 録音の最大継続時間（秒）。release 取りこぼし等による永久録音を防ぐ保険
_MAX_RECORDING_SEC: float = 300.0
# PortAudio 操作がこの秒数を超えてブロックしたらハングとみなし recover する
_AUDIO_HANG_SEC: float = 5.0
# ダブルタップ（auto_enter）の判定ウィンドウ（秒）
_DOUBLE_TAP_SEC: float = 0.4
# これより短い録音は誤操作とみなして文字起こししない（秒）
_MIN_AUDIO_SEC: float = 0.3
# ホットリロードで退役したトランスクライバを close するまでの猶予（秒）
_RETIRE_CLOSE_DELAY_SEC: float = 30.0
# 修飾キーの汎用名。macOS の pynput は左修飾キーを汎用名（cmd 等）で報告する
_GENERIC_MODIFIERS = ("ctrl", "alt", "shift", "cmd")


@dataclass
class HotkeySlot:
    """
    ホットキースロットの設定とトランスクライバを保持する。

    Attributes:
        slot_id: スロット ID（1 または 2）
        hotkey: ホットキー文字列（例: "<cmd_r>", "<ctrl>+<space>"）
        hotkey_mode: 動作モード（hold/toggle）
        required_keys: パース済みのキートークン集合
        backend: 使用するバックエンド（groq/openai）
        api_model: API モデル名
        api_prompt: API プロンプト
        format_enabled: 貼り付け前に LLM テキスト整形を行うか
        transcriber: このスロット用のトランスクライバ
    """
    slot_id: int
    hotkey: str
    hotkey_mode: str
    required_keys: Set[str]
    backend: str
    api_model: str
    api_prompt: str
    format_enabled: bool
    transcriber: ApiTranscriber


class VoicekeyApp(QObject):
    """
    メインアプリケーションコントローラー。

    Signals:
        status_changed: UI 状態の変更通知（"idle"/"recording"/"recording_auto_enter"/"transcribing"）
        audio_level: 録音中の音声レベル（0.0-1.0、約 30fps）。HUD のレベルメーター用
        notice: ユーザーに見せるべき一時メッセージ（エラー、無音検出など）
        interim_text: ストリーミング中のライブ字幕（受信スレッド → メインへホップ）
    """

    status_changed = Signal(str)
    audio_level = Signal(float)
    notice = Signal(str)
    interim_text = Signal(str)

    def __init__(self) -> None:
        """アプリケーションを初期化する。"""
        super().__init__()
        logger.info("voicekey を初期化中...")
        self._platform = get_platform_adapter()
        self._config = ConfigManager()

        # --- コアコンポーネント ---
        self._recorder = AudioRecorder(
            input_device=self._config.get("audio_input_device", "default"),
            level_callback=self._on_audio_level,
        )
        self._current_input_device = AudioRecorder.normalize_device_setting(
            self._config.get("audio_input_device", "default")
        )
        self._input_handler = InputHandler(platform_adapter=self._platform)
        self._vad = SileroVad()

        # --- 内部状態（_state_lock で保護） ---
        self._state_lock = threading.Lock()
        self._recording_slot: Optional[int] = None  # 録音中のスロット ID
        self._recording_started: float = 0.0        # 録音開始時刻（monotonic）
        self._auto_enter = False                    # ダブルタップによる auto_enter 録音か
        self._outstanding = 0                       # 未完了の文字起こしタスク数
        # ストリーミング録音中の Deepgram セッション（非ストリーミング時は None）
        self._active_streamer: Optional[StreamingTranscriber] = None

        # ダブルタップ検出（hold モードのみ。listener スレッドからのみ更新）
        self._last_release_time: float = 0.0
        self._last_release_slot: Optional[int] = None
        # 短いタップの離鍵後、2 打目を待つ間の遅延停止タイマー（_state_lock で保護）。
        # 1 打目で録音を止めない（タップと同時に話し始めた声の冒頭を失わない）ための仕組み
        self._pending_tap_timer: Optional[threading.Timer] = None

        # 現在押されているキー（listener スレッドからのみ更新）
        self._pressed_keys: Set[str] = set()

        # --- ホットキースロット ---
        self._slots: Dict[int, HotkeySlot] = self._build_slots()

        # --- 文字起こしワーカー（常駐 1 本、None センチネルで終了） ---
        self._task_q: "queue.Queue[Optional[TranscriptionTask]]" = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="Transcription"
        )
        self._worker.start()

        # --- UI ---
        self._settings_window = SettingsWindow(platform_adapter=self._platform)
        self._tray = SystemTray(platform_adapter=self._platform)
        self._tray.open_settings.connect(self._open_settings)
        self._tray.force_reset.connect(self._force_restart)
        self._tray.quit_app.connect(self._quit_app)
        self.status_changed.connect(self._tray.set_status)

        # 録音中 HUD（画面下部中央の小型ピル）。シグナルはワーカー/音声スレッドから
        # 発火するが、Qt のキュー接続でメインスレッド上の HUD へ安全にホップする
        self._hud = Hud(enabled=bool(self._config.get("hud_enabled", True)))
        self.status_changed.connect(self._hud.set_state)
        self.audio_level.connect(self._hud.push_level)
        self.notice.connect(self._hud.show_notice)
        self.interim_text.connect(self._hud.set_caption)

        # --- 背景スレッド ---
        self._monitoring = True
        self._listener: Optional[keyboard.Listener] = None
        self._listener_thread = self._spawn_listener_thread()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="ConfigWatchdog"
        )
        self._monitor_thread.start()

        # VAD モデルを事前ロード（初回録音時の数十 ms 遅延を回避）
        threading.Thread(target=self._vad.preload, daemon=True).start()

        # macOS の権限不足は「ホットキーが無言で効かない」となるため起動時に明示
        self._check_permissions()

        logger.info("アプリケーション準備完了")
        self._emit_state()

    # ------------------------------------------------------------------
    # ホットキースロット構築
    # ------------------------------------------------------------------

    def _build_slots(self) -> Dict[int, HotkeySlot]:
        """設定からホットキースロット（トランスクライバ込み）を構築する。"""
        slots: Dict[int, HotkeySlot] = {}
        language = self._config.get("language", "ja")
        defaults = self._config.get("default_api_models", {}) or {}

        for slot_id in (1, 2):
            cfg = self._config.get(f"hotkey{slot_id}", {}) or {}
            hotkey = cfg.get("hotkey", f"<f{slot_id + 1}>")
            mode = cfg.get("hotkey_mode", HotkeyMode.HOLD.value)
            backend = cfg.get("backend", "openai")
            if backend not in _BACKEND_CLASSES:
                logger.warning(f"未対応バックエンド '{backend}' のため openai にフォールバックします")
                backend = TranscriptionBackend.OPENAI.value
            model = cfg.get("api_model", "") or defaults.get(backend, "")
            prompt = cfg.get("api_prompt", "")

            cls = _BACKEND_CLASSES[backend]
            transcriber = cls(model=model, language=language, prompt=prompt)
            if not transcriber.is_available():
                logger.warning(
                    f"ホットキー{slot_id}: {transcriber.display_name} の API キーが未設定です"
                    "（録音しても文字起こしはエラーになります）"
                )

            slots[slot_id] = HotkeySlot(
                slot_id=slot_id,
                hotkey=hotkey,
                hotkey_mode=mode,
                required_keys=self._parse_hotkey(hotkey),
                backend=backend,
                api_model=model,
                api_prompt=prompt,
                format_enabled=bool(cfg.get("format_enabled", False)),
                transcriber=transcriber,
            )
            logger.info(f"ホットキー{slot_id}: {hotkey} ({mode}) -> {backend}/{model}")
        return slots

    @staticmethod
    def _parse_hotkey(hotkey_str: str) -> Set[str]:
        """ホットキー文字列（"<cmd_r>" / "<ctrl>+<space>" 等）をトークン集合にする。"""
        tokens = hotkey_str.replace("<", "").replace(">", "").split("+")
        return {t.strip() for t in tokens if t.strip()}

    # ------------------------------------------------------------------
    # キーマッチング
    # ------------------------------------------------------------------

    @staticmethod
    def _acceptable_names(token: str) -> Set[str]:
        """設定トークンに対して「押された」とみなせる pynput キー名の集合を返す。

        macOS の pynput は左修飾キーを汎用名（'cmd' 等）、右を 'cmd_r' で報告する。
        旧実装は 'cmd_l' 設定が汎用名 'cmd' の押下と一致せず、左修飾キーの
        ホットキーが永久に反応しなかった。

        - 汎用指定（cmd）: cmd / cmd_l / cmd_r のいずれでも一致
        - 左指定（cmd_l）: cmd_l と汎用名 cmd（macOS の左キー報告）で一致
        - 右指定（cmd_r）: cmd_r のみ
        """
        if token in _GENERIC_MODIFIERS:
            return {token, f"{token}_l", f"{token}_r"}
        if token.endswith("_l") and token[:-2] in _GENERIC_MODIFIERS:
            return {token, token[:-2]}
        return {token}

    def _slot_matches(self, slot: HotkeySlot) -> bool:
        """スロットの必要キーがすべて押されているか判定する。"""
        return all(
            self._acceptable_names(t) & self._pressed_keys
            for t in slot.required_keys
        )

    def _key_in_slot(self, key_str: str, slot: HotkeySlot) -> bool:
        """解放されたキーがスロットのホットキー構成キーか判定する。"""
        return any(key_str in self._acceptable_names(t) for t in slot.required_keys)

    # ------------------------------------------------------------------
    # キーボードリスナー（ハンドラはブロック厳禁）
    # ------------------------------------------------------------------

    def _spawn_listener_thread(self) -> threading.Thread:
        """リスナーループのスレッドを生成・起動する。"""
        thread = threading.Thread(
            target=self._listener_loop, daemon=True, name="HotkeyListener"
        )
        thread.start()
        return thread

    def _listener_loop(self) -> None:
        """キーボードリスナーを保持し、停止しても自動再起動するループ。

        スロット設定はハンドラがイベント時に self._slots を読むため、
        ホットリロードでもリスナーの再起動は不要。
        """
        while self._monitoring:
            try:
                listener = keyboard.Listener(
                    on_press=self._on_press, on_release=self._on_release
                )
                self._listener = listener
                with listener:
                    listener.join()
            except Exception as e:
                logger.error(f"キーボードリスナーが停止しました（{e!r}）。再起動します")
            finally:
                self._listener = None
                # 取りこぼした on_release によるキー状態の持ち越しを防ぐ
                self._pressed_keys.clear()
            if not self._monitoring:
                break
            time.sleep(0.5)  # busy-loop 防止

    def _on_press(self, key) -> None:
        """キー押下イベント（CGEventTap コールバックスレッド上、ブロック厳禁）。"""
        try:
            key_str = self._platform.normalize_listener_key(key)
            if key_str is None:
                return
            if key_str in self._pressed_keys:
                return  # OS のキーリピートによる再送（エッジ検出）
            self._pressed_keys.add(key_str)

            recording_slot = self._recording_slot
            if recording_slot is None:
                for slot_id, slot in self._slots.items():
                    if self._slot_matches(slot):
                        # ダブルタップ: 同スロットを短時間内に再押下 → auto_enter
                        now = time.monotonic()
                        auto_enter = (
                            self._last_release_slot == slot_id
                            and now - self._last_release_time < _DOUBLE_TAP_SEC
                        )
                        self._begin_recording(slot_id, auto_enter)
                        break
            else:
                slot = self._slots.get(recording_slot)
                if slot is None or not self._slot_matches(slot):
                    return
                # toggle モード: 録音中の再押下で停止
                if slot.hotkey_mode == HotkeyMode.TOGGLE.value:
                    self._finish_recording()
                # hold モード: 短いタップ後の待機中に再押下されたらダブルタップ確定。
                # 録音は 1 打目から止めていないため、タップ中・タップ間の音声も残っている
                elif slot.hotkey_mode == HotkeyMode.HOLD.value:
                    with self._state_lock:
                        timer = self._pending_tap_timer
                        if timer is None:
                            return
                        timer.cancel()
                        self._pending_tap_timer = None
                        self._auto_enter = True
                    logger.info(
                        f"ダブルタップ検出: 録音を継続して auto_enter を有効化 (スロット{recording_slot})"
                    )
                    self._emit_state()
        except Exception as e:
            # ハンドラ内例外でリスナーを殺さない
            logger.exception(f"キー押下処理で例外: {e}")

    def _on_release(self, key) -> None:
        """キー解放イベント（CGEventTap コールバックスレッド上、ブロック厳禁）。"""
        try:
            key_str = self._platform.normalize_listener_key(key)
            if key_str is not None:
                self._pressed_keys.discard(key_str)

            recording_slot = self._recording_slot
            if recording_slot is None:
                return
            slot = self._slots.get(recording_slot)
            if slot is None or slot.hotkey_mode != HotkeyMode.HOLD.value:
                return

            if key_str is not None and self._key_in_slot(key_str, slot):
                # ダブルタップ検出用にリリース時刻を記録してから停止
                now = time.monotonic()
                self._last_release_time = now
                self._last_release_slot = recording_slot
                if now - self._recording_started < _DOUBLE_TAP_SEC:
                    # 短いタップ＝ダブルタップの 1 打目の可能性。録音を止めずに 2 打目を待つ
                    # （タップと同時に話し始めた声を失わない。2 打目が来なければ通常どおり確定。
                    #  誤タップで発話が無い場合は通知を出さず静かに捨てる）
                    with self._state_lock:
                        if self._pending_tap_timer is not None:
                            self._pending_tap_timer.cancel()
                        timer = threading.Timer(
                            _DOUBLE_TAP_SEC,
                            self._finish_recording,
                            kwargs={"quiet_if_no_speech": True},
                        )
                        timer.daemon = True
                        self._pending_tap_timer = timer
                    timer.start()
                else:
                    self._finish_recording()
            elif key_str is None and not self._pressed_keys:
                # 正規化失敗でキー状態が消失した場合の保険（永久録音防止）
                logger.warning("キー状態の消失を検出したため録音を停止します")
                self._finish_recording()
        except Exception as e:
            logger.exception(f"キー解放処理で例外: {e}")

    # ------------------------------------------------------------------
    # 録音制御（すべてノンブロッキング）
    # ------------------------------------------------------------------

    def _begin_recording(self, slot_id: int, auto_enter: bool) -> None:
        """録音を開始する（listener スレッドから呼ばれる。即座に返る）。"""
        with self._state_lock:
            if self._recording_slot is not None:
                return
            self._recording_slot = slot_id
            self._recording_started = time.monotonic()
            self._auto_enter = auto_enter

        slot = self._slots[slot_id]
        logger.info(
            f"録音開始要求 (スロット{slot_id}, backend={slot.backend}"
            + (", auto_enter" if auto_enter else "") + ")"
        )
        self._emit_state()
        # 録音中に API への TLS 接続を事前確立し、停止後の初回往復を短縮する
        threading.Thread(target=slot.transcriber.prewarm, daemon=True).start()
        # 整形が有効なら整形 LLM への接続も録音中に温めておく
        if slot.format_enabled:
            threading.Thread(target=text_formatter.prewarm, daemon=True).start()

        # Deepgram かつストリーミング有効ならライブ字幕用に WebSocket を開く。
        # 開始できなければ（キー無し / websockets 未導入）chunk_callback を張らないため
        # REST 経路（slot.transcriber = DeepgramTranscriber）に自動フォールバックする
        if (
            self._config.get("streaming_enabled", True)
            and slot.backend == TranscriptionBackend.DEEPGRAM.value
        ):
            stream = StreamingTranscriber(
                model=slot.api_model,
                language=self._config.get("language", "ja"),
                on_interim=self.interim_text.emit,  # 受信スレッド → シグナルでメインへ
            )
            if stream.start():
                with self._state_lock:
                    self._active_streamer = stream
                # 録音中のみ発火するフック。次の停止で解除する
                self._recorder.chunk_callback = stream.send

        self._recorder.start_async(self._on_record_started)

    def _on_record_started(self, ok: bool) -> None:
        """録音開始の結果（AudioControl スレッド上で呼ばれる）。"""
        if ok:
            return
        logger.warning("録音を開始できませんでした")
        with self._state_lock:
            self._recording_slot = None
        self.notice.emit("録音を開始できませんでした（マイクを確認してください）")
        self._emit_state()

    def _finish_recording(self, quiet_if_no_speech: bool = False) -> None:
        """録音を停止し文字起こしタスクを積む（即座に返る）。

        Args:
            quiet_if_no_speech: 無音時の「音声が検出されませんでした」通知を抑制する
                （ダブルタップ待ち期限切れ＝誤タップの可能性が高い経路で使う）
        """
        with self._state_lock:
            slot_id = self._recording_slot
            if slot_id is None:
                return
            auto_enter = self._auto_enter
            streamer = self._active_streamer  # ストリーミング録音なら確定待ちをワーカーへ託す
            self._recording_slot = None
            self._auto_enter = False
            self._active_streamer = None
            self._outstanding += 1  # 音声確定前から「変換中」を表示する
            # ダブルタップ待ちが残っていれば破棄（failsafe 等の別経路からの停止に備える）
            if self._pending_tap_timer is not None:
                self._pending_tap_timer.cancel()
                self._pending_tap_timer = None

        # ストリーミング送出を停止（確定は finish() がワーカー上で行う）
        self._recorder.chunk_callback = None

        logger.info(f"録音停止要求 (スロット{slot_id})")
        self._emit_state()

        def _on_audio(audio) -> None:
            # AudioControl スレッド上。キュー投函のみ（重い処理はワーカーで）
            self._task_q.put(
                TranscriptionTask(
                    audio_data=audio,
                    slot_id=slot_id,
                    timestamp=time.perf_counter(),
                    auto_enter=auto_enter,
                    streamer=streamer,
                    quiet_if_no_speech=quiet_if_no_speech,
                )
            )

        self._recorder.stop_async(_on_audio)

    def _on_audio_level(self, level: float) -> None:
        """録音中の音声レベル（audio callback スレッドから約 30fps で呼ばれる）。"""
        self.audio_level.emit(level)

    # ------------------------------------------------------------------
    # 文字起こしワーカー
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """文字起こしタスクを直列処理する常駐ワーカー。"""
        while True:
            task = self._task_q.get()
            if task is None:  # 終了センチネル
                return
            try:
                self._process_task(task)
            except TranscriptionError as e:
                logger.error(f"文字起こし失敗: {e}")
                self.notice.emit(str(e))
            except Exception as e:
                logger.exception(f"文字起こしタスクで予期しない例外: {e}")
                self.notice.emit("文字起こし中にエラーが発生しました（ログを確認してください）")
            finally:
                with self._state_lock:
                    self._outstanding -= 1
                self._emit_state()

    def _process_task(self, task: TranscriptionTask) -> None:
        """ストリーミング確定 →（空なら）正規化 → VAD → API → テキスト挿入を実行する。"""
        # --- ストリーミング経路: Deepgram の確定テキストを受け取って貼り付け ---
        streamer = task.streamer
        if streamer is not None:
            try:
                streamed = streamer.finish()
            except Exception as e:
                logger.warning(f"ストリーミング確定でエラー、REST にフォールバック: {e}")
                streamed = ""
            if streamed:
                total_ms = (time.perf_counter() - task.timestamp) * 1000
                logger.info(f"ストリーミング確定: {len(streamed)} 文字 ({total_ms:.0f}ms)")
                # 貼り付け前の LLM テキスト整形（失敗時は原文のまま）
                streamed = self._maybe_format(streamed, task.slot_id)
                self._insert_and_enter(streamed, task.auto_enter)
                return
            # 確定が空（接続失敗・無音など）→ 取得済みバッファで REST にフォールバック
            logger.info("ストリーミング結果が空のため REST にフォールバックします")

        # --- REST 経路（従来の 正規化 → VAD → API → 貼り付け）---
        audio = task.audio_data
        duration = len(audio) / SAMPLE_RATE
        if duration < _MIN_AUDIO_SEC:
            logger.info(f"録音が短すぎるためスキップ ({duration:.2f}s)")
            return

        slot = self._slots.get(task.slot_id)
        if slot is None:
            return

        # 音量正規化（ゲイン上限 +20dB。ノイズフロアの過剰増幅 = 幻覚を防ぐ）
        preprocess_cfg = self._config.get("audio_preprocess", {}) or {}
        if bool(preprocess_cfg.get("volume_normalize", True)):
            try:
                audio = preprocess_audio(audio, sample_rate=SAMPLE_RATE)
            except Exception as e:
                logger.warning(f"音声前処理でエラー、原音を使用: {e}")

        # VAD: 発話がなければ API に送らない（幻覚と無駄コストの防止）。
        # 推論 1 回で発話判定と無音圧縮（前後トリミング + 発話間の長い無音の短縮）を行う
        if self._config.get("vad_filter", True):
            vad_start = time.perf_counter()
            has_speech, condensed = self._vad.analyze(audio)
            if not has_speech:
                logger.info("発話が検出されなかったためスキップ")
                # 誤タップ由来（ダブルタップ待ちの期限切れ）の無音は通知しない
                if not task.quiet_if_no_speech:
                    self.notice.emit("音声が検出されませんでした")
                return
            if condensed is not None:
                cut_sec = (len(audio) - len(condensed)) / SAMPLE_RATE
                if cut_sec > 0.1:
                    logger.info(f"無音圧縮: {cut_sec:.1f}s 削減")
                audio = condensed
            logger.info(f"VAD 処理: {(time.perf_counter() - vad_start) * 1000:.0f}ms")

        text = slot.transcriber.transcribe(audio)
        if not text:
            logger.info("文字起こし結果が空でした")
            return

        total_ms = (time.perf_counter() - task.timestamp) * 1000
        logger.info(f"文字起こし完了: 音声 {duration:.1f}s → {len(text)} 文字 ({total_ms:.0f}ms)")

        # 貼り付け前の LLM テキスト整形（失敗時は原文のまま）
        text = self._maybe_format(text, task.slot_id)

        # テキスト挿入（ワーカースレッド上で実行し UI スレッドを塞がない）
        self._insert_and_enter(text, task.auto_enter)

    def _maybe_format(self, text: str, slot_id: int) -> str:
        """
        スロットで整形が有効なら LLM テキスト整形を適用する（ワーカースレッド上）。

        format_text は失敗時に必ず原文を返すため、ここでは例外処理は不要。

        Args:
            text: 文字起こし確定テキスト
            slot_id: 使用したホットキースロット ID

        Returns:
            整形後テキスト。整形が無効・失敗時は原文そのまま
        """
        slot = self._slots.get(slot_id)
        if slot is None or not slot.format_enabled:
            return text
        return text_formatter.format_text(
            text,
            self._config.get("format_model", "llama-3.1-8b-instant"),
            prompt=self._config.get("format_auto_prompt", ""),
        )

    def _insert_and_enter(self, text: str, auto_enter: bool) -> None:
        """テキストを貼り付け、ダブルタップ時は遅延後に Enter を送る（ワーカースレッド上）。"""
        self._input_handler.insert_text(text)
        if auto_enter:
            delay_ms = self._config.get("auto_enter_delay_ms", 50)
            time.sleep(max(0, delay_ms) / 1000.0)
            self._input_handler.press_enter()
            logger.info("auto_enter: Enter を送信しました")

    # ------------------------------------------------------------------
    # 状態通知（唯一の発信点）
    # ------------------------------------------------------------------

    def _emit_state(self) -> None:
        """内部状態から UI 状態を計算して通知する。"""
        with self._state_lock:
            if self._recording_slot is not None:
                state = "recording_auto_enter" if self._auto_enter else "recording"
            elif self._outstanding > 0:
                state = "transcribing"
            else:
                state = "idle"
        self.status_changed.emit(state)

    # ------------------------------------------------------------------
    # 設定監視 + ウォッチドッグ
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        """設定ファイルの変更監視と健全性チェックを行うループ。"""
        while self._monitoring:
            time.sleep(CONFIG_CHECK_INTERVAL_SEC)
            try:
                if self._config.reload_if_changed():
                    self._apply_config_changes()
                self._watchdog_check()
            except Exception as e:
                logger.exception(f"監視スレッドで例外: {e}")

    def _apply_config_changes(self) -> None:
        """設定変更を適用する（リスナー再起動は不要な設計）。"""
        # 入力デバイス
        device = AudioRecorder.normalize_device_setting(
            self._config.get("audio_input_device", "default")
        )
        if device != self._current_input_device:
            self._recorder.set_input_device(device)
            self._current_input_device = device

        # スロットを常に再構築（生成は軽量。ネットワークアクセスなし）
        old_slots = self._slots
        self._slots = self._build_slots()

        # 旧トランスクライバは処理中タスクが使っている可能性があるため遅延 close
        for old in old_slots.values():
            timer = threading.Timer(_RETIRE_CLOSE_DELAY_SEC, old.transcriber.close)
            timer.daemon = True
            timer.start()

        # HUD 表示の有効/無効を反映
        self._hud.enabled = bool(self._config.get("hud_enabled", True))

        logger.info("設定を再読み込みして適用しました")

    def _watchdog_check(self) -> None:
        """録音時間上限・PortAudio ハング・リスナー死活を確認する。"""
        # 録音時間の上限（release 取りこぼし等の保険）
        with self._state_lock:
            recording = self._recording_slot is not None
            elapsed = time.monotonic() - self._recording_started if recording else 0.0
        if recording and elapsed > _MAX_RECORDING_SEC:
            logger.warning(f"録音が {_MAX_RECORDING_SEC:.0f}s を超えたため自動停止します")
            self.notice.emit("録音時間の上限に達したため自動停止しました")
            self._finish_recording()

        # PortAudio ハング検知 → 制御スレッドを作り直して自動復旧
        health = self._recorder.health()
        if (health["busy_op"] and health["busy_sec"] > _AUDIO_HANG_SEC) or not health["alive"]:
            logger.warning(f"音声処理のハングを検知: {health}")
            self.notice.emit("マイク処理の応答がないため自動復旧しました")
            with self._state_lock:
                self._recording_slot = None
                streamer = self._active_streamer
                self._active_streamer = None
            # ハング時もストリーミング接続を確実に破棄する
            self._recorder.chunk_callback = None
            if streamer is not None:
                streamer.cancel()
            self._recorder.recover()
            self._emit_state()

        # リスナースレッドの死活確認
        if not self._listener_thread.is_alive() and self._monitoring:
            logger.error("ホットキーリスナースレッドが停止していたため再起動します")
            self._listener_thread = self._spawn_listener_thread()

    # ------------------------------------------------------------------
    # 権限チェック（macOS）
    # ------------------------------------------------------------------

    def _check_permissions(self) -> None:
        """入力系権限が不足していればダイアログで案内する。"""
        perms = self._platform.check_input_permissions()
        missing = [name for name, ok in perms.items() if not ok]
        if not missing:
            return

        labels = {"accessibility": "アクセシビリティ", "input_monitoring": "入力監視"}
        names = "・".join(labels.get(m, m) for m in missing)
        logger.warning(f"OS 権限が不足しています: {names}")

        box = QMessageBox()
        box.setWindowTitle("voicekey - 権限が必要です")
        box.setText(
            f"voicekey の動作には「{names}」権限が必要です。\n\n"
            "システム設定でこのアプリ（ターミナル / Python）を許可し、\n"
            "voicekey を再起動してください。許可されるまでホットキーは反応しません。"
        )
        open_button = box.addButton("システム設定を開く", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("後で", QMessageBox.ButtonRole.RejectRole)
        self._platform.bring_to_front(box)
        box.exec()
        if box.clickedButton() is open_button:
            self._platform.open_permission_settings(missing[0])

    # ------------------------------------------------------------------
    # UI アクション
    # ------------------------------------------------------------------

    def _open_settings(self) -> None:
        """設定ウィンドウを開いて確実に前面化する。"""
        win = self._settings_window
        if win.isMinimized():
            win.showNormal()
        else:
            win.show()
        win.raise_()
        win.activateWindow()
        self._platform.bring_to_front(win)

    def _force_restart(self) -> None:
        """強制リセット: 新プロセスを起動して自分は即終了する（トレイメニュー用）。

        recover() による自動復旧で通常は不要だが、leak したストリームが
        OS のマイクインジケーターを掴み続けた場合などの最終脱出口として残す。
        プロセス終了で OS が確実にマイクハンドルを回収する。
        """
        logger.warning("強制リセット: 新プロセスを起動して終了します")
        listener = self._listener
        if listener is not None:
            try:
                listener.stop()
            except Exception as e:
                logger.warning(f"リスナー停止失敗（無視して再起動）: {e}")
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        try:
            # 新セッションで独立起動し、自分の終了に巻き込まない
            subprocess.Popen([sys.executable] + sys.argv, start_new_session=True, cwd=os.getcwd())
        except Exception as e:
            logger.error(f"新プロセス起動失敗、再起動を中止: {e}")
            return
        os._exit(0)

    def _quit_app(self) -> None:
        """アプリケーションを終了する（最大 1 秒でマイクを解放）。"""
        logger.info("終了処理を開始します")
        self._monitoring = False

        # 録音中のストリーミング接続を破棄してマイク/WebSocket を確実に解放する
        with self._state_lock:
            streamer = self._active_streamer
            self._active_streamer = None
        self._recorder.chunk_callback = None
        if streamer is not None:
            streamer.cancel()

        listener = self._listener
        if listener is not None:
            try:
                listener.stop()
            except Exception as e:
                logger.warning(f"キーボードリスナー停止失敗: {e}")

        self._task_q.put(None)  # ワーカー停止
        # ストリームを閉じてマイクを OS に返す（ハング時も 1 秒で諦める。
        # プロセス終了時に OS がハンドルを回収するため実害はない）
        self._recorder.shutdown(timeout=1.0)
        QApplication.quit()
