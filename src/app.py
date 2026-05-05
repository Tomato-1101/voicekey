"""
メインアプリケーションコントローラーモジュール

音声録音、文字起こし、UI、ホットキー処理など、
すべてのコンポーネントを統合するメインコントローラー。
"""

import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Tuple, Union

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from pynput import keyboard

from .config import ConfigManager, HotkeyMode, TranscriptionBackend
from .config.constants import CONFIG_CHECK_INTERVAL_SEC, SAMPLE_RATE
from .config.types import TranscriptionTask
from .core import AudioRecorder, GroqTranscriber, InputHandler, OpenAITranscriber
from .core.audio_preprocess import preprocess as preprocess_audio
from .platform import get_platform_adapter
from .ui import SettingsWindow, SystemTray
from .utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class HotkeySlot:
    """
    ホットキースロットの状態管理クラス。

    各スロットのホットキー設定と、API使用時のTranscriberインスタンスを保持する。

    Attributes:
        slot_id: スロットID（1または2）
        hotkey: ホットキー文字列
        hotkey_mode: 動作モード（hold/toggle）
        required_keys: パース済みのキーセット
        backend: 使用するバックエンド
        api_model: APIモデル名
        api_prompt: APIプロンプト
        api_transcriber: API Transcriberインスタンス（APIバックエンドの場合のみ）
    """
    slot_id: int
    hotkey: str
    hotkey_mode: str
    required_keys: Set[str]
    backend: str
    api_model: str
    api_prompt: str
    api_transcriber: Optional[Union[GroqTranscriber, OpenAITranscriber]] = None


class SuperWhisperApp(QObject):
    """
    メインアプリケーションコントローラー。
    
    すべてのコンポーネント（音声録音、文字起こし、UI、設定、ホットキー）を
    統合し、アプリケーション全体のライフサイクルを管理する。
    
    Signals:
        status_changed: 状態変更通知（UIスレッドセーフ）
        text_ready: 文字起こし完了通知
    """
    
    # UIスレッドセーフな更新用シグナル
    status_changed = Signal(str)
    text_ready = Signal(str, bool)  # (text, auto_enter)
    
    def __init__(self) -> None:
        """アプリケーションを初期化する。"""
        super().__init__()
        logger.info("voicekeyを初期化中...")
        self._platform = get_platform_adapter()
        
        self._setup_config()
        self._setup_core_components()
        self._setup_ui_components()
        self._setup_signals()
        self._setup_state()
        self._start_background_threads()
        self._preload_models_async()
        
        logger.info("アプリケーション準備完了。")
        self.status_changed.emit("idle")

    def _setup_config(self) -> None:
        """設定マネージャーを初期化する。"""
        self._config = ConfigManager()

    def _setup_core_components(self) -> None:
        """コアビジネスロジックコンポーネントを初期化する。"""
        initial_input_device = self._config.get("audio_input_device", "default")
        self._recorder = AudioRecorder(input_device=initial_input_device)
        self._current_input_device = AudioRecorder.normalize_device_setting(initial_input_device)

        self._input_handler = InputHandler(platform_adapter=self._platform)

    def _get_transcriber_for_slot(self, slot: HotkeySlot) -> Optional[Union[GroqTranscriber, OpenAITranscriber]]:
        """
        スロットに対応するTranscriberを取得する。

        Args:
            slot: ホットキースロット

        Returns:
            API Transcriberインスタンス
        """
        return slot.api_transcriber

    def _create_api_transcriber(self, slot: HotkeySlot) -> Optional[Union[GroqTranscriber, OpenAITranscriber]]:
        """
        APIバックエンドのTranscriberを作成する。

        Args:
            slot: ホットキースロット

        Returns:
            APITranscriberインスタンス、またはNone
        """
        language = self._config.get("language", "ja")
        vad_filter = self._config.get("vad_filter", True)
        vad_min_silence = self._config.get("vad_min_silence_duration_ms", 500)

        if slot.backend == TranscriptionBackend.GROQ.value:
            transcriber = GroqTranscriber(
                model=slot.api_model,
                language=language,
                prompt=slot.api_prompt,
                vad_filter=vad_filter,
                vad_min_silence_duration_ms=vad_min_silence,
            )

            if not transcriber.is_available():
                logger.warning(
                    "Groq APIが利用できません（SDKが未インストールまたはGROQ_API_KEYが未設定）。"
                )
                self._show_backend_warning("groq_unavailable")
                return None

            logger.info(f"ホットキー{slot.slot_id}: Groq API使用 (モデル={transcriber.model})")
            return transcriber

        elif slot.backend == TranscriptionBackend.OPENAI.value:
            transcriber = OpenAITranscriber(
                model=slot.api_model,
                language=language,
                prompt=slot.api_prompt,
                vad_filter=vad_filter,
                vad_min_silence_duration_ms=vad_min_silence,
            )

            if not transcriber.is_available():
                logger.warning(
                    "OpenAI APIが利用できません（SDKが未インストールまたはOPENAI_API_KEYが未設定）。"
                )
                self._show_backend_warning("openai_unavailable")
                return None

            logger.info(f"ホットキー{slot.slot_id}: OpenAI API使用 (モデル={transcriber.model})")
            return transcriber

        return None

    def _get_common_api_settings(self) -> Tuple[str, bool, int]:
        """API Transcriber共通設定（language/VAD）を取得する。"""
        return (
            self._config.get("language", "ja"),
            self._config.get("vad_filter", True),
            self._config.get("vad_min_silence_duration_ms", 500),
        )

    def _show_backend_warning(self, warning_type: str) -> None:
        """
        バックエンドの利用不可をログとトレイのツールチップで通知する。

        以前は Dynamic Island オーバーレイにメッセージを浮かべていたが、
        オーバーレイ廃止に伴いログ出力のみに変更。状態自体はトレイアイコンの
        色（IDLE 青 / RECORDING 赤 等）で判別できる。
        """
        messages = {
            "groq_unavailable": "Groq API unavailable - GROQ_API_KEY または Keychain を確認してください",
            "openai_unavailable": "OpenAI API unavailable - OPENAI_API_KEY または Keychain を確認してください",
        }
        message = messages.get(warning_type, f"API unavailable: {warning_type}")
        logger.warning(message)

    def _preload_vad_model(self) -> None:
        """
        VADモデルをプリロードする。

        最初の音声入力時のVADモデルロード遅延を回避する。
        """
        try:
            for slot in self._hotkey_slots.values():
                if slot.api_transcriber and hasattr(slot.api_transcriber, 'preload_vad'):
                    slot.api_transcriber.preload_vad()
                    logger.info(f"スロット{slot.slot_id}のVADをプリロードしました")
            logger.info("VADプリロード完了")
        except Exception as e:
            logger.warning(f"VADプリロードに失敗しました: {e}")

    def _preload_models_async(self) -> None:
        """
        起動時にモデルをバックグラウンドでプリロードする。

        UIをブロックせずにVADモデルをロードする。
        """
        if not self._config.get("preload_on_startup", True):
            logger.info("起動時プリロードが無効です")
            return

        threading.Thread(target=self._preload_vad_model, daemon=True).start()

    def _setup_ui_components(self) -> None:
        """UIコンポーネントを初期化する。"""
        self._settings_window = SettingsWindow(platform_adapter=self._platform)
        self._tray = SystemTray(platform_adapter=self._platform)

    def _setup_signals(self) -> None:
        """シグナルをスロットに接続する。"""
        self._tray.open_settings.connect(self._open_settings)
        self._tray.force_reset.connect(self.force_reset_recording)
        self._tray.quit_app.connect(self._quit_app)
        self.status_changed.connect(self._update_ui_status)
        self.text_ready.connect(self._handle_transcription_result)

    def _setup_state(self) -> None:
        """アプリケーション状態を初期化する。"""
        self._is_recording = False
        self._is_transcribing = False
        self._active_slot: Optional[int] = None  # 現在アクティブなスロット

        # 文字起こしキュー関連
        self._transcription_queue: queue.Queue = queue.Queue()
        self._queue_worker_running = False
        # ワーカー起動の check-and-set を排他化（二重ワーカー起動を防ぐ）
        self._queue_worker_lock = threading.Lock()

        # ダブルタップ検出用の状態
        self._last_hotkey_release_time: float = 0.0
        self._last_hotkey_release_slot: Optional[int] = None
        self._auto_enter_active: bool = False
        self._double_tap_window_sec: float = 0.4  # 400msのダブルタップ判定ウィンドウ

        # ホットキースロットの初期化
        self._hotkey_slots: Dict[int, HotkeySlot] = {}
        self._setup_hotkey_slots()
        self._api_common_settings = self._get_common_api_settings()

        # 現在押されているキー（全スロット共通）
        self._pressed_keys: Set[str] = set()

        # スレッド制御
        self._monitoring = True
        # キーボードリスナーへの参照（停止・再起動のため保持）
        self._listener: Optional[Any] = None
        # 録音 start/stop の check-then-set を排他化（並列で start↔stop が競合する race を防ぐ）
        # ロック順序: _recording_lock を取得した中で _queue_worker_lock を取る（逆順は禁止）
        self._recording_lock = threading.RLock()

    def _setup_hotkey_slots(self) -> None:
        """両方のホットキースロットを設定する。

        既存の API Transcriber インスタンスがあれば先に close() を呼び、
        Hot reload 時に httpx 接続プールが leak しないようにする。
        """
        # 旧 transcriber の HTTP コネクションプールを閉じる
        for old_slot in self._hotkey_slots.values():
            old_transcriber = old_slot.api_transcriber
            if old_transcriber is not None and hasattr(old_transcriber, "close"):
                try:
                    old_transcriber.close()
                except Exception as e:
                    logger.warning(f"旧 transcriber close 失敗 (slot{old_slot.slot_id}): {e}")

        for slot_id in [1, 2]:
            slot_config = self._config.get(f"hotkey{slot_id}", {})

            hotkey = slot_config.get("hotkey", f"<f{slot_id + 1}>")
            hotkey_mode = slot_config.get("hotkey_mode", HotkeyMode.TOGGLE.value)
            backend = slot_config.get("backend", "openai")
            if backend not in [TranscriptionBackend.GROQ.value, TranscriptionBackend.OPENAI.value]:
                logger.warning(
                    f"未対応バックエンド '{backend}' が設定されています。openai にフォールバックします。"
                )
                backend = TranscriptionBackend.OPENAI.value
            api_model = slot_config.get("api_model", "")
            api_prompt = slot_config.get("api_prompt", "")

            # APIモデルのデフォルト値を設定
            if not api_model and backend in ["groq", "openai"]:
                defaults = self._config.get("default_api_models", {})
                api_model = defaults.get(backend, "")

            slot = HotkeySlot(
                slot_id=slot_id,
                hotkey=hotkey,
                hotkey_mode=hotkey_mode,
                required_keys=self._parse_hotkey(hotkey),
                backend=backend,
                api_model=api_model,
                api_prompt=api_prompt,
            )

            # API Transcriberの作成
            slot.api_transcriber = self._create_api_transcriber(slot)

            self._hotkey_slots[slot_id] = slot
            logger.info(f"ホットキースロット{slot_id}: {hotkey} ({hotkey_mode}) -> {backend}")

    def _start_background_threads(self) -> None:
        """ホットキーと設定監視のバックグラウンドスレッドを開始する。"""
        # ホットキーリスナー
        self._listener_thread = threading.Thread(
            target=self._start_keyboard_listener,
            daemon=True
        )
        self._listener_thread.start()
        
        # 設定ファイル監視
        self._monitor_thread = threading.Thread(
            target=self._monitor_config,
            daemon=True
        )
        self._monitor_thread.start()

    # -------------------------------------------------------------------------
    # UIアクション
    # -------------------------------------------------------------------------

    def _open_settings(self) -> None:
        """設定ウィンドウを開く。

        macOS の Accessory モードでは Qt 標準の `show()` だけでは前面化しない
        ことがあるため、最小化からの復帰・raise_/activate を経由して、最後に
        プラットフォーム固有処理（macOS は `NSApp.activateIgnoringOtherApps_`）
        で他アプリの上に確実に出す。
        """
        win = self._settings_window
        if win.isMinimized():
            win.showNormal()
        else:
            win.show()
        win.raise_()
        win.activateWindow()
        self._platform.bring_to_front(win)

    def _quit_app(self) -> None:
        """アプリケーションを終了する。

        キーボードリスナーと録音ストリームを明示的に停止してから Qt を終了する。
        これを怠るとマイクが OS にロックされたままになる。
        """
        logger.info("終了中...")
        self._monitoring = False

        # キーボードリスナーを停止（listener.join() のブロックを解除）
        listener = self._listener
        if listener is not None:
            try:
                listener.stop()
            except Exception as e:
                logger.warning(f"キーボードリスナー停止失敗: {e}")

        # 録音を停止してマイクを OS に確実に返す
        try:
            self._recorder.stop()
        except Exception as e:
            logger.warning(f"終了時の録音停止失敗: {e}")

        QApplication.quit()

    def _update_ui_status(self, status: str) -> None:
        """UIコンポーネントの状態を更新する。"""
        self._tray.set_status(status)

    def _handle_transcription_result(self, text: str, auto_enter: bool = False) -> None:
        """
        文字起こし結果を処理する。

        Args:
            text: 文字起こしテキスト
            auto_enter: Trueの場合、テキスト挿入後にEnterキーを自動送信
        """
        if not text:
            logger.info("テキストが検出されませんでした。")
            return

        if text.startswith("Error:"):
            logger.error(f"文字起こし失敗: {text}")
            return

        # 開発者モード：出力を引用符で囲む
        dev_mode = self._config.get("dev_mode", False)
        if dev_mode:
            text = f'"{text}"'

        logger.info(f"結果: {text}" + (" [auto_enter]" if auto_enter else ""))

        insert_start = time.perf_counter()
        self._input_handler.insert_text(text)
        insert_time = (time.perf_counter() - insert_start) * 1000

        # ダブルタップモード：テキスト挿入後にEnterキーを自動送信
        if auto_enter:
            # 設定で調整可能（既定50ms）。一部アプリは即時Enterに反応しないため
            delay_ms = self._config.get("auto_enter_delay_ms", 50)
            time.sleep(max(0, delay_ms) / 1000.0)
            self._input_handler.press_enter()
            logger.info(f"auto_enter: Enterキーを送信しました (delay={delay_ms}ms)")

        # 開発者モード：タイミングをファイルに記録
        if dev_mode:
            self._log_timing_to_file(insert_time)

    def _log_timing_to_file(self, insert_time: float) -> None:
        """
        タイミングデータをdev_timing.logファイルに記録する。
        
        Args:
            insert_time: テキスト挿入時間（ミリ秒）
        """
        import datetime
        log_file = "dev_timing.log"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 前回の文字起こしからタイミング情報を取得
        whisper_time = getattr(self, '_last_whisper_time', 0)
        audio_duration = getattr(self, '_last_audio_duration', 0)
        vad_time = getattr(self, '_last_vad_time', 0)
        whisper_api_time = getattr(self, '_last_whisper_api_time', 0)
        
        # 実際の合計時間を計算（Whisper + Insert）
        real_total_time = whisper_time + insert_time
        
        # 詳細なログエントリ
        log_entry = (
            f"{timestamp} | "
            f"Audio: {audio_duration:.1f}s | "
            f"VAD: {vad_time:.0f}ms | "
            f"WhisperAPI: {whisper_api_time:.0f}ms | "
            f"Insert: {insert_time:.0f}ms | "
            f"Total: {real_total_time:.0f}ms\n"
        )
        
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(log_entry)
            logger.debug(f"タイミングを {log_file} に記録しました")
        except Exception as e:
            logger.warning(f"タイミングログの書き込みに失敗: {e}")

    # -------------------------------------------------------------------------
    # 録音と文字起こし
    # -------------------------------------------------------------------------

    def start_recording(self, slot_id: Optional[int] = None) -> None:
        """
        音声録音を開始する。

        Args:
            slot_id: アクティブなホットキースロットID
        """
        with self._recording_lock:
            if self._is_recording or slot_id is None:
                return

            self._active_slot = slot_id
            slot = self._hotkey_slots[slot_id]

            logger.info(f"録音開始 (スロット {slot_id}, バックエンド: {slot.backend})")

            transcriber = self._get_transcriber_for_slot(slot)
            if transcriber is None:
                logger.warning(f"スロット{slot_id}のAPIクライアント初期化に失敗したため録音を開始しません。")
                self._active_slot = None
                self._show_backend_warning(f"{slot.backend}_unavailable")
                return

            self._is_recording = True
            if self._auto_enter_active:
                self.status_changed.emit("recording_auto_enter")
            else:
                self.status_changed.emit("recording")

            # 使用するTranscriberのモデルをプリロード
            threading.Thread(target=transcriber.load_model, daemon=True).start()
            self._recorder.start()

    def force_reset_recording(self) -> None:
        """強制リセット: 新プロセスを起動して自分は終了する。

        PortAudio / CoreAudio のマイクハンドルや「マイク使用中」のオレンジドットは
        プロセスが死ぬまで OS から解放されない。execv だと macOS では同 PID のまま
        NSApplication を作り直すことになり、メニューバー (NSStatusItem) が
        AppKit に再登録されず表示されない事象がある。そのため subprocess.Popen で
        独立した新プロセスを spawn し、自分は os._exit(0) で即時終了する方式に
        統一する。これで:
        - 新プロセスは新規 NSApplication として起動 → メニューバー正常
        - 旧プロセスは即時終了 → OS がマイクハンドル回収 → オレンジドット消失

        ユーザーがメニューから明示的に押した時のみ呼ばれる（自動発動はしない）。
        """
        logger.warning("強制リセット: 新プロセス起動 → 自分は終了します")

        # キーボードリスナーを止めて pynput が握っているリソースを早期解放。
        # 子プロセス側で同じキーをグローバルフックすると競合する可能性があるため。
        try:
            listener = self._listener
            if listener is not None:
                listener.stop()
        except Exception as e:
            logger.warning(f"リスナー停止失敗（無視して再起動）: {e}")

        # ログを確実にディスクへ流す（os._exit は flush しないため）。
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass

        # 同じコマンドラインで子プロセスを独立起動。
        # start_new_session=True で新セッション化し、親終了後も生き残る。
        python = sys.executable
        args = [python] + sys.argv
        logger.info(f"新プロセス起動: {python} {sys.argv}")
        try:
            subprocess.Popen(args, start_new_session=True, cwd=os.getcwd())
        except Exception as e:
            logger.error(f"新プロセス起動失敗、再起動を中止: {e}")
            return

        # 旧プロセスは即時終了。Qt/PortAudio のクリーンアップは飛ばすが、
        # OS がプロセス終了時にハンドルを回収するのでマイクは解放される。
        os._exit(0)

    def stop_and_transcribe(self) -> None:
        """録音を停止して文字起こしタスクをキューに追加する。

        keyboard listener スレッドから呼ばれることを前提に、フラグ更新だけを
        同期で済ませ、recorder.stop()（最大 2 秒のタイムアウト）以降は
        別スレッドに逃がす。これによりキーを離した直後の次のキー押下イベントが
        listener で詰まらず、ダブルタップ検出が正常に動作する。
        """
        with self._recording_lock:
            if not self._is_recording or self._active_slot is None:
                return

            logger.info("録音停止")
            self._is_recording = False

            # ダブルタップのauto_enterフラグを取得してリセット
            auto_enter = self._auto_enter_active
            self._auto_enter_active = False

            active_slot_id = self._active_slot

        # 重い処理（recorder.stop の 2 秒タイムアウト含む）は別スレッドへ。
        threading.Thread(
            target=self._finalize_recording_async,
            args=(active_slot_id, auto_enter),
            daemon=True,
            name="FinalizeRecording",
        ).start()

    def _finalize_recording_async(self, active_slot_id: int, auto_enter: bool) -> None:
        """録音停止と文字起こしキューへの投入を listener スレッド外で実行する。

        recorder.stop() が PortAudio の close 待ちで最大 2 秒ブロックするため、
        listener スレッドを巻き込まないようここで完結させる。
        """
        audio_data = self._recorder.stop()

        # 音声データが空の場合
        if len(audio_data) == 0:
            # 後続の録音や文字起こしが走っていなければ idle に戻す。
            # ダブルタップ等で新しい録音が既に始まっている場合は、その status
            # (recording / recording_auto_enter) を上書きしないよう触らない。
            if not self._queue_worker_running and not self._is_recording:
                self.status_changed.emit("idle")
            return

        # API 送信前の音声前処理（音量正規化）
        # 失敗してもアプリは止めず原音で続行。
        preprocess_cfg = self._config.get("audio_preprocess", {}) or {}
        try:
            audio_data = preprocess_audio(
                audio_data,
                sample_rate=SAMPLE_RATE,
                enable_normalize=bool(preprocess_cfg.get("volume_normalize", True)),
            )
        except Exception as e:
            logger.warning(f"音声前処理でエラー、原音を使用: {e}")

        # 開発者モード用に保存
        audio_duration = len(audio_data) / SAMPLE_RATE
        self._last_audio_duration = audio_duration

        # タスクをキューに追加
        task = TranscriptionTask(
            audio_data=audio_data,
            slot_id=active_slot_id,
            timestamp=time.perf_counter(),
            auto_enter=auto_enter,
        )
        self._transcription_queue.put(task)

        # 処理中状態を表示。ただしダブルタップ等で既に次の録音が走っている場合は
        # その状態（recording / recording_auto_enter）を維持する。
        if not self._is_recording:
            self.status_changed.emit("transcribing")

        # ワーカーが動いていなければ開始（check-and-set はロックで排他化）
        with self._queue_worker_lock:
            if not self._queue_worker_running:
                self._start_queue_worker_locked()

    def _start_queue_worker_locked(self) -> None:
        """文字起こしキュー処理ワーカースレッドを開始する。

        呼び出し側が self._queue_worker_lock を取得済みであることを前提とする。
        """
        self._queue_worker_running = True
        self._is_transcribing = True
        threading.Thread(target=self._queue_processor, daemon=True).start()

    def _queue_processor(self) -> None:
        """キューからタスクを順番に処理するワーカー。

        個別タスクの例外でワーカー全体が死なないよう、各タスク処理を
        try/except/finally で囲み、必ず task_done() を呼ぶ。
        """
        try:
            while True:
                try:
                    task = self._transcription_queue.get(timeout=0.1)
                except queue.Empty:
                    break

                try:
                    self._process_transcription_task(task)
                except Exception as e:
                    # タスク単位の例外を吸収してワーカーを止めない
                    logger.exception(f"文字起こしタスク処理で例外発生: {e}")
                finally:
                    try:
                        self._transcription_queue.task_done()
                    except ValueError:
                        # 万一 task_done が多すぎても無視（キューを止めない）
                        pass
        finally:
            with self._queue_worker_lock:
                self._queue_worker_running = False
                self._is_transcribing = False
            if self._transcription_queue.empty() and not self._is_recording:
                self.status_changed.emit("idle")

    def _process_transcription_task(self, task: TranscriptionTask) -> None:
        """
        単一の文字起こしタスクを処理する。

        Args:
            task: 処理する文字起こしタスク
        """
        try:
            slot = self._hotkey_slots[task.slot_id]
            transcriber = self._get_transcriber_for_slot(slot)
            if transcriber is None:
                self.text_ready.emit(f"Error: {slot.backend} transcriber is unavailable", False)
                return

            transcribe_start = time.perf_counter()
            text = transcriber.transcribe(task.audio_data)
            transcribe_time = (time.perf_counter() - transcribe_start) * 1000

            # 開発者モード用に保存
            self._last_whisper_time = transcribe_time
            self._last_vad_time = getattr(transcriber, 'last_vad_time', 0)
            self._last_whisper_api_time = getattr(transcriber, 'last_api_time', 0)
            self._last_total_time = (time.perf_counter() - task.timestamp) * 1000

            self.text_ready.emit(text, task.auto_enter)
        except Exception as e:
            logger.error(f"文字起こしエラー: {e}")
            self.text_ready.emit("", False)

    # -------------------------------------------------------------------------
    # ホットキー処理
    # -------------------------------------------------------------------------



    def _start_keyboard_listener(self) -> None:
        """両方のホットキースロットを監視するキーボードリスナーを開始する。

        リスナーが例外で停止しても自動的に再起動する。
        外部から self._listener.stop() で停止すると正常終了として扱い、
        self._monitoring が True なら新しい設定で再起動する（Hot reload 用）。
        """
        while self._monitoring:
            listener = None
            try:
                # いずれかのスロットがHoldモードの場合は低レベルリスナーを使用
                has_hold_mode = any(
                    slot.hotkey_mode == HotkeyMode.HOLD.value
                    for slot in self._hotkey_slots.values()
                )

                if has_hold_mode:
                    listener = keyboard.Listener(
                        on_press=self._handle_key_press,
                        on_release=self._handle_key_release,
                    )
                else:
                    # 両方Toggleモードの場合はGlobalHotKeysを使用
                    hotkey_map = {}
                    for slot_id, slot in self._hotkey_slots.items():
                        hotkey_map[slot.hotkey] = lambda sid=slot_id: self._on_activate_toggle(sid)
                    listener = keyboard.GlobalHotKeys(hotkey_map)

                self._listener = listener
                with listener:
                    listener.join()
            except Exception as e:
                # リスナーが例外で死んでも黙って永久停止しないよう必ず再起動
                logger.error(f"キーボードリスナーが停止しました（{e!r}）。再起動します")
            finally:
                self._listener = None
                # 再起動時に古いキー状態を持ち越さない
                # （listener 死亡で取りこぼした on_release を強制クリア）
                self._pressed_keys.clear()

            if not self._monitoring:
                break
            # busy-loop 防止（Hot reload 時はほぼ即時に次へ進む）
            time.sleep(0.5)

    def _on_activate_toggle(self, slot_id: int) -> None:
        """
        トグルモードのアクティベーションを処理する。

        Args:
            slot_id: アクティベーションされたスロットID
        """
        if not self._is_recording:
            self.start_recording(slot_id)
        else:
            self.stop_and_transcribe()

    def _handle_key_press(self, key: Any) -> None:
        """
        キー押下イベントを処理する。

        ダブルタップ検出：前回のリリースから短時間内に同じスロットの
        ホットキーが押された場合、auto_enterモードで録音を開始する。

        Args:
            key: 押されたキー
        """
        try:
            key_str = self._normalize_key(key)
            if key_str is None:
                # 正規化失敗キーは無視（後で発見できるよう debug ログだけ残す）
                logger.debug(f"キー正規化に失敗（無視）: {key!r}")
                return

            self._pressed_keys.add(key_str)
            # 録音中でなければ、どのスロットのホットキーかチェック
            if not self._is_recording:
                for slot_id, slot in self._hotkey_slots.items():
                    if self._check_hotkey_match_for_slot(slot):
                        # ダブルタップ検出：同じスロットで短時間内の再押下
                        now = time.perf_counter()
                        if (self._last_hotkey_release_slot == slot_id
                                and (now - self._last_hotkey_release_time) < self._double_tap_window_sec):
                            self._auto_enter_active = True
                            logger.info(f"ダブルタップ検出 (スロット{slot_id}) - auto_enterモード")
                        else:
                            self._auto_enter_active = False
                        self.start_recording(slot_id)
                        break
        except Exception as e:
            # ハンドラ内例外を握り潰すとリスナーが止まるため必ず復帰
            logger.exception(f"キー押下処理で例外: {e}")

    def _handle_key_release(self, key: Any) -> None:
        """
        キー解放イベントを処理する。

        ダブルタップ検出のためにリリース時刻とスロットを記録する。
        正規化失敗で「録音中なのに押下キーが消失」した状態を検出したら、
        永久録音を防ぐ保険として stop_and_transcribe() を呼ぶ。

        Args:
            key: 解放されたキー
        """
        try:
            key_str = self._normalize_key(key)
            if key_str is None:
                logger.debug(f"キー正規化に失敗（無視）: {key!r}")
                # 保険：押下キーが空なのに録音中の場合は停止（永久録音防止）
                if self._is_recording and not self._pressed_keys:
                    logger.warning("正規化失敗時に押下キー無し＋録音中を検出 → 安全のため停止")
                    self.stop_and_transcribe()
                return

            if key_str in self._pressed_keys:
                self._pressed_keys.remove(key_str)
            # ホットキーに含まれるキーが離されたら録音停止
            if self._is_recording and self._active_slot is not None:
                active_slot = self._hotkey_slots[self._active_slot]
                if self._is_hotkey_key_released_for_slot(key_str, active_slot):
                    # ダブルタップ検出用にリリース時刻とスロットを記録
                    self._last_hotkey_release_time = time.perf_counter()
                    self._last_hotkey_release_slot = self._active_slot
                    self.stop_and_transcribe()
        except Exception as e:
            logger.exception(f"キー解放処理で例外: {e}")

    def _is_hotkey_key_released_for_slot(self, key_str: str, slot: HotkeySlot) -> bool:
        """
        解放されたキーが指定スロットのホットキーの一部かチェックする。

        汎用修飾キー（ctrl, alt, shift）の場合は対応する左右キーも確認。

        Args:
            key_str: 解放されたキー文字列
            slot: チェック対象のスロット

        Returns:
            ホットキーの一部の場合True
        """
        # 直接マッチ
        if key_str in slot.required_keys:
            return True

        # 汎用修飾キーへのマッピングをチェック
        specific_to_generic = {
            'ctrl_l': 'ctrl', 'ctrl_r': 'ctrl',
            'alt_l': 'alt', 'alt_r': 'alt',
            'shift_l': 'shift', 'shift_r': 'shift',
            'cmd_l': 'cmd', 'cmd_r': 'cmd',
        }

        generic_key = specific_to_generic.get(key_str)
        if generic_key and generic_key in slot.required_keys:
            return True

        return False

    def _normalize_key(self, key: Any) -> Optional[str]:
        """
        キーを標準的な文字列表現に正規化する。
        
        左右の修飾キー（ctrl_l/r, alt_l/r, shift_l/r, cmd_l/r）を
        個別に認識しつつ、汎用設定（ctrl, alt, shift）にも対応。
        
        Args:
            key: 正規化するキー
            
        Returns:
            正規化されたキー文字列、または失敗時None
        """
        return self._platform.normalize_listener_key(key)

    def _parse_hotkey(self, hotkey_str: str) -> Set[str]:
        """
        ホットキー文字列をキー名のセットにパースする。
        
        汎用設定 (ctrl, alt, shift) と左右指定 (ctrl_l, alt_r) の
        両方に対応。汎用設定の場合は左右両方を展開する。
        
        Args:
            hotkey_str: ホットキー文字列（例："<ctrl>+<space>" or "<alt_r>"）
            
        Returns:
            キー名のセット
        """
        keys = hotkey_str.replace('<', '').replace('>', '').split('+')
        result = set()
        
        for k in keys:
            k = k.strip()
            if k:
                result.add(k)
        
        return result

    def _check_hotkey_match_for_slot(self, slot: HotkeySlot) -> bool:
        """
        現在押されているキーが指定スロットのホットキー設定と一致するかチェックする。

        汎用修飾キー（ctrl, alt, shift）の場合は左右どちらでも一致、
        左右指定（ctrl_l, alt_r等）の場合は完全一致を要求。

        Args:
            slot: チェック対象のスロット

        Returns:
            ホットキーが一致した場合True
        """
        # 汎用修飾キーから具体的な左右キー名へのマッピング
        generic_to_specific = {
            'ctrl': ('ctrl_l', 'ctrl_r'),
            'alt': ('alt_l', 'alt_r'),
            'shift': ('shift_l', 'shift_r'),
            'cmd': ('cmd_l', 'cmd_r'),
        }

        for required_key in slot.required_keys:
            if required_key in generic_to_specific:
                # 汎用キー: 左右どちらかが押されていればOK
                left, right = generic_to_specific[required_key]
                if left not in self._pressed_keys and right not in self._pressed_keys:
                    return False
            else:
                # 具体的なキー（ctrl_l等）または通常キー: 完全一致
                if required_key not in self._pressed_keys:
                    return False

        return True

    # -------------------------------------------------------------------------
    # 設定監視
    # -------------------------------------------------------------------------

    def _monitor_config(self) -> None:
        """設定ファイルの変更を監視する。"""
        while self._monitoring:
            time.sleep(CONFIG_CHECK_INTERVAL_SEC)
            
            if self._config.reload_if_changed():
                self._apply_config_changes()
                logger.info("設定を再読み込みして適用しました。")

    def _apply_config_changes(self) -> None:
        """設定変更を適用する。"""
        # 入力デバイス設定を更新
        next_input_device = AudioRecorder.normalize_device_setting(
            self._config.get("audio_input_device", "default")
        )
        if next_input_device != self._current_input_device:
            self._recorder.set_input_device(next_input_device)
            self._current_input_device = next_input_device
            device_label = "default" if next_input_device is None else str(next_input_device)
            logger.info(f"入力デバイス設定を更新: {device_label}")

        # ホットキースロット設定を更新
        slots_changed = False
        for slot_id in [1, 2]:
            slot_config = self._config.get(f"hotkey{slot_id}", {})
            new_hotkey = slot_config.get("hotkey", f"<f{slot_id + 1}>")
            new_mode = slot_config.get("hotkey_mode", HotkeyMode.TOGGLE.value)
            new_backend = slot_config.get("backend", "openai")
            if new_backend not in [TranscriptionBackend.GROQ.value, TranscriptionBackend.OPENAI.value]:
                new_backend = TranscriptionBackend.OPENAI.value
            new_api_model = slot_config.get("api_model", "")
            new_api_prompt = slot_config.get("api_prompt", "")

            current_slot = self._hotkey_slots.get(slot_id)
            if current_slot:
                if (new_hotkey != current_slot.hotkey or
                    new_mode != current_slot.hotkey_mode or
                    new_backend != current_slot.backend or
                    new_api_model != current_slot.api_model or
                    new_api_prompt != current_slot.api_prompt):
                    slots_changed = True
                    logger.info(f"ホットキースロット{slot_id}を更新: {new_hotkey} -> {new_backend}")

        # language/VADの共通設定が変わった場合もTranscriberを再作成
        current_common_settings = self._get_common_api_settings()
        if current_common_settings != self._api_common_settings:
            slots_changed = True
            self._api_common_settings = current_common_settings

        # スロット設定が変更された場合は再初期化＋リスナー再起動
        if slots_changed:
            self._setup_hotkey_slots()
            # 現在のリスナーを停止すると、_start_keyboard_listener の while ループが
            # 新しいスロット設定で次のリスナーを起動する（_monitoring=True のため）
            listener = self._listener
            if listener is not None:
                try:
                    listener.stop()
                    logger.info("Hot reload: キーボードリスナーを再起動します")
                except Exception as e:
                    logger.warning(f"Hot reload 時のリスナー停止失敗: {e}")
