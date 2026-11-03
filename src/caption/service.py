"""
ライブ字幕の統括（Mac 版 CaptionService.swift の Windows 版）

キャプチャ（WASAPI ループバック）→ 認識（Soniox / OpenAI ライブ）→ 翻訳（Soniox / Groq / Gemini）→
表示・議事録 を 1 か所で結線し、状態（停止中・準備中・キャプチャ中・エラー）を画面とトレイへ配る。

- **ディクテーションのクリティカルパスに 1ms も足さない**: このサービスは字幕を開始するまで作られない
  （app.py が遅延生成する）。既存の録音〜貼り付け経路には手を入れない。
- **エンジンの切替はユーザー操作のみ**: Soniox が使えなくなっても勝手に別の課金エンジンへ切り替えない。
  諦めた理由（残高不足・キー無効）を状態として出して止まる。
- **クラウド（Groq / Gemini）へ送るのは確定文だけ**（TranslationCoordinator がまとめた区切り）。
- 議事録に保存するのは文字起こしだけ（訳文は保存しない）。保存スイッチ（caption.save_transcript）は字幕の保存だけに効く。

スレッド: 認識・翻訳のコールバックは各スレッドから来るので、`_ui_event` シグナルでメインスレッドへ渡してから
表示を触る。セッションには世代番号を振り、停止・張り直しの後に遅れて届いた結果は表示しない
（議事録には世代で縛らずに書く＝停止の瞬間に確定した最後の文も残す）。
"""

import os
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from ..utils.logger import get_logger
from . import settings as cs
from . import soniox_assembler as sa
from .transcript_recorder import TranscriptRecorder

logger = get_logger(__name__)

# 設定変更で張り直すとき、後片付けが終わるのを待つ時間（ミリ秒）
RESTART_DELAY_MSEC = 600

STOPPED = "stopped"
PREPARING = "preparing"
RUNNING = "running"
FAILED = "failed"

# API 使用量の用途（字幕・議事録の音声の文字起こし）
PURPOSE_CAPTION_AUDIO = "caption"


def state_title(state: str, detail: str) -> str:
    """状態の 1 行（Mac の CaptionRunState.menuTitle と同じ文言）。"""
    if state == STOPPED:
        return "停止中"
    if state == PREPARING:
        return f"準備中 — {detail}"
    if state == RUNNING:
        return "キャプチャ中"
    return f"エラー — {detail}"


def is_disabled_by_environment() -> bool:
    """字幕機能を丸ごと無効化する（VOICEKEY_CAPTION_DISABLE=1。画素判定のハーネス用）。"""
    return os.environ.get("VOICEKEY_CAPTION_DISABLE") == "1"


class CaptionService(QObject):
    """字幕の開始・停止と各部品の結線。

    Signals:
        changed: 状態・接続状態・対象アプリなどが変わった（トレイ・設定画面を描き直す）
        visibility_changed: 字幕が出た / 畳み終わった（待機ピルの出し入れ）
        model_replaced: 字幕の翻訳モデルが提供終了で差し替わった (設定キー, 新モデル)
    """

    changed = Signal()
    # 字幕が止まった（残高不足・キー無効・取り込めない など。ユーザー向けの理由）
    failed = Signal(str)
    visibility_changed = Signal(bool)
    model_replaced = Signal(str, str)
    # 内部: 別スレッド → メインスレッド (世代, 種類, a, b)
    _ui_event = Signal(int, str, str, str)

    def __init__(self, config_manager, hud_factory: Optional[Callable] = None,
                 stream_factory: Optional[Callable] = None, capture_factory: Optional[Callable] = None,
                 recorder: Optional[TranscriptRecorder] = None) -> None:
        """
        Args:
            config_manager: 設定（ConfigManager か、get/save を持つもの）
            hud_factory / stream_factory / capture_factory: テストで差し替える部品の作り方
            recorder: 議事録の記録係（テストで保存先を差し替える）
        """
        super().__init__()
        self._config = config_manager
        self._hud_factory = hud_factory
        self._stream_factory = stream_factory
        self._capture_factory = capture_factory
        self._recorder = recorder or TranscriptRecorder()
        self._hud = None
        self._generation = 0
        self._stream = None
        self._capture = None
        self._coordinator = None
        self._translators: dict = {}
        self._session_key: Optional[tuple] = None
        self._dictation_active = False
        self._pending_restart: Optional[QTimer] = None
        self._translation_error_notified = False
        self.state = STOPPED
        self.detail = ""
        self.link_state: Optional[str] = None
        self.link_provider = ""
        self.give_up_reason: Optional[str] = None
        self.translation_error: Optional[str] = None
        self.target_name: Optional[str] = None
        self._ui_event.connect(self._on_ui_event)

    # --- 設定 ---

    def _settings(self) -> cs.CaptionConfig:
        return cs.CaptionConfig.from_config(self._config)

    def _save(self, values: dict) -> None:
        try:
            self._config.save({"caption": values})
        except Exception as e:
            logger.warning(f"字幕の設定を保存できませんでした: {e}")

    # --- 状態 ---

    @property
    def is_active(self) -> bool:
        return self.state in (PREPARING, RUNNING)

    @property
    def title(self) -> str:
        return state_title(self.state, self.detail)

    def status_lines(self) -> list:
        """トレイ・設定画面に出す補足の行（接続状態・対象・失敗理由）。"""
        lines = []
        if self.is_active and self.link_state:
            lines.append(sa.link_state_text(self.link_state, self.link_provider))
        if self.is_active and self.target_name:
            lines.append(f"対象: {self.target_name}")
        if self.is_active and self.translation_error:
            lines.append(f"翻訳: {self.translation_error}（原文のまま表示中）")
        return lines

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        self.changed.emit()
        if state == FAILED:
            self.failed.emit(detail)

    # --- 外から使う口（メインスレッド） ---

    def start(self) -> None:
        """字幕を開始する。"""
        if self.is_active:
            return
        if is_disabled_by_environment():
            logger.info("VOICEKEY_CAPTION_DISABLE=1 のため字幕を開始しません")
            return
        logger.info(f"ライブ字幕を開始 (mode={self._settings().mode})")
        self._cancel_restart()
        self.give_up_reason = None
        self.translation_error = None
        self._translation_error_notified = False
        # 一度でも開始したら、次回起動からは設定に従って自動開始してよい
        if not self._settings().ever_started:
            self._save({"ever_started": True})
        self._set_state(PREPARING, "音声の取り込みを準備中…")
        self._start_session()

    def stop(self) -> None:
        """字幕を停止する。"""
        if not self.is_active:
            return
        logger.info("ライブ字幕を停止")
        self._cancel_restart()
        self._teardown()
        self._set_state(STOPPED)

    def toggle(self) -> None:
        if self.is_active:
            self.stop()
        else:
            self.start()

    def shutdown(self) -> None:
        """アプリ終了時の後片付け（音声の取り込みと接続を確実に閉じ、議事録を書き切る）。"""
        self._cancel_restart()
        self._generation += 1
        capture, stream = self._capture, self._stream
        self._capture = self._stream = None
        if capture is not None:
            capture.stop()
        if stream is not None:
            stream.stop(timeout=3.0)
        if self._coordinator is not None:
            self._coordinator.close()
            self._coordinator = None
        self._recorder.end_session()
        self._recorder.wait_until_written()
        if self._hud is not None:
            self._hud.clear()
        self.visibility_changed.emit(False)

    def apply_config(self) -> None:
        """設定が保存された。表示の設定はその場で、張り直しが要る設定は動作中なら張り直す。"""
        cfg = self._settings()
        if self._hud is not None:
            self._hud.set_scale(cfg.hud_scale)
            self._hud.set_shows_source(cfg.shows_source)
        if self.is_active and self._session_key is not None and self._session_key != cfg.session_key():
            self._restart()
        self.changed.emit()

    def set_hud_scale(self, scale: float) -> None:
        """字幕の大きさを見ながら合わせる（保存は設定画面の保存で）。"""
        if self._hud is not None:
            self._hud.set_scale(scale)

    def set_dictation_active(self, active: bool) -> None:
        """音声入力中（録音・変換中・通知）は字幕を隠す。認識・翻訳は止めないので、戻れば続きから出る。"""
        self._dictation_active = bool(active)
        if self._hud is not None:
            self._hud.set_suppressed(self._dictation_active)

    @property
    def recorder(self) -> TranscriptRecorder:
        return self._recorder

    # --- セッション ---

    def _ensure_hud(self):
        if self._hud is None:
            cfg = self._settings()
            if self._hud_factory is not None:
                self._hud = self._hud_factory()
            else:
                from .caption_hud import CaptionHud

                self._hud = CaptionHud(cfg.hud_scale, cfg.shows_source)
            self._hud.visibility_changed.connect(self.visibility_changed.emit)
            self._hud.set_suppressed(self._dictation_active)
        return self._hud

    def _make_stream(self, cfg: cs.CaptionConfig):
        if self._stream_factory is not None:
            return self._stream_factory(cfg)
        from .live_stream import make_stream

        return make_stream(cfg.effective_recognizer, cfg.uses_soniox_translation, cfg.recognition_language)

    def _make_capture(self, cfg: cs.CaptionConfig, on_audio):
        if self._capture_factory is not None:
            return self._capture_factory(cfg, on_audio)
        from .audio_capture import CaptureController

        return CaptureController(on_audio=on_audio, frontmost=cfg.scope == cs.SCOPE_FRONTMOST)

    def _start_session(self) -> None:
        self._generation += 1
        gen = self._generation
        cfg = self._settings()
        self._session_key = cfg.session_key()
        hud = self._ensure_hud()
        hud.live_looks_confirmed = cfg.mode == cs.MODE_TRANSCRIBE
        hud.set_scale(cfg.hud_scale)
        hud.set_shows_source(cfg.shows_source)
        self.link_state = None
        self.target_name = None
        emit = lambda kind, a="", b="": self._ui_event.emit(gen, kind, a, b)  # noqa: E731

        stream = self._make_stream(cfg)
        self.link_provider = getattr(stream, "PROVIDER", "")
        translator = cfg.translator
        coordinator = None
        if translator is not None:
            from .translation_coordinator import TranslationCoordinator

            coordinator = TranslationCoordinator(translator_provider=self._current_translator)
            coordinator.on_source_ready = lambda source: emit("pending", source)
            coordinator.on_streaming_delta = lambda source, so_far, _seq: emit("delta", source, so_far)
            coordinator.on_translated = lambda source, japanese, _seq: emit("translated", source, japanese)
            coordinator.on_failed = lambda source, message: emit("failed", source, message)
        self._coordinator = coordinator

        def on_confirmed(source: str, translation: str) -> None:
            # 議事録へは確定した原文を書く（世代では縛らない＝停止時の最後の文も残す。訳文は保存しない）
            if source:
                self._save_transcript(source)
            if gen != self._generation:
                return
            if coordinator is not None:
                coordinator.submit(source)
            else:
                emit("confirmed", source, translation)

        stream.on_live = lambda source, translation: emit("live", source, translation)
        stream.on_confirmed = on_confirmed
        stream.on_give_up = lambda reason: emit("give_up", reason)
        stream.on_link_state = lambda state: emit("link", state)
        stream.on_usage = lambda seconds: self._record_usage(stream, seconds)
        if not stream.start():
            self._teardown()
            key_name = {"soniox": "Soniox", "openai": "OpenAI"}.get(getattr(stream, "KEY_ITEM", ""), "認識")
            self._set_state(FAILED, f"{key_name} の API キーが未設定です（設定 › API キー）")
            return
        self._stream = stream

        capture = self._make_capture(cfg, stream.send)
        capture.on_target_changed = lambda name: emit("target", name)
        capture.on_failed = lambda reason: emit("capture_failed", reason)
        capture.on_notice = lambda text: emit("notice", text)
        self._capture = capture

        def open_capture() -> None:
            # Process Loopback の準備は数秒かかることがあるので、UI を止めないよう裏で開く
            try:
                capture.start()
            except Exception as e:
                emit("capture_failed", str(e) or type(e).__name__)
                return
            emit("running")

        threading.Thread(target=open_capture, daemon=True, name="CaptionCaptureStart").start()

    def _teardown(self) -> None:
        """動作中のセッションを畳む（停止・張り直し共通）。

        先に世代を進めるので、この後に届く認識・翻訳の結果は表示しない。
        音声と接続は最後まで吐き切らせてから閉じる（最後の確定文を議事録に残すため）。
        """
        self._generation += 1
        ended = self._generation
        self._session_key = None
        capture, stream, coordinator = self._capture, self._stream, self._coordinator
        self._capture = self._stream = self._coordinator = None
        self.link_state = None
        self.target_name = None
        if coordinator is not None:
            coordinator.reset()
            coordinator.close()
        if self._hud is not None:
            self._hud.clear()
        # 止めた拍子に待機ピルが戻らなくなるのを塞ぐ
        self.visibility_changed.emit(False)

        def finish() -> None:
            if capture is not None:
                capture.stop()
            if stream is not None:
                stream.stop(timeout=3.0)
            # 議事録はここで閉じる（次に開始したときは別ファイル）。張り直しで新しいセッションが始まっていたら切らない
            if ended == self._generation:
                self._recorder.end_session()

        threading.Thread(target=finish, daemon=True, name="CaptionTeardown").start()

    def _restart(self) -> None:
        """設定変更で張り直す（後片付けの時間を置いてから開始する。状態は準備中のまま）。"""
        if not self.is_active:
            return
        self._teardown()
        self._set_state(PREPARING, "設定を反映中…")
        self._cancel_restart()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._run_restart)
        self._pending_restart = timer
        timer.start(RESTART_DELAY_MSEC)

    def _run_restart(self) -> None:
        self._pending_restart = None
        if self.state == PREPARING:
            self._start_session()

    def _cancel_restart(self) -> None:
        if self._pending_restart is not None:
            self._pending_restart.stop()
            self._pending_restart = None

    # --- 翻訳器 ---

    def _current_translator(self):
        """確定文の翻訳器（1 文ごとに解決する＝ Groq / Gemini の切り替えを次の文から効かせる）。"""
        cfg = self._settings()
        engine = cfg.translator
        if engine is None:
            return None
        translator = self._translators.get(engine)
        if translator is None:
            from .translators import GeminiCaptionTranslator, GroqCaptionTranslator

            if engine == cs.ENGINE_GEMINI:
                translator = GeminiCaptionTranslator(model_provider=lambda: self._settings().gemini_model)
            else:
                translator = GroqCaptionTranslator(
                    model_provider=lambda: self._settings().groq_model,
                    on_model_replaced=lambda model: self.model_replaced.emit("groq_model", model),
                )
            self._translators[engine] = translator
        return translator

    # --- 議事録・使用量 ---

    def _save_transcript(self, text: str) -> None:
        """確定文をローカルへ保存する（字幕の保存スイッチが OFF なら何もしない）。"""
        cfg = self._settings()
        if not cfg.saves_transcript:
            return
        self._recorder.record(text, context=self.target_name, language=cfg.recognition_language_name)

    @staticmethod
    def _record_usage(stream, seconds: float) -> None:
        try:
            from ..core import api_usage

            api_usage.record_audio(getattr(stream, "KEY_ITEM", ""), getattr(stream, "MODEL", ""), seconds,
                                   purpose=PURPOSE_CAPTION_AUDIO)
        except Exception:
            pass

    # --- 別スレッドからの出来事（メインスレッド） ---

    def _on_ui_event(self, gen: int, kind: str, a: str, b: str) -> None:
        if gen != self._generation:
            return
        hud = self._hud
        cfg = self._settings()
        transcribing = cfg.mode == cs.MODE_TRANSCRIBE
        if kind == "live" and hud is not None:
            if transcribing:
                hud.show_live_transcript(a)
            elif b:
                hud.show_partial(a, b)
            else:
                hud.show_listening(a)
        elif kind == "confirmed" and hud is not None:
            if transcribing:
                hud.show_transcript(a)
            elif b:
                hud.show_translation(a, b)
            elif a:
                # 訳が届かなかった（訳されない言語・時間切れ）。落とさず原文で出す
                hud.show_source_only(a)
        elif kind == "pending" and hud is not None:
            hud.show_pending_source(a)
        elif kind == "delta" and hud is not None:
            hud.show_partial(a, b)
        elif kind == "translated" and hud is not None:
            if self.translation_error:
                self.translation_error = None
                self.changed.emit()
            hud.show_translation(a, b)
        elif kind == "failed":
            if hud is not None:
                hud.show_source_only(a)
            if b != self.translation_error:
                self.translation_error = b
                self.changed.emit()
            if not self._translation_error_notified and hud is not None:
                # キー未設定などは 1 回だけ字幕の場所で知らせる（毎行は出さない）
                self._translation_error_notified = True
                hud.show_notice(f"翻訳できません: {b}")
        elif kind == "link":
            self.link_state = a
            self.changed.emit()
        elif kind == "target":
            self.target_name = a
            self.changed.emit()
        elif kind == "notice" and hud is not None:
            hud.show_notice(a)
        elif kind == "running":
            if self.state == PREPARING:
                self._set_state(RUNNING)
        elif kind in ("give_up", "capture_failed"):
            # 勝手に別のエンジンへ切り替えない。理由を出して止まる
            self.give_up_reason = a
            self._teardown()
            self._set_state(FAILED, a)
