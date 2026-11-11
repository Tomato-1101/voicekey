"""
Google Meet 議事録ボットの本体（画面から呼ぶ口）

会議の URL を受け取り、ボット用 Chrome で会議に入り、Meet の字幕を読んで議事録へ 1 行ずつ書く。
画面（リアルタイム欄・議事録一覧・要約ボタン）はこのクラスのシグナルを受けて表示するだけでよい。

参加〜記録〜退出は 1 本の作業スレッドの上で順番に進める（Chrome との通信が同期処理なので、
画面のスレッドを止めないため）。止める合図は `threading.Event` 1 つで、待ちはすべてこの合図で起きる。
"""

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

from PySide6.QtCore import QObject, Signal

from ..utils.logger import get_logger
from . import rules, scripts, summarizer, transcript
from .chrome import Browser, CDPDisconnected, CDPError, CDPTimeout, ChromeNotFound
from .rules import MeetBotError
from .settle import CaptionSettleTracker, Line

logger = get_logger(__name__)

# 設定キー（`config/constants.py` の DEFAULT_CONFIG と同じ名前）
CONFIG_CAPTION_LANGUAGE = "meetbot_caption_language"
CONFIG_SUMMARY_MODEL = "meetbot_summary_model"

_MEET_ORIGIN = "https://meet.google.com"
_LOGIN_URL = "https://accounts.google.com/"
_TITLE_SUFFIX = " - Google Meet"
# 字幕をオンにする操作を試す回数
_CAPTION_ATTEMPTS = 4

SUMMARY_BUSY = "別の議事録を要約しています。終わってからもう一度お試しください。"
SUMMARY_UNREADABLE = "議事録を読めませんでした"


@dataclass
class Timing:
    """
    待ち時間の一式（秒）。テストでは小さい値に差し替える。

    Attributes:
        devtools_wait: Chrome の DevTools が応答するまで待つ上限
        ready_poll: その確認の間隔
        page_load: 会議のページを開いてから操作を始めるまでの待ち
        consent_wait: 同意の画面を押した後の待ち
        join_poll: 入室待ちの確認の間隔
        join_limit: 入室待ちの上限（承認待ちに入ったら承認待ちの上限へ延ばす）
        join_retry: 参加ボタンを押し直す間隔
        caption_retry: 字幕をオンにする操作の間隔
        caption_poll: 字幕を読む間隔
        status_poll: 会議に居続けているかを確かめる間隔
        leave_wait: 退出ボタンを押してから Chrome を閉じるまでの待ち
        login_poll: ログイン画面が閉じられたかを確かめる間隔
        login_cookie: ログインできたかを確かめる間隔
        login_limit: ログイン画面を開いたままにする上限
    """

    devtools_wait: float = 20.0
    ready_poll: float = 0.25
    page_load: float = 4.0
    consent_wait: float = 3.0
    join_poll: float = 2.0
    join_limit: float = 90.0
    join_retry: float = 4.0
    caption_retry: float = 1.5
    caption_poll: float = 1.0
    status_poll: float = 3.0
    leave_wait: float = 1.5
    login_poll: float = 2.0
    login_cookie: float = 6.0
    login_limit: float = 30 * 60.0


class _Stopped(Exception):
    """会議に入る前に、止める合図が来た。"""


class MeetBotService(QObject):
    """
    Google Meet 議事録ボット。

    シグナルは作業スレッドから出る。画面側は普通に `connect` すればよい
    （このオブジェクトは画面のスレッドに属するので、Qt が自動で画面のスレッドへ渡す）。

    Signals:
        state_changed(state, text): 状態が変わった。state は `rules.STATE_*`、text は画面に出す 1 行
        line_confirmed(speaker, time, text): 議事録に 1 行書いた（話者が分からなければ speaker は空）
        finished(path, message): 記録を閉じた。path は議事録のファイル（作れなかったら空）、message は結果の文
        notice(message): 本人への知らせ（承認待ち・参加できなかった理由・字幕が取れていない など）
        summary_progress(path, message): 要約の進み具合（最初の 1 回が「開始」）
        summary_finished(path, text, message): 要約ができた。text は要約の本文、message は保存の結果
        summary_failed(path, message): 要約できなかった
        login_state_changed(text): ボット用ブラウザのログイン状態の表示
    """

    state_changed = Signal(str, str)
    line_confirmed = Signal(str, str, str)
    finished = Signal(str, str)
    notice = Signal(str)
    summary_progress = Signal(str, str)
    summary_finished = Signal(str, str, str)
    summary_failed = Signal(str, str)
    login_state_changed = Signal(str)

    def __init__(
        self,
        config: Any = None,
        usage_store: Any = None,
        browser_factory: Callable[[], Any] = Browser,
        directory: Optional[Path] = None,
        timing: Optional[Timing] = None,
        chat: Optional[Callable[[str, str], str]] = None,
        parent: Optional[QObject] = None,
    ):
        """
        Args:
            config: 設定（`get(key, default)` を持つもの。`ConfigManager`）。省略時は既定値で動く
            usage_store: 要約の使用量の記録先（`ApiUsageStore`）。省略時は記録しない
            browser_factory: ボット用 Chrome の作り方（テストで偽物に差し替える）
            directory: 議事録の保存先（省略時は `Documents/voicekey/transcripts`）
            timing: 待ち時間の一式
            chat: 要約の問い合わせ（テストで差し替える。省略時は Groq）
            parent: Qt の親
        """
        super().__init__(parent)
        self._config = config
        self._usage_store = usage_store
        self._browser_factory = browser_factory
        self._directory = directory
        self._timing = timing or Timing()
        self._chat = chat

        self._lock = threading.Lock()
        self._state = rules.STATE_IDLE
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._browser: Any = None
        self._login_stop = threading.Event()
        self._login_thread: Optional[threading.Thread] = None
        self._login_browser: Any = None
        self._summary_stop = threading.Event()
        self._summary_thread: Optional[threading.Thread] = None

    # ============================================
    # 画面から呼ぶ口
    # ============================================
    @property
    def state(self) -> str:
        """いまの状態（`rules.STATE_*`）。"""
        return self._state

    def is_active(self) -> bool:
        """会議への参加が動いているか（準備中・承認待ち・記録中・後片付け中）。"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, url: str) -> Optional[str]:
        """
        会議に参加して記録を始める。

        Args:
            url: 会議の URL（`meet.google.com/abc-defg-hij`・会議コードだけでもよい）

        Returns:
            受け付けたら None。断ったときは、その理由の文（画面にそのまま出せる）
        """
        normalized = rules.normalize_meet_url(url)
        if not normalized:
            return rules.INVALID_URL_MESSAGE
        with self._lock:
            if self.is_active():
                return rules.JOIN_WHILE_CLOSING if self._stop.is_set() else rules.ALREADY_ACTIVE
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run_meeting, args=(normalized, self._stop), name="meetbot", daemon=True,
            )
            self._thread.start()
        return None

    def stop(self) -> None:
        """会議から退出して記録を閉じる（参加の準備中なら取りやめる）。結果は `finished` で届く。"""
        self._stop.set()

    def summarize(self, path: str) -> Optional[str]:
        """
        議事録を要約する（結果は `summary_*` シグナルで届き、`<議事録>.summary.md` に保存される）。

        Args:
            path: 議事録のファイル

        Returns:
            受け付けたら None。断ったときは、その理由の文
        """
        with self._lock:
            if self._summary_thread is not None and self._summary_thread.is_alive():
                return SUMMARY_BUSY
            self._summary_stop = threading.Event()
            self._summary_thread = threading.Thread(
                target=self._run_summary, args=(str(path), self._summary_stop), name="meetbot-summary", daemon=True,
            )
            self._summary_thread.start()
        return None

    def cancel_summary(self) -> None:
        """要約を止める（`summary_failed` で「要約を止めました」が届く）。"""
        self._summary_stop.set()

    def open_login(self) -> Optional[str]:
        """
        ボット用ブラウザを画面に出して、Google のログイン画面を開く（ログインは本人がする）。

        Returns:
            受け付けたら None。断ったときは、その理由の文
        """
        with self._lock:
            if self.is_active():
                return rules.LOGIN_WHILE_ACTIVE
            if self._login_thread is not None and self._login_thread.is_alive():
                return None  # もう開いている
            self._login_stop = threading.Event()
            self._login_thread = threading.Thread(
                target=self._run_login, args=(self._login_stop,), name="meetbot-login", daemon=True,
            )
            self._login_thread.start()
        return None

    def shutdown(self, timeout: float = 5.0) -> None:
        """
        アプリの終了時に呼ぶ。動いているものを止め、ボット用 Chrome を必ず畳む。

        Args:
            timeout: 作業スレッドの後片付け（退出・議事録を閉じる）を待つ上限（秒）
        """
        self._stop.set()
        self._login_stop.set()
        self._summary_stop.set()
        for thread in (self._thread, self._login_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout)
        # 待ちきれなかったときも Chrome は残さない（議事録は 1 行ごとに書いてあるので失われない）
        for browser in (self._browser, self._login_browser):
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    # ============================================
    # 設定
    # ============================================
    def _setting(self, key: str, default: str) -> str:
        """設定を読む（設定が渡されていなければ既定値）。"""
        if self._config is None:
            return default
        try:
            value = self._config.get(key, default)
        except Exception:
            return default
        return value if isinstance(value, str) and value else default

    def _set_state(self, state: str, detail: str = "", text: Optional[str] = None) -> None:
        """状態を変えて知らせる。"""
        self._state = state
        self.state_changed.emit(state, text if text is not None else rules.state_text(state, detail))

    # ============================================
    # 会議への参加〜記録
    # ============================================
    def _run_meeting(self, url: str, stop: threading.Event) -> None:
        """作業スレッドの本体（参加 → 記録 → 後片付け）。"""
        browser: Any = None
        page: Any = None
        try:
            # ログイン画面が開いたままなら先に閉じる（同じプロファイルは 1 つの Chrome でしか開けない）
            self._close_login()
            self._set_state(rules.STATE_LAUNCHING)
            browser = self._browser_factory()
            self._browser = browser
            self._launch(browser, stop, headless=True, mute=True, initial_url=None)

            self._set_state(rules.STATE_JOINING, "ログインを確認しています")
            mode = rules.join_mode(self._logged_in(browser))
            try:
                browser.deny_media(_MEET_ORIGIN)
            except CDPError:
                # 拒否にできなくても起動引数で確認は出ない。参加は続ける
                logger.warning("ボット用 Chrome のカメラ・マイクを拒否にできませんでした")
            try:
                page = browser.open_page(url)
            except CDPError:
                raise MeetBotError(MeetBotError.CHROME_NOT_RESPONDING) from None

            streak = rules.TimeoutStreak()
            meeting_name = self._join(browser, page, streak, stop, mode, url)
            self._record(browser, page, streak, stop, url, meeting_name)
        except _Stopped:
            self._set_state(rules.STATE_IDLE)
        except MeetBotError as error:
            logger.warning(f"Meet ボット: 参加できませんでした（{error.kind}）")
            self._set_state(rules.STATE_FAILED, error.message)
            self.notice.emit(rules.join_failure_notice(error))
        except Exception as error:
            logger.error(f"Meet ボット: 想定外の失敗（{type(error).__name__}: {error}）")
            failure = MeetBotError(MeetBotError.BROWSER_FAILED, "会議に参加できませんでした")
            self._set_state(rules.STATE_FAILED, failure.message)
            self.notice.emit(rules.join_failure_notice(failure))
        finally:
            if page is not None:
                page.close()
            if browser is not None:
                browser.close()
            self._browser = None

    def _launch(self, browser: Any, stop: threading.Event, headless: bool, mute: bool, initial_url: Optional[str]) -> None:
        """
        ボット用 Chrome を起動して、DevTools が応答するまで待つ。

        Raises:
            MeetBotError: Chrome が無い・ポートが使われている・起動できない・応答しない
            _Stopped: 待っているあいだに止める合図が来た
        """
        browser.close_leftover()
        if browser.is_answering():
            # 自分の取り残しではない何かがポートを使っている。それには触らない
            raise MeetBotError.port_busy(browser.port)
        try:
            browser.launch(headless=headless, mute=mute, initial_url=initial_url)
        except ChromeNotFound:
            raise MeetBotError(MeetBotError.CHROME_NOT_FOUND) from None
        except OSError as error:
            raise MeetBotError.browser_failed(error.strerror or type(error).__name__) from None
        deadline = time.monotonic() + self._timing.devtools_wait
        while not browser.is_answering():
            if not browser.is_running():
                raise MeetBotError(MeetBotError.BROWSER_CLOSED)
            if time.monotonic() >= deadline:
                raise MeetBotError(MeetBotError.CHROME_NOT_RESPONDING)
            self._pause(stop, self._timing.ready_poll)

    @staticmethod
    def _pause(stop: threading.Event, seconds: float) -> None:
        """待つ。止める合図が来たらすぐ起きて `_Stopped` を投げる。"""
        if stop.wait(seconds):
            raise _Stopped()

    @staticmethod
    def _logged_in(browser: Any) -> Optional[bool]:
        """ボット用ブラウザが Google にログインしているか（確かめられなければ None）。"""
        try:
            return rules.is_logged_in(browser.cookies())
        except CDPError:
            return None

    @staticmethod
    def _evaluate(browser: Any, page: Any, streak: rules.TimeoutStreak, java_script: str) -> Any:
        """
        ページ内でスクリプトを実行する。

        1 回の時間切れやスクリプトの失敗では止めず、None を返して次の周回に任せる
        （画面の切り替わり途中は普通に起きる）。

        Returns:
            スクリプトの値（読めなかったら None）

        Raises:
            MeetBotError: 接続が切れた・Chrome が終了した・時間切れが続いた
        """
        try:
            value = page.evaluate(java_script)
        except CDPTimeout:
            if streak.record(True):
                raise MeetBotError(MeetBotError.CHROME_NOT_RESPONDING) from None
            return None
        except CDPDisconnected:
            kind = MeetBotError.DISCONNECTED if browser.is_running() else MeetBotError.BROWSER_CLOSED
            raise MeetBotError(kind) from None
        except CDPError:
            streak.record(False)
            return None
        streak.record(False)
        return value

    def _join(self, browser: Any, page: Any, streak: rules.TimeoutStreak, stop: threading.Event, mode: str, url: str) -> str:
        """
        会議に入る（参加ボタンを押し、入室か失敗が決まるまで待つ）。

        Returns:
            会議名（読めなければ会議コード）

        Raises:
            MeetBotError: 入れなかった
            _Stopped: 止める合図が来た
        """
        timing = self._timing

        def run(java_script: str) -> Any:
            return self._evaluate(browser, page, streak, java_script)

        self._set_state(rules.STATE_JOINING, "会議を開いています")
        self._pause(stop, timing.page_load)
        consent = run(scripts.CONSENT)
        if isinstance(consent, dict) and consent.get("clicked"):
            self._pause(stop, timing.consent_wait)
        if run(scripts.LEFT_MEETING_PAGE) is True:
            raise rules.left_meeting_page(mode)

        retry = rules.JoinRetry(timing.join_retry)

        def press() -> None:
            result = run(scripts.join(rules.BOT_DISPLAY_NAME))
            found = isinstance(result, dict) and bool(result.get("clicked") or result.get("nameFilled"))
            retry.record_press(found, time.monotonic())

        self._set_state(rules.STATE_JOINING, "参加ボタンを押しています")
        press()
        self._set_state(rules.STATE_JOINING, "会議への入室を待っています")

        deadline = time.monotonic() + timing.join_limit
        waiting_seen = False
        while True:
            self._pause(stop, timing.join_poll)
            status = rules.page_status_from_payload(run(scripts.PAGE_STATUS))
            step = rules.join_step(status, mode)
            now = time.monotonic()
            if step.action == rules.STEP_ENTERED:
                break
            if step.action == rules.STEP_FAIL and step.error is not None:
                raise step.error
            if step.action == rules.STEP_WAIT_FOR_APPROVAL:
                if not waiting_seen:
                    waiting_seen = True
                    # 承認は人が押すので、入室待ちの上限では短すぎる
                    deadline = now + rules.approval_wait_limit(mode)
                    self._set_state(rules.STATE_WAITING_APPROVAL, text=rules.waiting_status_line(mode))
                    self.notice.emit(rules.waiting_notice(mode))
            elif step.action == rules.STEP_PRESS_JOIN and retry.should_press(status, now):
                self._set_state(rules.STATE_JOINING, f"参加ボタンを押し直しています（{retry.attempts + 1} 回目）")
                press()
                self._set_state(rules.STATE_JOINING, "会議への入室を待っています")
            if now >= deadline:
                raise rules.join_timeout(mode, waiting_seen, retry.button_found)

        title = run(scripts.TITLE)
        name = title.strip() if isinstance(title, str) else ""
        if name.endswith(_TITLE_SUFFIX):
            name = name[: -len(_TITLE_SUFFIX)].strip()
        name = name or rules.meeting_code(url)

        run(scripts.ENSURE_MUTED)
        self._set_state(rules.STATE_JOINING, "字幕をオンにしています")
        self._enable_captions(run, stop)
        return name

    def _enable_captions(self, run: Callable[[str], Any], stop: threading.Event) -> None:
        """Meet の字幕をオンにして、言語を設定に合わせる（できなくても参加は続け、後で知らせる）。"""
        shortcut_tried = False
        for _ in range(_CAPTION_ATTEMPTS):
            state = run(scripts.CAPTION_STATE)
            buttons = state.get("buttons") if isinstance(state, dict) else None
            toggle = rules.caption_toggle(
                buttons if isinstance(buttons, list) else [],
                bool(isinstance(state, dict) and state.get("regionVisible")),
                shortcut_tried,
            )
            if toggle.action == rules.CAPTION_ALREADY_ON:
                break
            if toggle.action == rules.CAPTION_CLICK:
                run(scripts.click_caption_button(toggle.label))
            elif toggle.action == rules.CAPTION_SHORTCUT:
                run(scripts.CAPTION_SHORTCUT)
                shortcut_tried = True
            self._pause(stop, self._timing.caption_retry)
        run(scripts.caption_language(self._setting(CONFIG_CAPTION_LANGUAGE, "ja")))

    def _record(self, browser: Any, page: Any, streak: rules.TimeoutStreak, stop: threading.Event, url: str, meeting_name: str) -> None:
        """字幕を読んで議事録へ書き続け、終わったら閉じて結果を知らせる。"""
        timing = self._timing
        language = self._setting(CONFIG_CAPTION_LANGUAGE, "ja")
        recorder = transcript.TranscriptRecorder(self._directory)
        file_started = recorder.begin(
            rules.meeting_code(url), f"Google Meet / {meeting_name}", rules.caption_language_label(language), datetime.now(),
        )
        tracker = CaptionSettleTracker()
        presence = rules.MeetingPresenceWatch()
        captions = rules.CaptionPresenceWatch(time.monotonic())

        def refresh() -> None:
            line = rules.recording_status_line(recorder.lines, meeting_name, captions.warning, recorder.last_error)
            self._set_state(rules.STATE_RECORDING, line)

        def write(lines: List[Line]) -> None:
            wrote = False
            for line in lines:
                stamp = recorder.append(line.text, line.speaker, datetime.now())
                if stamp:
                    wrote = True
                    self.line_confirmed.emit(line.speaker or "", stamp, line.text)
            if wrote or (lines and recorder.last_error):
                refresh()

        refresh()
        reason = ""
        leave = False
        next_status = time.monotonic() + timing.status_poll
        try:
            while not reason:
                if stop.wait(timing.caption_poll):
                    reason, leave = rules.REASON_LEFT, True
                    break
                payload = self._evaluate(browser, page, streak, scripts.CAPTIONS)
                now = time.monotonic()
                if isinstance(payload, dict):
                    write(self._ingest(tracker, payload, now))
                    event = captions.observe(bool(payload.get("captionsOn")), now)
                    if event == "warn":
                        self.notice.emit(rules.CAPTION_MISSING_NOTICE)
                    if event:
                        refresh()
                if now >= next_status:
                    next_status = now + timing.status_poll
                    status = rules.page_status_from_payload(self._evaluate(browser, page, streak, scripts.PAGE_STATUS))
                    ending = presence.observe(status) if status else None
                    if ending == "removed":
                        reason = rules.REASON_REMOVED
                    elif ending == "ended":
                        reason = rules.REASON_ENDED
        except MeetBotError as error:
            # 記録の途中で Chrome を失った。ここまでの議事録は残っているので、理由を添えて閉じる
            reason = error.message

        # まだ確定待ちの字幕を落とさない
        write(tracker.flush())
        if leave:
            try:
                page.evaluate(scripts.LEAVE)
                time.sleep(timing.leave_wait)
            except CDPError:
                pass
        summary = recorder.end()
        message = rules.closing_message(reason, file_started, summary.lines, summary.error)
        self._set_state(rules.STATE_ENDED, reason)
        self.finished.emit(str(summary.path) if summary.path else "", message)

    @staticmethod
    def _ingest(tracker: CaptionSettleTracker, payload: dict, now: float) -> List[Line]:
        """字幕の読み取り結果を確定判定へ渡す。"""
        raw = payload.get("entries")
        entries = [entry for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []
        texts = [str(entry.get("text") or "") for entry in entries]
        if payload.get("source") == "region":
            # 話者ごとの塊が取れず、字幕欄の文字だけ読めた（Meet の画面が変わったときの保険）
            return tracker.ingest_region(" ".join(text for text in texts if text), now)
        return tracker.ingest([(str(entry.get("speaker") or ""), text) for entry, text in zip(entries, texts)], now)

    # ============================================
    # ログイン画面
    # ============================================
    def _close_login(self) -> None:
        """開いているログイン画面を閉じて、その後片付けを待つ。"""
        thread = self._login_thread
        if thread is not None and thread.is_alive():
            self._login_stop.set()
            thread.join(10.0)

    def _run_login(self, stop: threading.Event) -> None:
        """ログイン画面を出し、閉じられるまでログインできたかを見張る。"""
        timing = self._timing
        browser: Any = None
        logged_in: Optional[bool] = None
        try:
            self.login_state_changed.emit(rules.LOGIN_CHECKING)
            browser = self._browser_factory()
            self._login_browser = browser
            self._launch(browser, stop, headless=False, mute=False, initial_url=_LOGIN_URL)
            logged_in = self._logged_in(browser)
            self.login_state_changed.emit(rules.LOGIN_DONE if logged_in else rules.LOGIN_WAITING)

            deadline = time.monotonic() + timing.login_limit
            next_cookie = time.monotonic() + timing.login_cookie
            page_seen = False
            while time.monotonic() < deadline and not stop.wait(timing.login_poll):
                if not browser.is_running():
                    break
                try:
                    pages = browser.page_count()
                except CDPError:
                    break
                if pages > 0:
                    page_seen = True
                elif page_seen:
                    break  # 本人が窓を閉じた
                if time.monotonic() >= next_cookie:
                    next_cookie = time.monotonic() + timing.login_cookie
                    checked = self._logged_in(browser)
                    if checked is not None and checked != logged_in:
                        logged_in = checked
                        self.login_state_changed.emit(rules.LOGIN_DONE if logged_in else rules.LOGIN_WAITING)
        except _Stopped:
            pass
        except MeetBotError as error:
            self.notice.emit(error.message)
        except Exception as error:
            logger.error(f"Meet ボット: ログイン画面で想定外の失敗（{type(error).__name__}: {error}）")
        finally:
            if browser is not None:
                browser.close()
            self._login_browser = None
            if logged_in is None:
                self.login_state_changed.emit(rules.LOGIN_UNKNOWN)
            else:
                self.login_state_changed.emit(rules.LOGIN_DONE if logged_in else rules.LOGIN_NONE)

    # ============================================
    # 要約
    # ============================================
    def _run_summary(self, path: str, stop: threading.Event) -> None:
        """議事録を要約して保存する。"""
        self.summary_progress.emit(path, summarizer.PREPARING_MESSAGE)
        text = transcript.read_text(Path(path))
        if text is None:
            self.summary_failed.emit(path, SUMMARY_UNREADABLE)
            return
        chat = self._chat
        if chat is None:
            model = self._setting(CONFIG_SUMMARY_MODEL, "")

            def chat(system: str, user: str) -> str:
                return summarizer.groq_chat(system, user, model or None, self._usage_store)

        try:
            result = summarizer.summarize(
                text, chat, progress=lambda message: self.summary_progress.emit(path, message), cancelled=stop,
            )
        except Exception as error:
            if not isinstance(error, (summarizer.ChatError, summarizer.SummaryEmpty, summarizer.SummaryCancelled)):
                logger.error(f"議事録の要約: 想定外の失敗（{type(error).__name__}）")
            self.summary_failed.emit(path, summarizer.failure_message(error))
            return
        saved = transcript.write_summary(Path(path), result)
        self.summary_finished.emit(
            path, result, summarizer.SAVED_MESSAGE if saved else summarizer.NOT_SAVED_MESSAGE,
        )
