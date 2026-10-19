"""
Google Meet 議事録ボット（Windows 版）— Mac 版 MeetBotService.swift の移植

会議 URL を渡すと、**専用プロファイルの Chrome を裏で起動して会議に参加し**、文字起こしを議事録として保存する。

**文字起こしの取り方は 2 つあり、設定で選ぶ**（どちらも消さない。本人がここを行き来している）:
- `meetCaptions`（**既定**）: Meet の内蔵字幕を読む。**話者名が取れる**。Chrome は --mute-audio で起こす。
- `apiSpeech`（Mac の localSpeech の代わり）: ボットの Chrome のプロセスの木だけの音を Process Loopback で拾い、
  API（Soniox / OpenAI ライブ）で文字起こしする。Windows はローカル認識を持たない（本人の決定）。
  話者名は会議画面から読めたときだけ添える。記録を始める前にライブ字幕を止める（同じ音を二重に拾わない）。

**ボットは保存スイッチに関係なく必ず議事録を保存する**（会議ごとに 1 ファイル）。
ライブ字幕の「字幕の文字起こしを保存」スイッチはここでは見ない。

**参加の試行には世代番号を振る**。退出や再参加のあとに古い試行の続きが戻ってきても、世代が違えば捨てる。
処理は 1 本のボット用スレッドで「起動 → 参加 → 記録の見張り」を順に回す（Chrome への命令は必ず上限時間つき）。
"""

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from ...utils.logger import get_logger
from ...utils.paths import data_dir
from .. import settings as cs
from ..transcript_recorder import TranscriptRecorder
from . import cdp, rules, scripts
from .settle import CaptionSettleTracker, Entry

logger = get_logger(__name__)

# DevTools のポート（普段使いの Chrome と衝突しない値・Mac と同じ）
DEVTOOLS_PORT = 9333
# 会議で表示されるボットの名前（ゲスト参加のときに入力される）
BOT_DISPLAY_NAME = "voicekey 議事録ボット"
# 見張りの間隔（秒）
SPEAKER_POLL = 0.8
CAPTION_POLL = 1.0
STATUS_POLL = 3.0
# 伸びが止まってから確定とみなすまで
SETTLE_SECONDS = 2.5
# 退出・終了のとき、音声の停止（最後の 1 文の確定）を待つ上限
STOP_WAIT_LIMIT = 3.0

# 状態
IDLE = "idle"
LAUNCHING = "launching"
JOINING = "joining"
WAITING = "waitingForApproval"
RECORDING = "recording"
ENDED = "ended"
FAILED = "failed"

# ログイン状態
LOGIN_UNKNOWN = "unknown"
LOGIN_CHECKING = "checking"
LOGGED_IN = "loggedIn"
LOGGED_OUT = "loggedOut"
LOGIN_WAITING = "waitingForLogin"
LOGIN_TITLES = {
    LOGIN_UNKNOWN: "未確認",
    LOGIN_CHECKING: "確認中…",
    LOGGED_IN: "ログイン済み",
    LOGGED_OUT: "ログインしていません",
    LOGIN_WAITING: "ログイン待ち",
}


def profile_dir() -> Path:
    """ボット専用の Chrome プロファイル（普段使いの Chrome を巻き込まない）。"""
    import os
    import sys

    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "voicekey" / "meetbot-profile"
    return data_dir() / "meetbot-profile"


def menu_title(state: str, detail: str) -> str:
    """状態の 1 行（Mac の MeetBotState.menuTitle と同じ文言）。"""
    if state == IDLE:
        return "参加していません"
    if state == LAUNCHING:
        return "ブラウザを準備中…"
    if state == JOINING:
        return f"参加の準備中 — {detail}"
    if state == WAITING:
        return "ホストの承認待ち"
    if state == RECORDING:
        return f"参加中 — {detail}"
    if state == ENDED:
        return f"終了 — {detail}"
    return f"エラー — {detail}"


def is_active_state(state: str) -> bool:
    return state in (LAUNCHING, JOINING, WAITING, RECORDING)


def needs_login(status: str) -> bool:
    """参加の前にログインへ案内すべきか（ログイン済みと分かっていない間は先にログインへ）。"""
    return status not in (LOGGED_IN, LOGIN_CHECKING)


class _Superseded(Exception):
    """退出・再参加で古くなった試行（表には出さない）。"""


class MeetBotService(QObject):
    """Meet に入って会議の文字起こしを議事録に残すボット。

    Signals:
        changed: 状態・行数・ログイン状態などが変わった（画面を描き直す）
        notified: 本人に知らせたいこと (本文, 議事録のパス or "")
        login_checked: ログイン状態を確かめた（設定へ保存するため・メインスレッドで受ける）
    """

    changed = Signal()
    notified = Signal(str, str)
    login_checked = Signal(bool)

    def __init__(self, config_manager=None, recorder: Optional[TranscriptRecorder] = None) -> None:
        super().__init__()
        self._config = config_manager
        self._lock = threading.RLock()
        self._generation = rules.JoinGeneration()
        self._recorder = recorder or TranscriptRecorder()
        self._recorder.on_write = self._handle_write
        self.state = IDLE
        self.detail = ""
        self.recorded_lines = 0
        self.save_error: Optional[str] = None
        self.current_transcript: Optional[Path] = None
        self.caption_warning: Optional[str] = None
        meet = cs.MeetBotConfig.from_config(config_manager) if config_manager is not None else cs.MeetBotConfig()
        self.login_status = (LOGGED_IN if meet.logged_in else LOGGED_OUT) if meet.login_checked_at else LOGIN_UNKNOWN
        self.login_checked_at = meet.login_checked_at
        self._chrome = None
        self._devtools: Optional[cdp.ChromeDevTools] = None
        self._capture = None
        self._stream = None
        self._closing = False
        self._meeting_tag = 0
        self._file_started = False
        self._settle = CaptionSettleTracker(SETTLE_SECONDS)
        self._meeting_name = "Google Meet"
        self._meeting_code: Optional[str] = None
        self.active_source = cs.MEET_SOURCE_CAPTIONS
        self._active_language = cs.LANG_JA
        self._active_recognizer = cs.RECOGNIZER_SONIOX
        self._speaker_now: Optional[str] = None
        self._speaker_for_segment: Optional[str] = None
        self._login_watch_id = 0
        # 会議の音声を拾い始める直前に呼ぶ（ライブ字幕を止めてもらう）
        self.on_will_start_recording: Optional[Callable[[], None]] = None
        # テストで差し替える部品
        self.cdp = cdp

    # --- 状態 ---

    @property
    def is_active(self) -> bool:
        return is_active_state(self.state)

    @property
    def is_closing(self) -> bool:
        return self._closing

    @property
    def status_line(self) -> str:
        """状態を 1 行で（保存失敗・字幕が取れていない・記録行数も含めて）。"""
        if self.state != RECORDING:
            return menu_title(self.state, self.detail)
        if self.save_error:
            return f"議事録を保存できません — {self.save_error}"
        if self.caption_warning:
            return f"{self.caption_warning} — {self.detail}"
        return f"記録中 {self.recorded_lines} 行 — {self.detail}"

    @property
    def login_title(self) -> str:
        return LOGIN_TITLES.get(self.login_status, "未確認")

    @staticmethod
    def is_available() -> bool:
        """Chrome が使えるか（メニュー・設定画面の有効/無効に使う）。"""
        return cdp.is_chrome_installed()

    def _set_state(self, state: str, detail: str = "") -> None:
        with self._lock:
            if self.state == state and self.detail == detail:
                return
            self.state = state
            self.detail = detail
        self.changed.emit()

    def _notify(self, message: str, transcript: Optional[Path] = None) -> None:
        self.notified.emit(message, str(transcript) if transcript else "")

    def _ensure_current(self, generation: int) -> None:
        if not self._generation.is_current(generation):
            raise _Superseded()

    def _sleep(self, generation: int, seconds: float) -> None:
        """世代が変わったらすぐ抜ける待ち。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._ensure_current(generation)
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        self._ensure_current(generation)

    # --- 公開操作（メインスレッドから呼ぶ） ---

    def join(self, url_string: str) -> None:
        """会議に参加して記録を始める。"""
        if self.is_active:
            return
        if self._closing:
            # 押したのに何も起きないと壊れたように見えるので理由を知らせる
            self._notify(rules.JOIN_WHILE_CLOSING)
            return
        if not cdp.is_chrome_installed():
            self._set_state(FAILED, "Google Chrome が必要です")
            return
        url = rules.normalized_meet_url(url_string)
        if url is None:
            self._set_state(FAILED, "Meet の会議 URL ではありません（meet.google.com/abc-defg-hij の形）")
            return
        meet = cs.MeetBotConfig.from_config(self._config) if self._config is not None else cs.MeetBotConfig()
        with self._lock:
            self._meeting_code = rules.meeting_code(url)
            self._meeting_name = self._meeting_code or "Google Meet"
            self.active_source = meet.source
            self._active_language = meet.language
            self._active_recognizer = meet.recognizer
            self.recorded_lines = 0
            self.save_error = None
            self.current_transcript = None
            self.caption_warning = None
            self._file_started = False
            self._settle = CaptionSettleTracker(SETTLE_SECONDS)
            generation = self._generation.begin()
            self._meeting_tag = generation
            self._login_watch_id += 1  # ログイン窓の見張りを止める
        logger.info(f"Meet ボット開始 (文字起こし={self.active_source}, 言語={self._active_language})")
        self._set_state(LAUNCHING)
        threading.Thread(target=self._join_thread, args=(url, generation), daemon=True, name="MeetBot").start()

    def leave(self) -> None:
        """会議から退出して記録を閉じる。"""
        if not self.is_active or self._closing:
            return
        logger.info("Meet ボット終了（退出）")
        was_recording = self.state == RECORDING
        self._finish(IDLE, "", leave_first=True, closing_reason="会議から退出しました" if was_recording else None)

    def show_login_window(self) -> None:
        """初回ログイン用に、見える Chrome を開く（Google のログインは本人にしかできない）。

        ウィンドウが閉じられたらログインできたかを確かめ、Chrome を畳む（参加するときは見えない Chrome で起こし直す）。
        ログイン画面は DevTools を経由せず URL を起動引数で渡して開く（途中で黙る段を作らない）。
        """
        if self.is_active:
            return
        if not cdp.is_chrome_installed():
            self._set_state(FAILED, "Google Chrome が必要です")
            return
        logger.info("ボット用ブラウザで Google のログイン画面を開く")
        with self._lock:
            self._login_watch_id += 1
            watch_id = self._login_watch_id
            previous, self._chrome = self._chrome, None
        if self.state == FAILED:
            self._set_state(IDLE)
        threading.Thread(target=self._login_thread, args=(previous, watch_id), daemon=True,
                         name="MeetBotLogin").start()

    def shutdown(self) -> None:
        """アプリ終了時の後片付け（Chrome を残さない・議事録を書き切る）。"""
        with self._lock:
            self._login_watch_id += 1
            self._flush_pending_captions()
            self._generation.invalidate()
            capture, stream, tools, chrome = self._detach()
        self._stop_audio(capture, stream)
        if tools is not None:
            tools.close()
        cdp.terminate_and_wait(chrome, limit=2.0)
        self._recorder.end_session()
        # キューに残った追記をプロセスと一緒に消さない
        self._recorder.wait_until_written()

    # --- 参加の流れ（ボット用スレッド） ---

    def _join_thread(self, url: str, generation: int) -> None:
        try:
            self._start_chrome_and_join(url, generation)
            self._record_loop(generation)
        except _Superseded:
            return
        except Exception as e:
            if not self._generation.is_current(generation):
                return
            message = str(e) or type(e).__name__
            logger.error(f"会議への参加に失敗: {message}")
            if isinstance(e, rules.MeetBotError) and e.kind == "notLoggedIn":
                self._set_login(False)
            was_recording = self.state == RECORDING
            self._finish(FAILED, message, closing_reason=message if was_recording else None)
            if was_recording:
                return
            if isinstance(e, rules.MeetBotError) and e.kind == "denied":
                self._notify("参加を断られました。ホストに承認してもらうか、会議の URL を確かめてください。")
            else:
                self._notify(f"会議に参加できませんでした: {message}")

    def _start_chrome_and_join(self, url: str, generation: int) -> None:
        # ログイン用に開いた見える Chrome が残っていれば閉じる（見える窓で会議に入らない）
        with self._lock:
            existing, self._chrome = self._chrome, None
            tools, self._devtools = self._devtools, None
        cdp.terminate_and_wait(existing)
        if tools is not None:
            tools.close()
        # 前回落ちたときの取り残し（ボット用プロファイルを使う Chrome だけ）を片付ける
        stale = self.cdp.terminate_stale_chrome(profile_dir())
        if stale:
            logger.info(f"取り残されていたボット用 Chrome を終了しました（{stale} 個）")
        self._ensure_current(generation)
        if self.cdp.is_answering(DEVTOOLS_PORT):
            raise rules.MeetBotError("portBusy", str(DEVTOOLS_PORT))
        self._ensure_current(generation)

        process = self.cdp.launch_chrome(
            DEVTOOLS_PORT, profile_dir(), headless=True,
            mute_audio=self.active_source == cs.MEET_SOURCE_CAPTIONS,
        )
        with self._lock:
            self._chrome = process
        threading.Thread(target=self._watch_chrome_exit, args=(process, generation), daemon=True,
                         name="MeetBotChrome").start()
        self.cdp.wait_for_devtools(DEVTOOLS_PORT, should_stop=lambda: not self._generation.is_current(generation))
        self._ensure_current(generation)

        # 未ログインのまま会議ページへ行くと紹介サイトへ飛ばされるだけなので、先に確かめる
        self._set_state(JOINING, "ログインを確認しています")
        logged_in = self._check_login()
        self._ensure_current(generation)
        if logged_in is False:
            raise rules.MeetBotError("notLoggedIn")
        self._deny_media_permissions()
        self._ensure_current(generation)

        socket_url = self.cdp.open_tab(url, DEVTOOLS_PORT)
        self._ensure_current(generation)
        tools = self.cdp.ChromeDevTools()
        tools.on_disconnect = lambda: self._devtools_disconnected(generation)
        tools.connect(socket_url)
        with self._lock:
            if not self._generation.is_current(generation):
                tools.close()
                raise _Superseded()
            self._devtools = tools

        self._set_state(JOINING, "会議を開いています")
        # ロビー画面が組み上がるまで待つ（DOM は非同期に描かれる）
        self._sleep(generation, 4.0)
        consent = self._try_eval(tools, scripts.CONSENT_SCRIPT)
        if isinstance(consent, dict) and consent.get("clicked"):
            logger.info("同意画面を閉じました")
            self._sleep(generation, 3.0)
        left = self._try_eval(tools, scripts.LEFT_MEETING_PAGE_SCRIPT)
        if left is True:
            raise rules.MeetBotError("notLoggedIn")
        self._ensure_current(generation)

        # 参加ボタンは入室待ちの中で押す（ロビーの組み上がりが遅いときは数秒おきに押し直す）
        self._set_state(JOINING, "参加ボタンを押しています")
        self._wait_until_in_meeting(tools, generation)

        title = self._try_eval(tools, "document.title")
        if isinstance(title, str) and title:
            with self._lock:
                self._meeting_name = title.replace(" - Google Meet", "")
        unmuted = self._try_eval(tools, scripts.ENSURE_MUTED_SCRIPT)
        if isinstance(unmuted, (int, float)) and unmuted > 0:
            logger.info(f"会議に入った時点でオンだったマイク/カメラを切りました（{int(unmuted)} 個）")
        self._ensure_current(generation)

        captions_on = True
        if self.active_source == cs.MEET_SOURCE_CAPTIONS:
            self._set_state(JOINING, "字幕をオンにしています")
            captions_on = self._enable_captions(tools, generation)
            self._ensure_current(generation)
            if captions_on:
                result = self._try_eval(tools, scripts.caption_language_script(self._active_language))
                logger.info(f"字幕の言語: {result}")
                self._ensure_current(generation)
        else:
            self._start_recognition(generation)
            self._ensure_current(generation)
        self._start_meeting_file()
        self._set_state(RECORDING, self._meeting_name)
        logger.info("Meet ボット入室・記録開始")
        if not captions_on:
            self._notify("会議に参加しましたが、Meet の字幕をオンにできませんでした。議事録に何も残らない可能性があります。")
        else:
            self._notify(f"会議に参加しました（{self._meeting_name}）。議事録を記録しています。")

    @staticmethod
    def _try_eval(tools, expression: str):
        try:
            return tools.evaluate(expression)
        except Exception:
            return None

    def _wait_until_in_meeting(self, tools, generation: int) -> None:
        """会議に入れるまで待つ（承認待ちは 10 分に延ばす・ロビーでは参加ボタンを押し直す・固まったら諦める）。"""
        start = time.monotonic()
        deadline = start + 90
        waiting_notified = False
        retry = rules.JoinRetry()
        timeouts = rules.TimeoutStreak()
        while True:
            self._ensure_current(generation)
            status = None
            try:
                raw = tools.evaluate(scripts.PAGE_STATUS_SCRIPT)
                if isinstance(raw, dict):
                    status = rules.page_status(bool(raw.get("inMeeting")), str(raw.get("text") or ""))
                timeouts.record(False)
            except cdp.ChromeDevToolsError as e:
                if e.kind == "timedOut":
                    if timeouts.record(True):
                        raise rules.MeetBotError("chromeNotResponding")
                else:
                    # JS の例外などは「応答はある」ので数えない（画面の切り替わり中に起きる）
                    timeouts.record(False)
            self._ensure_current(generation)
            if status == rules.IN_MEETING:
                return
            if status == rules.WAITING_APPROVAL:
                if not waiting_notified:
                    waiting_notified = True
                    deadline = start + 600
                    self._set_state(WAITING)
                    self._notify("ホストの承認を待っています。会議の画面でボットの参加を承認してください。")
            elif status == rules.DENIED:
                raise rules.MeetBotError("denied")
            elif status == rules.REMOVED:
                raise rules.MeetBotError("removed")
            elif status == rules.ENDED:
                raise rules.MeetBotError("callEnded")
            elif status == rules.NOT_FOUND:
                raise rules.MeetBotError("meetingNotFound")
            elif status == rules.OTHER:
                now = time.monotonic()
                if not waiting_notified and retry.should_press(rules.OTHER, now):
                    if retry.attempts > 0:
                        self._set_state(JOINING, f"参加ボタンを押し直しています（{retry.attempts + 1} 回目）")
                    clicked = None
                    try:
                        result = tools.evaluate(scripts.join_script(BOT_DISPLAY_NAME))
                        clicked = result.get("clicked") if isinstance(result, dict) else None
                        timeouts.record(False)
                    except cdp.ChromeDevToolsError as e:
                        if e.kind == "timedOut" and timeouts.record(True):
                            raise rules.MeetBotError("chromeNotResponding")
                    self._ensure_current(generation)
                    retry.record_press(clicked is not None, time.monotonic())
                    logger.info(f"参加操作（{retry.attempts} 回目）: {'押せた' if clicked else 'ボタンが見つからない'}")
                    self._set_state(JOINING, "会議への入室を待っています")
            if time.monotonic() >= deadline:
                raise rules.join_timeout_error(waiting_notified, retry.button_found)
            self._sleep(generation, 2.0)

    def _enable_captions(self, tools, generation: int) -> bool:
        """Meet の字幕をオンにする（既にオンなら触らない。押した後も読み直して確かめる・4 回まで）。"""
        shortcut_tried = False
        for _ in range(4):
            if not self._generation.is_current(generation):
                return False
            raw = self._try_eval(tools, scripts.CAPTION_STATE_SCRIPT)
            if isinstance(raw, dict):
                buttons = [(str(b.get("label") or ""), b.get("pressed")) for b in (raw.get("buttons") or [])
                           if isinstance(b, dict)]
                decision, label = rules.caption_toggle(buttons, bool(raw.get("regionVisible")), shortcut_tried)
                if decision == "alreadyOn":
                    logger.info("Meet の字幕はオンです")
                    return True
                if decision == "click" and label:
                    logger.info("字幕ボタンを押します")
                    self._try_eval(tools, scripts.click_caption_button_script(label))
                elif decision == "shortcut":
                    logger.info("字幕ボタンが見つからないのでショートカットを 1 回だけ送ります")
                    shortcut_tried = True
                    self._try_eval(tools, scripts.CAPTION_SHORTCUT_SCRIPT)
                else:
                    logger.error("Meet の字幕の状態が分かりません")
                    return False
            try:
                self._sleep(generation, 1.5)
            except _Superseded:
                return False
        return False

    # --- 記録中の見張り（ボット用スレッド） ---

    def _record_loop(self, generation: int) -> None:
        presence = rules.MeetingPresenceWatch()
        status_timeouts = rules.TimeoutStreak()
        caption_watch = rules.CaptionPresenceWatch(time.monotonic())
        next_status = time.monotonic() + STATUS_POLL
        next_caption = time.monotonic() + CAPTION_POLL
        next_speaker = time.monotonic() + SPEAKER_POLL
        while self._generation.is_current(generation):
            tools = self._devtools
            if tools is None:
                return
            now = time.monotonic()
            if now >= next_status:
                next_status = now + STATUS_POLL
                if not self._poll_status(tools, generation, presence, status_timeouts):
                    return
            if self.active_source == cs.MEET_SOURCE_CAPTIONS and now >= next_caption:
                next_caption = now + CAPTION_POLL
                self._poll_captions(tools, generation, caption_watch)
            if self.active_source == cs.MEET_SOURCE_API and now >= next_speaker:
                next_speaker = now + SPEAKER_POLL
                self._poll_speaker(tools)
            time.sleep(0.1)

    def _poll_status(self, tools, generation: int, presence, timeouts) -> bool:
        """会議画面かを 1 回確かめる（外された・終わったら閉じる）。続けるなら True。"""
        try:
            raw = tools.evaluate(scripts.PAGE_STATUS_SCRIPT)
            timeouts.record(False)
        except cdp.ChromeDevToolsError as e:
            if not self._generation.is_current(generation):
                return False
            if e.kind == "timedOut" and timeouts.record(True):
                self._fail_unexpectedly(str(rules.MeetBotError("chromeNotResponding")))
                return False
            return True
        if not self._generation.is_current(generation) or not isinstance(raw, dict):
            return self._generation.is_current(generation)
        status = rules.page_status(bool(raw.get("inMeeting")), str(raw.get("text") or ""))
        verdict = presence.observe(status)
        if verdict == rules.REMOVED:
            self._end_meeting("会議から外されました")
            return False
        if verdict == rules.ENDED:
            self._end_meeting("会議が終了しました")
            return False
        return True

    def _poll_captions(self, tools, generation: int, caption_watch) -> None:
        """字幕を 1 回読んで、確定した発言を記録する（確定の判定は CaptionSettleTracker）。"""
        payload = self._try_eval(tools, scripts.CAPTION_SCRIPT)
        if not isinstance(payload, dict) or not self._generation.is_current(generation):
            return
        now = time.monotonic()
        rows = [r for r in (payload.get("entries") or []) if isinstance(r, dict)]
        # 本文ではなく「字幕がオンか」で見る（無言の会議で、字幕が出ているのに警告しないため）
        event = caption_watch.observe(bool(payload.get("captionsOn")) or bool(rows), now)
        if event == "warn":
            self.caption_warning = rules.CAPTION_MISSING
            logger.error("Meet の字幕が 60 秒以上読めていません")
            self._notify(rules.CAPTION_MISSING_NOTICE)
            self.changed.emit()
        elif event == "recovered":
            self.caption_warning = None
            self.changed.emit()
        with self._lock:
            if not self._generation.is_current(generation):
                return
            if payload.get("source") == "region":
                text = " ".join(str(r.get("text") or "") for r in rows)
                lines = self._settle.ingest_region(text, now)
            else:
                lines = self._settle.ingest(
                    [Entry(str(r.get("speaker") or ""), str(r.get("text") or "")) for r in rows], now)
        for line in lines:
            self._record_line(line.text, line.speaker)

    def _poll_speaker(self, tools) -> None:
        value = self._try_eval(tools, scripts.ACTIVE_SPEAKER_SCRIPT)
        if isinstance(value, str) and value.strip():
            self._speaker_now = value.strip()

    # --- 議事録 ---

    def _context(self) -> str:
        return f"Google Meet / {self._meeting_name}"

    def _language_label(self) -> str:
        name = cs.LANGUAGE_LABELS.get(self._active_language, "日本語")
        if self.active_source == cs.MEET_SOURCE_CAPTIONS:
            return f"Meet の字幕（{name}）"
        model = cs.RECOGNIZER_LABELS.get(self._active_recognizer, "")
        return f"{name}（API: {model.split('（')[0]}）"

    def _start_meeting_file(self) -> None:
        """この会議の議事録ファイルを作る（1 会議 1 回。保存スイッチに関係なく必ず作る）。"""
        with self._lock:
            if self._file_started:
                return
            self._file_started = True
            tag = self._meeting_tag
        self._recorder.begin_meeting(self._meeting_code, self._context(), self._language_label(), tag=tag)

    def _record_line(self, text: str, speaker: Optional[str]) -> None:
        """1 件を議事録へ書く（本文はログに出さない）。"""
        self._start_meeting_file()
        self._recorder.record(text, speaker=speaker, context=self._context(), language=self._language_label())

    def _flush_pending_captions(self) -> None:
        for line in self._settle.flush():
            self._record_line(line.text, line.speaker)

    def _handle_write(self, outcome, tag: int) -> None:
        """議事録の書き込み結果を受ける（記録用スレッドから。前の会議の結果は取り違えない）。"""
        if tag != self._meeting_tag:
            return
        kind, value, lines = outcome
        if kind == "written":
            self.current_transcript = value
            self.recorded_lines = lines
            self.save_error = None
        else:
            # 失敗は会議中に 1 回だけ知らせる（毎行ごとに通知を出さない）
            if self.save_error is None:
                self._notify(f"議事録を保存できません: {value}")
            self.save_error = str(value)
        self.changed.emit()

    # --- 会議の音声を API で文字起こし ---

    def _start_recognition(self, generation: int) -> None:
        """ボットの Chrome のプロセスの木だけの音を拾って、API で文字起こしを始める。"""
        from ..audio_capture import CaptureController, CaptureUnavailable
        from ..live_stream import make_stream

        chrome = self._chrome
        if chrome is None:
            raise rules.MeetBotError("protocol", "Chrome のプロセスが見つかりません")
        # 同じ音を字幕とボットの両方で拾わないよう、先に字幕を止めてもらう
        callback = self.on_will_start_recording
        if callback is not None:
            try:
                callback()
            except Exception:
                pass
        self._set_state(JOINING, "音声認識を準備しています")
        stream = make_stream(self._active_recognizer, False, self._active_language)
        stream.on_confirmed = lambda source, _t: self._on_final(source)
        stream.on_live = lambda _s, _t: self._on_partial()
        stream.on_usage = lambda seconds: self._record_usage(stream, seconds)
        stream.on_give_up = lambda reason: self._fail_unexpectedly(f"文字起こしを続けられません（{reason}）")
        if not stream.start():
            raise rules.MeetBotError(
                "noKey", f"{stream.PROVIDER} の API キーが未設定です（設定 › API キー）")
        capture = CaptureController(on_audio=stream.send, fixed_pid=chrome.pid)
        capture.on_failed = lambda reason: self._fail_unexpectedly(reason)
        try:
            capture.start()
        except CaptureUnavailable as e:
            stream.stop(timeout=1.0)
            raise rules.MeetBotError(
                "noCapture", f"{e}。「Meet の字幕を使う」に切り替えてください")
        with self._lock:
            if not self._generation.is_current(generation):
                stale = True
            else:
                stale = False
                self._capture = capture
                self._stream = stream
        if stale:
            capture.stop()
            stream.stop(timeout=1.0)
            raise _Superseded()
        logger.info(f"会議音声の API 文字起こしを開始 pid={chrome.pid} 言語={self._active_language}")

    @staticmethod
    def _record_usage(stream, seconds: float) -> None:
        try:
            from ...core import api_usage

            api_usage.record_audio(stream.KEY_ITEM, stream.MODEL, seconds, purpose="meetingTranscription")
        except Exception:
            pass

    def _on_partial(self) -> None:
        # 発話中に見えている話者を覚えておく（確定時にはもう切り替わっていることがある）
        if self._speaker_now:
            self._speaker_for_segment = self._speaker_now

    def _on_final(self, text: str) -> None:
        """確定した文字起こしを議事録へ書く（退出の後片付け中に届いた最後の 1 文も残す）。"""
        speaker = self._speaker_for_segment or self._speaker_now
        self._speaker_for_segment = None
        self._record_line(text, speaker)

    # --- 終わりに気づく ---

    def _watch_chrome_exit(self, process, generation: int) -> None:
        try:
            process.wait()
        except Exception:
            return
        if self._generation.is_current(generation) and self.is_active:
            self._fail_unexpectedly("ブラウザが終了しました")

    def _devtools_disconnected(self, generation: int) -> None:
        if self._generation.is_current(generation) and self.is_active:
            self._fail_unexpectedly("ブラウザとの接続が切れました")

    def _end_meeting(self, reason: str) -> None:
        logger.info(f"会議の終了を検出: {reason}")
        self._finish(ENDED, reason, closing_reason=reason)

    def _fail_unexpectedly(self, reason: str) -> None:
        if not self.is_active or self._closing:
            return
        was_recording = self.state == RECORDING
        logger.error(f"Meet ボットが止まりました: {reason}")
        self._finish(FAILED, reason, closing_reason=reason if was_recording else None)
        if not was_recording:
            self._notify(f"会議に参加できませんでした: {reason}")

    # --- 後片付け ---

    def _detach(self):
        """Chrome・接続・音声を手放す（すぐ再参加しても新しい Chrome を巻き込まないよう、先に参照を外す）。"""
        capture, self._capture = self._capture, None
        stream, self._stream = self._stream, None
        tools, self._devtools = self._devtools, None
        chrome, self._chrome = self._chrome, None
        if tools is not None:
            tools.on_disconnect = None
        return capture, stream, tools, chrome

    @staticmethod
    def _stop_audio(capture, stream) -> None:
        """音声の取り込みと文字起こしを止める（最後の 1 文の確定を上限 3 秒まで待つ）。"""
        if capture is not None:
            try:
                capture.stop()
            except Exception:
                pass
        if stream is not None:
            try:
                stream.stop(timeout=STOP_WAIT_LIMIT)
            except Exception:
                pass

    def _finish(self, new_state: str, detail: str, leave_first: bool = False,
                closing_reason: Optional[str] = None) -> None:
        """記録を閉じて Chrome・音声を畳み、状態を移す（どのスレッドから呼んでもよい・1 回だけ効く）。"""
        with self._lock:
            if self._closing:
                return
            self._flush_pending_captions()
            self._generation.invalidate()
            capture, stream, tools, chrome = self._detach()
            self._closing = True

        def close_all() -> None:
            # 最後の 1 文は停止処理（終端への応答）の中で確定するので、議事録を閉じる前に待つ
            self._stop_audio(capture, stream)
            if tools is not None:
                if leave_first:
                    # 退出ボタンを押して、Meet が離脱を相手に伝える間だけ待ってから閉じる
                    try:
                        tools.evaluate(scripts.LEAVE_SCRIPT, timeout=1.5)
                        time.sleep(1.0)
                    except Exception:
                        pass
                tools.close()
            cdp.terminate_and_wait(chrome, limit=3.0)
            self._close_session(new_state, detail, closing_reason)

        if threading.current_thread() is threading.main_thread():
            threading.Thread(target=close_all, daemon=True, name="MeetBotClose").start()
        else:
            close_all()

    def _close_session(self, new_state: str, detail: str, closing_reason: Optional[str]) -> None:
        """議事録を閉じて状態を移す（終了通知は書き込みが全部終わった後の集計で出す）。"""
        self._speaker_now = None
        self._speaker_for_segment = None
        tag = self._meeting_tag
        file_started = self._file_started

        def on_closed(summary) -> None:
            if tag == self._meeting_tag:
                self.recorded_lines = summary.lines
                self.changed.emit()
            if closing_reason:
                message = rules.closing_message(closing_reason, file_started, summary.lines, summary.error)
                self._notify(message, summary.path if file_started else None)

        self._recorder.end_session(on_closed)
        with self._lock:
            self._closing = False
        self._set_state(new_state, detail)

    # --- ログイン ---

    def _check_login(self, while_watching: bool = False) -> Optional[bool]:
        """ボット用ブラウザが Google にログインしているかを Cookie で確かめる（確かめられなければ None）。"""
        previous = self.login_status
        if not while_watching:
            self.login_status = LOGIN_CHECKING
            self.changed.emit()
        browser = None
        try:
            browser = self.cdp.ChromeDevTools()
            browser.connect(self.cdp.browser_socket_url(DEVTOOLS_PORT))
            result = browser.send("Storage.getCookies")
            logged_in = rules.is_logged_in(result.get("cookies") or [], time.time())
        except Exception as e:
            self.login_status = previous
            self.changed.emit()
            logger.error(f"ログイン状態を確かめられません: {e}")
            return None
        finally:
            if browser is not None:
                browser.close()
        if not while_watching or logged_in:
            self._set_login(logged_in)
        else:
            self.login_status = LOGIN_WAITING
            self.changed.emit()
        logger.info(f"ボット用ブラウザのログイン状態: {'ログイン済み' if logged_in else '未ログイン'}")
        return logged_in

    def _set_login(self, logged_in: bool) -> None:
        self.login_status = LOGGED_IN if logged_in else LOGGED_OUT
        self.login_checked_at = datetime.now().isoformat(timespec="minutes")
        self.changed.emit()
        # 設定への保存はメインスレッドで（ConfigManager の保存は UI と同じスレッドに寄せる）
        self.login_checked.emit(logged_in)

    def save_login(self, logged_in: bool) -> None:
        """ログイン状態を設定へ保存する（次回の起動でも設定画面に出せるように。メインスレッドから）。"""
        if self._config is None:
            return
        try:
            self._config.save({"meet_bot": {"logged_in": bool(logged_in),
                                            "login_checked_at": self.login_checked_at}})
        except Exception as e:
            logger.warning(f"ログイン状態を保存できませんでした: {e}")

    def _deny_media_permissions(self) -> None:
        """Chrome にカメラとマイクを使わせない（失敗しても参加は続ける＝起動引数側で拒否が効いている）。"""
        browser = None
        try:
            browser = self.cdp.ChromeDevTools()
            browser.connect(self.cdp.browser_socket_url(DEVTOOLS_PORT))
            for name in ("camera", "microphone"):
                try:
                    browser.send("Browser.setPermission", {
                        "permission": {"name": name}, "setting": "denied", "origin": "https://meet.google.com"})
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if browser is not None:
                browser.close()

    def _login_thread(self, previous, watch_id: int) -> None:
        """ログイン用の見える Chrome を開き、閉じられるまで見張る（30 分で打ち切る）。"""
        cdp.terminate_and_wait(previous)
        self.cdp.terminate_stale_chrome(profile_dir())
        if watch_id != self._login_watch_id:
            return
        try:
            process = self.cdp.launch_chrome(DEVTOOLS_PORT, profile_dir(), headless=False,
                                             initial_url="https://accounts.google.com/")
        except Exception as e:
            logger.error(f"ログイン用の Chrome を起動できません: {e}")
            self._set_state(FAILED, f"ブラウザを開けません: {e}")
            return
        with self._lock:
            self._chrome = process
        if self.login_status != LOGGED_IN:
            self.login_status = LOGIN_WAITING
            self.changed.emit()
        saw_page = False
        rounds = 0
        deadline = time.monotonic() + 30 * 60
        try:
            while watch_id == self._login_watch_id and process.poll() is None and time.monotonic() < deadline:
                time.sleep(2.0)
                try:
                    pages = self.cdp.list_page_urls(DEVTOOLS_PORT)
                except Exception:
                    continue
                if not pages:
                    if saw_page:
                        break
                    continue
                saw_page = True
                rounds += 1
                # ログインが済んだら設定画面にすぐ出す（6 秒ごと・済んだら確認をやめる）
                if rounds % 3 == 0 and self.login_status != LOGGED_IN:
                    self._check_login(while_watching=True)
            if watch_id != self._login_watch_id:
                return
            if process.poll() is None:
                self._check_login()
                cdp.terminate_and_wait(process)
            with self._lock:
                if self._chrome is process:
                    self._chrome = None
            logger.info("ログイン用の Chrome を閉じました")
        finally:
            # どの抜け方でも「ログイン待ち」を残さない（最後の確認ができなかったときは前回の結果に戻す）
            if self.login_status == LOGIN_WAITING:
                meet = cs.MeetBotConfig.from_config(self._config) if self._config is not None else cs.MeetBotConfig()
                self.login_status = (LOGGED_IN if meet.logged_in else LOGGED_OUT) if meet.login_checked_at \
                    else LOGIN_UNKNOWN
                self.changed.emit()
