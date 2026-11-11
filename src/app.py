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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Callable, Dict, Optional, Set

from PySide6.QtCore import QObject, QTimer, Signal
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
from .core import media_ducker
from .core import numeral_normalizer
from .core import sound_fx
from .core.audio_preprocess import preprocess as preprocess_audio
from .core.text_utils import join_segments
from .core.history import HistoryStore
from .core.history_sync import HistorySync
from .core.stats import StatsStore
from .platform import get_platform_adapter
from .ui import Hud, SettingsWindow, SideNotch, SystemTray
from .utils.logger import get_logger
from .utils.updater import Updater

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
# 長文の分割並列送信を発動する最小録音長（秒）。vad._MIN_SPLIT_SEC と一致させる
_SPLIT_MIN_SEC: float = 12.0
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


@dataclass
class TaskContext:
    """録音開始時に確定する不変の処理コンテキスト（スナップショット）。

    設定の hot-reload やスロット変更が「録音中〜処理開始」の間に挟まっても、
    録音開始時点で選ばれた slot / provider / model / language / transcriber と
    処理フラグだけで文字起こしを完遂させるために使う。これが無いと、
    キュー滞留中に設定が変わると別プロバイダーへ音声を送ってしまう。

    Attributes:
        slot: 録音開始時のホットキースロット（transcriber・backend・model・整形可否を内包）
        language: 録音開始時の言語設定
        vad_on: VAD フィルタ有効か
        split_on: 長文分割並列送信が有効か
        volume_normalize: 音量正規化を行うか
        format_model: テキスト整形に使うモデル名
        format_auto_prompt: テキスト整形プロンプト
    """
    slot: HotkeySlot
    language: str
    vad_on: bool
    split_on: bool
    volume_normalize: bool
    format_model: str
    format_auto_prompt: str


class VoicekeyApp(QObject):
    """
    メインアプリケーションコントローラー。

    Signals:
        status_changed: UI 状態の変更通知（"idle"/"recording"/"recording_auto_enter"/"transcribing"）
        audio_level: 録音中の音声レベル（0.0-1.0、約 30fps）。HUD のレベルメーター用
        notice: ユーザーに見せるべき一時メッセージ（エラー、無音検出など）
        interim_text: ストリーミング中のライブ字幕（受信スレッド → メインへホップ）
        hands_free_changed: 現在の録音がハンズフリー（toggle 実効）か。HUD のドット/波形の着色用
    """

    status_changed = Signal(str)
    audio_level = Signal(float)
    notice = Signal(str)
    interim_text = Signal(str)
    # 録音がハンズフリー（toggle 実効モード）かどうか。Mac 版 handsFree と同義で、HUD は
    # ラベルではなくドット/波形の色（ティール）だけで通常録音と区別する。status_changed とは
    # 別チャネルにするのは、status_changed が tray の AppState 変換に使われており文字列集合を
    # 増やせないため（recording 文字列は変えずに色情報だけ HUD へ運ぶ）。
    hands_free_changed = Signal(bool)
    # 履歴同期の受信キャッシュが変わった（同期スレッドから emit → UI スレッドで履歴表示を更新）
    history_sync_changed = Signal()

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
        # 録音中の実効モード（ハンズフリー切替キー併用時は hold スロットでも toggle になる）
        self._recording_effective_mode: str = HotkeyMode.HOLD.value
        self._recording_started: float = 0.0        # 録音開始時刻（monotonic）
        self._auto_enter = False                    # ダブルタップによる auto_enter 録音か
        self._outstanding = 0                       # 未完了の文字起こしタスク数
        # ストリーミング録音中の Deepgram セッション（非ストリーミング時は None）
        self._active_streamer: Optional[StreamingTranscriber] = None
        # 録音開始時に確定する処理コンテキストのスナップショット（_finish_recording で task へ移す）
        self._active_context: Optional[TaskContext] = None

        # オンボーディングの整形体験ステップ中だけ整形を強制 ON にする一時フラグ（保存しない）。
        # True の間の録音は、スロットの format_enabled 設定に関わらず整形を実行する
        # （フィラー除去を体験させるため）。ステップを出たら必ず False へ戻す。
        self._practice_format_override = False

        # オンボーディングの録音キーテスト／マイクテスト中だけ True。True の間は録音を開始せず、
        # 押された録音キーを _hotkey_test_callback へ報告するだけにする（無料枠を消費しない）。
        # コールバックは listener スレッドから呼ばれるので、UI 側は Qt シグナルでメインスレッドへ渡すこと。
        self._hotkey_test_active = False
        self._hotkey_test_callback: Optional[Callable[[Optional[int]], None]] = None

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
        # ハンズフリー(toggle 実効)録音で groq スロットの代わりに使う EL(scribe_v1) を常設する
        # （長時間録音の精度対策）。設定再適用時に作り直す。
        self._handsfree_transcriber: ElevenLabsTranscriber = self._build_handsfree_transcriber()
        # ハンズフリー切替キー（この切替キー＋スロットキーで toggle 録音。空＝無効）
        self._handsfree_keys: Set[str] = self._parse_hotkey(
            self._config.get("handsfree_key", "")
        )

        # --- 文字起こしワーカー（常駐 1 本、None センチネルで終了） ---
        self._task_q: "queue.Queue[Optional[TranscriptionTask]]" = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="Transcription"
        )
        self._worker.start()

        # 前回録音中に異常終了していた場合のダッキング巻き戻し（残存フラグがあれば元音量へ戻す）。
        # Mac 版 startup() の MediaDucker.restore() と対（撃ちっぱなし・録音前に一度だけ）。
        media_ducker.restore()

        # --- 音声入力履歴（貼り付けたテキストを最大 10 件保持。設定の「履歴」タブで再コピー可） ---
        # Mac と履歴を共有。貼り付け経路は enqueue だけ＝ファイル追記のみでブロックしない。
        # on_changed は同期スレッドから呼ばれるため Signal 経由で UI スレッドへ渡す
        self._history_sync = HistorySync(self._config, on_changed=self.history_sync_changed.emit)
        self._history = HistoryStore(on_add=self._history_sync.enqueue)

        # --- 使用実績（節約時間・レベル・連続日数。設定の「実績」タブで表示。貼り付け後に集計する） ---
        self._stats = StatsStore()

        # --- UI ---
        # 自動アップデータは設定ウィンドウ（「バージョン情報」タブ）にも渡すため先に生成する。
        # 固めた実行ファイル（PyInstaller）でのみ定期チェックが動く（start() が sys.frozen を見る）。
        self._updater = Updater(parent=self)

        # 設定ウィンドウには本体と同じ ConfigManager を渡す（二重生成を避け保存値を即共有）。
        # 保存完了シグナルで設定変更をその場で適用する（mtime ポーリングの遅延を待たない）
        self._settings_window = SettingsWindow(
            platform_adapter=self._platform,
            history=self._history,
            history_sync=self._history_sync,
            stats=self._stats,
            config_manager=self._config,
            updater=self._updater,
        )
        self._settings_window.settings_saved.connect(self._apply_config_changes)
        # Mac 側の履歴が届いたら開いている履歴表示を作り直す（キュー接続で UI スレッド上で実行される）
        self.history_sync_changed.connect(self._settings_window.refresh_history_views)
        # 設定「一般」の「セットアップガイドを開く」からもう一度オンボーディングを表示する
        self._settings_window.open_onboarding_requested.connect(self.show_onboarding)
        self._tray = SystemTray(platform_adapter=self._platform)
        self._tray.open_settings.connect(self._open_settings)
        self._tray.force_reset.connect(self._force_restart)
        self._tray.quit_app.connect(self._quit_app)
        self.status_changed.connect(self._tray.set_status)

        # 自動アップデート通知・インストールはトレイ経由（設定タブは settings_window 側で配線済み）
        self._updater.update_available.connect(self._tray.show_update_available)
        self._updater.update_failed.connect(self._tray.show_update_failed)
        self._updater.quit_requested.connect(self._quit_app)
        self._tray.install_update.connect(self._updater.download_and_install)
        self._updater.start()

        # 録音中 HUD（画面下部中央の小型ピル）。シグナルはワーカー/音声スレッドから
        # 発火するが、Qt のキュー接続でメインスレッド上の HUD へ安全にホップする
        self._hud = Hud(enabled=bool(self._config.get("hud_enabled", True)))
        # 待機中も小型ピルを常時表示するか（Mac 版 hudAlwaysVisible と同義）。
        self._hud.always_visible = bool(self._config.get("hud_always_visible", False))
        self.status_changed.connect(self._hud.set_state)
        self.audio_level.connect(self._hud.push_level)
        self.notice.connect(self._hud.show_notice)
        self.interim_text.connect(self._hud.set_caption)
        # ハンズフリー着色は status より先に届くよう、_emit_state で status_changed の前に emit する
        self.hands_free_changed.connect(self._hud.set_hands_free)

        # サイドノッチ（画面左端の履歴スリット）。Mac 版 SideNotch と対。録音状態で点灯し、
        # クリックで履歴パネルを開く。「ホームを開く」は設定ウィンドウのホームページを開く。
        self._side_notch = SideNotch(
            history=self._history,
            is_dark=bool(self._config.get("dark_mode", False)),
            enabled=bool(self._config.get("side_notch_enabled", True)),
        )
        self.status_changed.connect(self._side_notch.set_status)
        self._side_notch.open_home_requested.connect(self._open_home)

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

        # 初回起動オンボーディング（Phase 5）。未完了なら初回だけセットアップウィンドウを出す。
        # イベントループ開始後に表示するため singleShot(0) で後回しにする（__init__ 中に
        # モーダルを出すと初期化が止まるため）。参照を保持してウィンドウが GC されないようにする。
        self._onboarding_window = None
        QTimer.singleShot(0, self._maybe_show_onboarding)

        # 履歴同期ワーカーを起動（設定で無効なら内部で待機するだけ）
        self._history_sync.enable()
        sync_status = self._history_sync.status()
        if sync_status["enabled"]:
            logger.info(f"履歴同期: 有効 ({self._config.get('history_sync', {}).get('url', '')})")
        else:
            logger.info("履歴同期: 無効")

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

    def _build_handsfree_transcriber(self) -> ElevenLabsTranscriber:
        """ハンズフリー(toggle 実効)録音で groq スロットの代わりに使う EL(scribe_v1) を作る。

        スタンダード(groq)をハンズフリーで使うと長時間録音になりやすいため、内部で
        ElevenLabs(scribe_v1)へ切り替えて精度を確保する（保存値 groq は変えない）。
        言語・既定モデルは _build_slots と同じく設定から取る。設定再適用時に作り直す。
        """
        language = self._config.get("language", "ja")
        defaults = self._config.get("default_api_models", {}) or {}
        model = defaults.get(TranscriptionBackend.ELEVENLABS.value, "scribe_v1")
        return ElevenLabsTranscriber(model=model, language=language, prompt="")

    def _maybe_handsfree_slot(self, slot: HotkeySlot, effective_mode: str) -> HotkeySlot:
        """ハンズフリー(toggle 実効)録音で groq スロットのとき、内部エンジンを
        ElevenLabs(scribe_v1)へ差し替えたスロットを返す（長時間録音の精度対策）。

        保存値(groq)は変えない。この録音の処理に使うスロットだけを差し替える。
        それ以外（hold 実効・groq 以外）は元のスロットをそのまま返す（Mac の usesHandsfreeEL と対）。

        Args:
            slot: 元のホットキースロット
            effective_mode: この録音の実効モード（ハンズフリー切替キー併用時は toggle）

        Returns:
            切替後のスロット（差し替え不要なら元のスロットそのもの）
        """
        if (
            effective_mode == HotkeyMode.TOGGLE.value
            and slot.backend == TranscriptionBackend.GROQ.value
        ):
            logger.info("ハンズフリー: 内部エンジン切替 (groq→elevenlabs)")
            return replace(
                slot,
                backend=TranscriptionBackend.ELEVENLABS.value,
                api_model="scribe_v1",
                transcriber=self._handsfree_transcriber,
            )
        return slot

    def _snapshot_context(self, slot: HotkeySlot) -> TaskContext:
        """録音開始時点の処理コンテキストを不変スナップショットとして返す。

        ここで読んだライブ設定（VAD・分割・正規化・整形）以降、設定が変わっても
        この録音の処理結果には影響させない（キュー処理はこの snapshot だけを使う）。
        """
        preprocess_cfg = self._config.get("audio_preprocess", {}) or {}
        # オンボーディング体験（整形ステップ）中はスロット設定に関わらず整形を強制 ON にする。
        # slot は dataclass なので複製して format_enabled だけ差し替える（保存値は変えない）。
        if self._practice_format_override and not slot.format_enabled:
            slot = replace(slot, format_enabled=True)
        return TaskContext(
            slot=slot,
            language=self._config.get("language", "ja"),
            vad_on=bool(self._config.get("vad_filter", True)),
            split_on=bool(self._config.get("split_parallel_enabled", True)),
            volume_normalize=bool(preprocess_cfg.get("volume_normalize", True)),
            format_model=self._config.get("format_model", "llama-3.1-8b-instant"),
            format_auto_prompt=self._config.get("format_auto_prompt", ""),
        )

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
        # 未設定（空ホットキー）のスロットを all([])==True で全キーに一致させない
        if not slot.required_keys:
            return False
        return all(
            self._acceptable_names(t) & self._pressed_keys
            for t in slot.required_keys
        )

    def _handsfree_pressed(self) -> bool:
        """ハンズフリー切替キーがすべて押されているか判定する（空＝無効で常に False）。"""
        if not self._handsfree_keys:
            return False
        return all(
            self._acceptable_names(t) & self._pressed_keys
            for t in self._handsfree_keys
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

            # オンボーディングの録音キーテスト／マイクテスト中は録音を開始せず、
            # 押されている録音キーのスロット（無ければ None）を報告するだけにする
            if self._hotkey_test_active:
                self._report_hotkey_test()
                return

            recording_slot = self._recording_slot
            if recording_slot is None:
                for slot_id, slot in self._slots.items():
                    if self._slot_matches(slot):
                        # ハンズフリー切替キー併用なら toggle 実効モードで起動する
                        # （スロット設定が hold でも、この一押しの間だけ toggle になる）
                        handsfree = self._handsfree_pressed()
                        effective_mode = (
                            HotkeyMode.TOGGLE.value if handsfree else slot.hotkey_mode
                        )
                        # ダブルタップ: 同スロットを短時間内に再押下 → auto_enter
                        # （ハンズフリー toggle 起動時はダブルタップ判定を使わない）
                        now = time.monotonic()
                        auto_enter = (
                            not handsfree
                            and self._last_release_slot == slot_id
                            and now - self._last_release_time < _DOUBLE_TAP_SEC
                        )
                        logger.info(
                            f"ホットキー押下 slot={slot_id} mode={effective_mode} "
                            f"auto_enter={auto_enter}"
                        )
                        self._begin_recording(slot_id, auto_enter, effective_mode)
                        break
            else:
                slot = self._slots.get(recording_slot)
                if slot is None or not self._slot_matches(slot):
                    return
                # toggle 実効モード: 録音中の再押下で停止（ハンズフリー切替キー併用での起動も含む）
                if self._recording_effective_mode == HotkeyMode.TOGGLE.value:
                    logger.info(f"ホットキー押下 slot={recording_slot} mode=toggle 録音停止")
                    self._finish_recording()
                # hold 実効モード: 短いタップ後の待機中に再押下されたらダブルタップ確定。
                # 録音は 1 打目から止めていないため、タップ中・タップ間の音声も残っている
                elif self._recording_effective_mode == HotkeyMode.HOLD.value:
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

            # 録音キーテスト中は録音制御に入らず、現在押されているスロット（無ければ None）を報告する
            if self._hotkey_test_active:
                self._report_hotkey_test()
                return

            recording_slot = self._recording_slot
            if recording_slot is None:
                return
            slot = self._slots.get(recording_slot)
            # toggle 実効モード（ハンズフリー起動含む）は離鍵で止めない（再押下で停止）
            if slot is None or self._recording_effective_mode != HotkeyMode.HOLD.value:
                return

            if key_str is not None and self._key_in_slot(key_str, slot):
                logger.info(
                    f"ホットキー離鍵 slot={recording_slot} mode=hold "
                    f"auto_enter={self._auto_enter}"
                )
                # ダブルタップ確定後（auto_enter）の離鍵は 2 打目の待ち窓に入れず即停止する。
                # ここで再び _DOUBLE_TAP_SEC 待つと、短い録音でも Enter 自動送信が 0.4 秒遅れていた
                if self._auto_enter:
                    self._finish_recording()
                    return
                now = time.monotonic()
                if now - self._recording_started < _DOUBLE_TAP_SEC:
                    # 短いタップ＝ダブルタップの 1 打目の可能性。録音を止めずに 2 打目を待つ
                    # （タップと同時に話し始めた声を失わない。2 打目が来なければ通常どおり確定。
                    #  誤タップで発話が無い場合は通知を出さず静かに捨てる）
                    # リリース情報は「短いタップとして成立したときだけ」記録する。長押しの離鍵で
                    # 記録すると、直後に始めた録音まで誤ってダブルタップ(auto_enter)扱いになる(#19)。
                    self._last_release_time = now
                    self._last_release_slot = recording_slot
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

    def _begin_recording(
        self, slot_id: int, auto_enter: bool,
        effective_mode: str = HotkeyMode.HOLD.value,
    ) -> None:
        """録音を開始する（listener スレッドから呼ばれる。即座に返る）。"""
        with self._state_lock:
            if self._recording_slot is not None:
                return
            # ハンズフリー(toggle 実効)で groq スロットのときは、この録音の処理に使うスロットだけ
            # 内部で EL(scribe_v1) へ差し替える（長時間録音の精度対策。保存値 groq は変えない）。
            # hold 実効・groq 以外は元スロットのまま返る。
            slot = self._maybe_handsfree_slot(self._slots[slot_id], effective_mode)
            self._recording_slot = slot_id
            self._recording_effective_mode = effective_mode
            self._recording_started = time.monotonic()
            self._auto_enter = auto_enter
            # 録音開始時点の slot/provider/model/language/処理フラグを不変スナップショット化する。
            # 設定 hot-reload やスロット変更が録音中〜処理開始に挟まっても開始時の設定で完遂する
            self._active_context = self._snapshot_context(slot)

        logger.info(
            f"録音開始要求 (スロット{slot_id}, backend={slot.backend}"
            + (", auto_enter" if auto_enter else "") + ")"
        )
        # 操作音（開始）・メディアダッキングを撃ちっぱなしで開始（音声パイプラインは待たせない）
        if self._config.get("sound_effects_enabled", True):
            sound_fx.play("start")
        if self._config.get("duck_media_enabled", True):
            media_ducker.duck()
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
                # 録音中のみ発火するフック。次の start_async がこの録音に結び付け、停止時は
                # 録音部が末尾まで送ってから外す（旧録音の音声は新 streamer へ混入しない）
                self._recorder.set_chunk_callback(stream.send)

        self._recorder.start_async(self._on_record_started)

    def _on_record_started(self, ok: bool) -> None:
        """録音開始の結果（AudioControl スレッド上で呼ばれる）。"""
        if ok:
            # 「開始要求は出したが完了しなかった」を後からログだけで見分けられるよう、
            # 要求（_begin_recording）と対で完了も残す
            logger.info("録音開始完了")
            return
        logger.warning("録音を開始できませんでした")
        # 開始失敗時は、この録音セッションのストリーミング接続・送出フックも確実に破棄する。
        # 残すと chunk_callback（= 古い streamer.send）が次回録音の音声を前回の Deepgram
        # WebSocket へ送ってしまう。ハング復帰（_monitor_loop）と同じ不変条件に揃える。
        with self._state_lock:
            self._recording_slot = None
            streamer = self._active_streamer
            self._active_streamer = None
            self._active_context = None
        self._recorder.set_chunk_callback(None)
        if streamer is not None:
            streamer.cancel()  # 受信ループ停止・WebSocket close
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
            context = self._active_context  # 録音開始時に確定した処理コンテキスト
            self._recording_slot = None
            self._auto_enter = False
            self._active_streamer = None
            self._active_context = None
            self._outstanding += 1  # 音声確定前から「変換中」を表示する
            # ダブルタップ待ちが残っていれば破棄（failsafe 等の別経路からの停止に備える）
            if self._pending_tap_timer is not None:
                self._pending_tap_timer.cancel()
                self._pending_tap_timer = None

        # ストリーミング送出はここでは外さない。外すと離鍵から stream.stop() 完了までの
        # 末尾（最後の 2〜3 文字）が streamer に届かない。録音部の _do_stop が末尾まで送ってから
        # 外し、その後で確定音声を渡すので、finish() はワーカー上で末尾を含めて確定できる

        # 操作音（停止）。下げたメディア音量を戻す（設定を録音中に OFF にしても確実に戻すため
        # restore は無条件で呼ぶ）。いずれも撃ちっぱなし。
        if self._config.get("sound_effects_enabled", True):
            sound_fx.play("stop")
        media_ducker.restore()

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
                    context=context,
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
                    # 万一コールバック経路が二重発火しても未完了数を負数にしない
                    # （表示が「変換中」に張り付くのを防ぐ防御。本来は録音側で exactly once）
                    self._outstanding = max(0, self._outstanding - 1)
                self._emit_state()

    def _process_task(self, task: TranscriptionTask) -> None:
        """ストリーミング確定 →（空なら）正規化 → VAD → API → テキスト挿入を実行する。

        設定のライブ値は読まず、録音開始時に確定した task.context のみを使う
        （キュー滞留中に設定が変わっても別プロバイダーへ送らないため）。
        """
        ctx: TaskContext = task.context
        # --- ストリーミング経路: Deepgram の確定テキストを受け取って貼り付け ---
        streamer = task.streamer
        if streamer is not None:
            logger.info(
                f"文字起こし要求 経路=streaming backend={ctx.slot.backend} "
                f"model={ctx.slot.api_model} 音声={len(task.audio_data) / SAMPLE_RATE:.2f}s"
            )
            try:
                streamed = streamer.finish()
            except Exception as e:
                logger.warning(f"ストリーミング確定でエラー、REST にフォールバック: {e}")
                streamed = ""
            if streamed:
                total_ms = (time.perf_counter() - task.timestamp) * 1000
                logger.info(f"ストリーミング確定: {len(streamed)} 文字 ({total_ms:.0f}ms)")
                # 貼り付け前の LLM テキスト整形（失敗時は原文のまま）
                streamed = self._maybe_format(streamed, ctx)
                # 実績を集計（貼り付け後のローカル処理なので遅延に影響しない）
                self._record_stats(streamed, task.audio_data)
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

        slot = ctx.slot

        # 音量正規化（ゲイン上限 +20dB。ノイズフロアの過剰増幅 = 幻覚を防ぐ）
        if ctx.volume_normalize:
            try:
                audio = preprocess_audio(audio, sample_rate=SAMPLE_RATE)
            except Exception as e:
                logger.warning(f"音声前処理でエラー、原音を使用: {e}")

        # 長文の分割並列送信（既定オン）。無音区間で区切り API へ並列送信して待ち時間を短縮する。
        # 区切りは無音の中だけなので語の途中では切れない。VAD 有効・長文時のみ発動する
        vad_on = ctx.vad_on
        split_on = ctx.split_on
        segments = None
        if vad_on and split_on and duration >= _SPLIT_MIN_SEC:
            segments = self._vad.segment(audio)

        if segments and len(segments) >= 2:
            logger.info(f"長文を {len(segments)} 分割して並列送信します")
            text = self._transcribe_parallel(slot, segments)
            if text is None:
                # 一部セグメントの失敗（429 等）→ 全体 1 本送信にフォールバック（部分欠落を防ぐ）。
                logger.warning("分割送信に失敗したため全体送信にフォールバックします")
                logger.info(
                    f"文字起こし要求 経路=rest(全体) backend={slot.backend} "
                    f"model={slot.api_model} 音声={len(audio) / SAMPLE_RATE:.2f}s"
                )
                text = slot.transcriber.transcribe(audio)
        else:
            # VAD: 発話がなければ API に送らない（幻覚と無駄コストの防止）。
            # 推論 1 回で発話判定と無音圧縮（前後トリミング + 発話間の長い無音の短縮）を行う
            if vad_on:
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

            logger.info(
                f"文字起こし要求 経路=rest backend={slot.backend} "
                f"model={slot.api_model} 音声={len(audio) / SAMPLE_RATE:.2f}s"
            )
            text = slot.transcriber.transcribe(audio)
        if not text:
            logger.info("文字起こし結果が空でした")
            return

        total_ms = (time.perf_counter() - task.timestamp) * 1000
        logger.info(
            f"文字起こし完了: 音声 {duration:.1f}s → {len(text)} 文字 ({total_ms:.0f}ms)"
        )

        # 貼り付け前の LLM テキスト整形（スロットで有効な場合のみ・失敗時は原文）
        text = self._maybe_format(text, ctx)

        # 実績を集計（貼り付け後のローカル処理なので遅延に影響しない）
        self._record_stats(text, task.audio_data)

        # テキスト挿入（ワーカースレッド上で実行し UI スレッドを塞がない）
        self._insert_and_enter(text, task.auto_enter)

    def _transcribe_parallel(self, slot: HotkeySlot, segments: list) -> Optional[str]:
        """各セグメントを並列に文字起こしし、index 昇順に結合して返す。

        1 つでも失敗したら None を返す（呼び出し側が全体 1 本送信にフォールバックする）。
        httpx.Client はスレッドセーフなため transcriber を共有してよい。同時数は最大 4。

        Args:
            slot: 使用するホットキースロット（transcriber を共有）
            segments: 分割済みセグメント（index 昇順）

        Returns:
            結合済みテキスト。1 つでも失敗したら None
        """
        results: list = [None] * len(segments)
        errors: list = []

        def _do(index: int, seg) -> None:
            try:
                results[index] = slot.transcriber.transcribe(seg)
            except Exception as e:  # フォールバック判断のため全例外を集約する
                errors.append(e)

        max_workers = min(4, len(segments))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="split") as ex:
            for future in [ex.submit(_do, i, seg) for i, seg in enumerate(segments)]:
                future.result()  # 例外は _do 内で捕捉済み

        if errors:
            logger.warning(f"分割送信で {len(errors)} 件のセグメントが失敗: {errors[0]}")
            return None
        return join_segments([r for r in results if r])

    def _maybe_format(self, text: str, ctx: TaskContext) -> str:
        """
        録音開始時のコンテキストで整形が有効なら LLM テキスト整形を適用する（ワーカースレッド上）。

        format_text は失敗時に必ず原文を返すため、ここでは例外処理は不要。
        整形可否・モデル・プロンプトはライブ設定でなく snapshot（ctx）を使う。

        Args:
            text: 文字起こし確定テキスト
            ctx: 録音開始時に確定した処理コンテキスト

        Returns:
            整形後テキスト。整形が無効・失敗時は原文そのまま
        """
        if not ctx.slot.format_enabled:
            return text
        return text_formatter.format_text(
            text,
            ctx.format_model,
            prompt=ctx.format_auto_prompt,
        )

    def _record_stats(self, text: str, audio) -> None:
        """音声入力 1 回分を実績に記録する（録音長は生の録音バッファ長から算出）。"""
        if not text:
            return
        try:
            recording_seconds = len(audio) / SAMPLE_RATE
            self._stats.record_session(len(text), recording_seconds)
        except Exception as e:
            # 実績集計の失敗で音声入力本体を止めない（あくまで付随機能）
            logger.warning(f"実績の集計に失敗: {e}")

    def _apply_replacements(self, text: str) -> str:
        """
        ユーザー辞書の確定置換を最終テキストに適用する（ワーカースレッド上）。

        有効・置換元が空でない行を登録順に単純置換する（部分一致。語境界は見ない）。
        API を通さないローカル処理なので音声入力に遅延を足さない。

        Args:
            text: 整形まで終わった確定テキスト

        Returns:
            置換適用後のテキスト（ルールが無ければ原文そのまま）
        """
        result = text
        for rule in self._config.get("replacements", []) or []:
            # 設定ファイルが手編集されていても落ちないよう型を緩く扱う
            if not isinstance(rule, dict) or not rule.get("enabled", True):
                continue
            src = rule.get("from", "")
            if src:
                result = result.replace(src, rule.get("to", ""))
        return result

    def _insert_and_enter(self, text: str, auto_enter: bool) -> None:
        """テキストを貼り付け、ダブルタップ時は遅延後に Enter を送る（ワーカースレッド上）。"""
        # 数字表記を半角アラビア数字へ正規化（全角→半角・漢数字→算用数字・助数詞つき単独漢数字）。
        # ストリーミング/REST 両経路の確定テキストが必ずここを通る単一の適用点。
        # ユーザー辞書置換より前に行い、置換ルールは正規化後のテキストに対して効かせる。
        text = numeral_normalizer.normalize(
            text,
            enabled=bool(self._config.get("numeral_normalize_enabled", True)),
            convert_counter=bool(self._config.get("numeral_convert_counter", True)),
            protect_words=self._config.get("numeral_protect_words", None),
        )
        # ユーザー辞書の確定置換を貼り付け直前に適用（履歴にも置換後を残す）
        text = self._apply_replacements(text)
        # 貼り付けに失敗しても履歴から救出できるよう、貼り付け前に記録する
        self._history.add(text)
        # 本文は残さない（文字数だけ）。「貼り付けたのに入らない」を切り分けるため対で記録する
        logger.info(f"貼り付け実行 ({len(text)} 文字)")
        pasted = self._input_handler.insert_text(text)
        logger.info(f"貼り付け完了 (成功={pasted})")
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
                # Mac 版 handsFree と同じ判定（toggle 実効モード＝切替キー併用 or toggle 設定スロット）
                hands_free = self._recording_effective_mode == HotkeyMode.TOGGLE.value
            elif self._outstanding > 0:
                state = "transcribing"
                hands_free = False
            else:
                state = "idle"
                hands_free = False
        # HUD・トレイへ流す状態をそのまま残す（画面が固まったときに
        # 「どの状態で止まったか」をログだけで追えるようにする）
        logger.info(f"UI 状態: {state} (hands_free={hands_free})")
        # 着色情報を先に届けてから状態を切り替える（set_state の再描画時に色が確定しているように）
        self.hands_free_changed.emit(hands_free)
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
        old_handsfree = self._handsfree_transcriber
        self._slots = self._build_slots()
        # ハンズフリー用 EL(scribe_v1) も言語変更等に追随して作り直す
        self._handsfree_transcriber = self._build_handsfree_transcriber()
        # ハンズフリー切替キーもホットリロードで更新する
        self._handsfree_keys = self._parse_hotkey(self._config.get("handsfree_key", ""))

        # 旧トランスクライバは処理中タスクが使っている可能性があるため遅延 close
        for old in old_slots.values():
            timer = threading.Timer(_RETIRE_CLOSE_DELAY_SEC, old.transcriber.close)
            timer.daemon = True
            timer.start()
        # ハンズフリー用 EL も同様に遅延 close（処理中タスクが参照している可能性がある）
        handsfree_timer = threading.Timer(_RETIRE_CLOSE_DELAY_SEC, old_handsfree.close)
        handsfree_timer.daemon = True
        handsfree_timer.start()

        # HUD 表示の有効/無効・待機ピル常時表示を反映
        self._hud.enabled = bool(self._config.get("hud_enabled", True))
        self._hud.always_visible = bool(self._config.get("hud_always_visible", False))
        # サイドノッチ表示トグルを反映
        self._side_notch.set_enabled(bool(self._config.get("side_notch_enabled", True)))
        # 待機ピルのオン/オフを即座に画面へ反映する（次の状態変化を待たない）
        self._emit_state()

        # 履歴同期の設定変更（有効/無効・URL）を反映し、401 停止も解除する
        self._history_sync.apply_config()

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
                self._active_context = None
            # ハング時もストリーミング接続を確実に破棄する
            self._recorder.set_chunk_callback(None)
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

    def _maybe_show_onboarding(self) -> None:
        """初回起動なら初回セットアップウィンドウを表示する（Phase 5）。

        - 完了済み（did_complete_onboarding=True）→ 出さない。
        - 既存ユーザー（起動時に settings.yaml が既にあった）→ フラグを補完して出さない。
        - 完全な初回起動（ファイルが無かった）→ 表示する。
        閉じる/スキップ/完了のいずれでも onboarding_finished で完了フラグを立て、以後は出さない。
        """
        if self._config.get("did_complete_onboarding", False):
            return
        if getattr(self._config, "config_file_existed", True):
            # 既存ユーザー: いきなり出すと戸惑うので、フラグを補完して以後出さない
            self._config.save({"did_complete_onboarding": True})
            return
        self._show_onboarding()

    def _show_onboarding(self) -> None:
        """セットアップガイド（オンボーディング）を表示する。

        起動時の初回自動表示（_maybe_show_onboarding）と、設定「一般」の
        「セットアップガイドを開く」（show_onboarding）から呼ばれる（トレイの再表示口は
        撤去済み。ユーザー指示「起動時にだけ表示するようにして」＝トレイには置かない）。
        app を渡して体験ステップから整形オーバーライド・録音キーテストを操作できるようにする
        （ホットキー監視は __init__ で既に起動済みなので、体験中もそのまま録音できる）。
        """
        from .ui.onboarding_window import OnboardingWindow

        win = OnboardingWindow(self._config, app=self)
        win.onboarding_finished.connect(self._on_onboarding_finished)
        self._onboarding_window = win
        win.show()
        win.raise_()
        win.activateWindow()
        self._platform.bring_to_front(win)

    def _on_onboarding_finished(self) -> None:
        """オンボーディング完了/スキップ時に完了フラグを保存する。"""
        # 整形体験の一時オーバーライドが残っていたら必ず解除（体験ステップ上で閉じた場合の保険）
        self._practice_format_override = False
        self._config.save({"did_complete_onboarding": True})

    def set_practice_format_override(self, enabled: bool) -> None:
        """オンボーディング整形体験ステップの間だけ整形を強制 ON にする（保存しない一時フラグ）。"""
        self._practice_format_override = bool(enabled)

    def set_hotkey_test(
        self, enabled: bool,
        callback: Optional[Callable[[Optional[int]], None]] = None,
    ) -> None:
        """オンボーディングの録音キーテスト／マイクテストモードを ON/OFF する。

        ON の間は録音を開始せず、録音キーの押下/離鍵で押されているスロット ID（無ければ None）を
        callback へ渡す。callback は listener スレッドから呼ばれるため、UI 側は Qt シグナル経由で
        メインスレッドに受け渡すこと。マイクテスト中は callback=None（録音を止めるだけ）で使う。
        """
        self._hotkey_test_active = bool(enabled)
        self._hotkey_test_callback = callback if enabled else None

    def _report_hotkey_test(self) -> None:
        """録音キーテスト中に、現在押されている録音キーのスロット（無ければ None）を報告する。"""
        matched: Optional[int] = None
        for slot_id, slot in self._slots.items():
            if self._slot_matches(slot):
                matched = slot_id
                break
        cb = self._hotkey_test_callback
        if cb is not None:
            try:
                cb(matched)
            except Exception as e:  # UI 側の例外で listener を殺さない
                logger.warning(f"録音キーテストのコールバックで例外: {e}")

    def show_onboarding(self) -> None:
        """セットアップガイドをもう一度表示する（設定画面の『セットアップガイドを開く』から）。"""
        self._show_onboarding()

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

    def _open_home(self) -> None:
        """ホーム（ダッシュボード）を開く。設定ウィンドウのホームページを前面化する
        （Mac 版はメインウィンドウのホームサイドバー項目。Windows は設定ウィンドウ内のホームページ）。"""
        # ホームページが未実装のビルドでも安全に設定ウィンドウを開く（select_home があれば選ぶ）
        select_home = getattr(self._settings_window, "select_home", None)
        if callable(select_home):
            select_home()
        self._open_settings()

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
        self._recorder.set_chunk_callback(None)
        if streamer is not None:
            streamer.cancel()

        listener = self._listener
        if listener is not None:
            try:
                listener.stop()
            except Exception as e:
                logger.warning(f"キーボードリスナー停止失敗: {e}")

        self._task_q.put(None)  # ワーカー停止
        self._history_sync.stop()
        # ストリームを閉じてマイクを OS に返す（ハング時も 1 秒で諦める。
        # プロセス終了時に OS がハンドルを回収するため実害はない）
        self._recorder.shutdown(timeout=1.0)
        QApplication.quit()
