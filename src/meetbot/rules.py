"""
Meet ボットの判断と文言（純粋なロジック）

ボット本体（`service.MeetBotService`）は Chrome と画面の状態に振り回されるので、
「画面の文字から何が起きたかを読む」「いつ諦めるか」「何と表示するか」をここへ切り出し、
ブラウザ無しでテストできるようにしてある。

Meet の**表示文言への依存**はここ（`page_status` / `caption_toggle`）と `scripts.py` だけに置く。
画面に出す文言は Mac 版と同じ（Mac のメニューバーを指す案内だけ Windows の言い方に直してある）。
"""

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

# 会議に出すボットの名前（ゲストとして入るとき、主催者の画面にこの名前で出る）
BOT_DISPLAY_NAME = "voicekey 議事録ボット"

# ============================================
# 会議の URL
# ============================================
_MEETING_PATH = re.compile(r"^/[a-z]{3}-?[a-z]{4}-?[a-z]{3}/?$", re.IGNORECASE)
_LOOKUP_PATH = re.compile(r"^/lookup/[A-Za-z0-9_-]+/?$")

INVALID_URL_MESSAGE = "Meet の会議 URL ではありません（meet.google.com/abc-defg-hij の形）"


def normalize_meet_url(text: str) -> Optional[str]:
    """
    入力された文字列を Meet の会議 URL に整える。

    Args:
        text: 入力（`meet.google.com/abc-defg-hij` のように https:// が無くてもよい）

    Returns:
        会議 URL（Meet の会議でなければ None）
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != "meet.google.com":
        return None
    if not (_MEETING_PATH.match(parts.path) or _LOOKUP_PATH.match(parts.path)):
        return None
    query = f"?{parts.query}" if parts.query else ""
    return f"https://meet.google.com{parts.path}{query}"


def meeting_code(url: str) -> str:
    """
    会議 URL から会議コード（議事録のファイル名に添える）を取り出す。

    Args:
        url: `normalize_meet_url` を通した URL

    Returns:
        パスの前後の `/` を除いたもの（例: `abc-defg-hij`）
    """
    return urlsplit(url).path.strip("/")


# ============================================
# 画面の状態
# ============================================
class PageStatus:
    """会議画面の状態（`scripts.PAGE_STATUS` の結果を分類したもの）。"""

    IN_MEETING = "in_meeting"            # 会議に入っている
    WAITING_APPROVAL = "waiting_approval"  # ホストの承認を待っている
    DENIED = "denied"                    # 参加を断られた（ホストが拒否した）
    CANNOT_JOIN = "cannot_join"          # この会議には入れないと表示された
    GUEST_BLOCKED = "guest_blocked"      # ゲスト（未ログイン）では入れない会議だった
    REMOVED = "removed"                  # 会議から外された
    ENDED = "ended"                      # 会議が終わった・退出した画面になった
    NOT_FOUND = "not_found"              # 会議コードが無効
    OTHER = "other"                      # どれでもない（ロビー・読み込み中など）


def _matches(text: str, words: Iterable[str]) -> bool:
    """大文字小文字を区別せずに、いずれかの語を含むかを返す。"""
    lower = (text or "").lower()
    return any(word.lower() in lower for word in words)


def page_status(in_meeting: bool, text: str, sign_in_page: bool = False, join_controls: bool = False) -> str:
    """
    画面の文字から会議の状態を分類する。

    承認待ちと拒否を取り違えると通知が逆になるので、先に「終わり」系を見てから承認待ちを見る。

    Args:
        in_meeting: 「通話から退出」ボタンが見えているか
        text: 会議外のときの画面の文字
        sign_in_page: Google のログイン画面へ飛ばされているか
        join_controls: 参加ボタンか名前欄が見えているか

    Returns:
        `PageStatus` の値
    """
    if in_meeting:
        return PageStatus.IN_MEETING
    if sign_in_page:
        return PageStatus.GUEST_BLOCKED
    if _matches(text, ["参加リクエストが拒否", "リクエストが拒否されました", "参加を拒否", "denied your request"]):
        return PageStatus.DENIED
    # ゲストが入れない会議の文言（実会議では未確認。推測で置いた語なので、壊れたらここを直す）。
    # 参加ボタンや名前欄が見えている間は当てない（ロビーの案内文に同じ語が出ても誤判定しないため）。
    if not join_controls and _matches(text, [
        "参加するにはログイン", "ログインして参加", "ログインが必要です", "参加をリクエストできません",
        "Sign in to join", "You must be signed in", "You can't ask to join", "can't request to join",
    ]):
        return PageStatus.GUEST_BLOCKED
    if _matches(text, ["この通話に参加できません", "You can't join this call", "can't join this video call"]):
        return PageStatus.CANNOT_JOIN
    # 「削除されました」だけだと、チャットのメッセージ削除など会議と関係ない表示でも当たる。
    # 会議・通話から外されたことを言う文言だけに絞る（確定は呼び出し側で 2 回連続を見る）。
    if _matches(text, [
        "通話から削除されました", "会議から削除されました", "退出させられました",
        "You've been removed from", "You have been removed from", "removed you from the",
    ]):
        return PageStatus.REMOVED
    if _matches(text, ["会議コードを確認", "無効なビデオ通話名", "Check your meeting code", "Invalid video call name"]):
        return PageStatus.NOT_FOUND
    if _matches(text, [
        "通話は終了しました", "会議は終了しました", "通話から退出しました", "通話を終了しました",
        "The call has ended", "This call has ended", "You left the meeting", "You've left the meeting",
        "The meeting has ended",
    ]):
        return PageStatus.ENDED
    if _matches(text, [
        "参加をリクエストしています", "参加を許可すると", "承認されるまで", "参加の承認を待って",
        "Asking to be let in", "when someone lets you in", "Waiting for the host", "Someone will let you in",
    ]):
        return PageStatus.WAITING_APPROVAL
    return PageStatus.OTHER


def page_status_from_payload(payload: Any) -> Optional[str]:
    """
    `scripts.PAGE_STATUS` の戻り値から状態を読む。

    Args:
        payload: スクリプトの戻り値

    Returns:
        `PageStatus` の値（読めなかったら None＝何もせず次の読み取りを待つ）
    """
    if not isinstance(payload, dict):
        return None
    return page_status(
        bool(payload.get("inMeeting")), str(payload.get("text") or ""),
        bool(payload.get("signInPage")), bool(payload.get("joinControls")),
    )


# ============================================
# 字幕ボタン
# ============================================
# 字幕をどう扱うかの判断結果（click のときだけ押すボタンのラベルが付く）
CAPTION_ALREADY_ON = "already_on"
CAPTION_CLICK = "click"
CAPTION_SHORTCUT = "shortcut"
CAPTION_UNKNOWN = "unknown"


@dataclass(frozen=True)
class CaptionToggle:
    """字幕をどう扱うかの判断結果。"""

    action: str
    label: str = ""


def caption_toggle(buttons: List[Dict[str, Any]], region_visible: bool, shortcut_tried: bool) -> CaptionToggle:
    """
    字幕ボタンの状態から、押すべきかを決める。

    間違えてオフにしないことを最優先にする。オンだと読める手掛かり（字幕が見えている・
    押下状態・「オフにする」のラベル）が 1 つでもあれば何もしない。

    Args:
        buttons: 字幕に関係するボタン（`{"label": str, "pressed": True/False/None}`）
        region_visible: 字幕の表示欄が見えているか
        shortcut_tried: ショートカットを既に試したか

    Returns:
        判断結果
    """
    if region_visible:
        return CaptionToggle(CAPTION_ALREADY_ON)
    related = [
        b for b in buttons
        if isinstance(b, dict) and _matches(str(b.get("label") or ""), ["字幕", "caption", "subtitle"])
    ]
    off_words = ["オフにする", "turn off", "hide", "無効にする"]
    if any(b.get("pressed") is True or _matches(str(b.get("label")), off_words) for b in related):
        return CaptionToggle(CAPTION_ALREADY_ON)
    on_words = ["オンにする", "turn on", "show", "有効にする"]
    for b in related:
        if _matches(str(b.get("label")), on_words) or b.get("pressed") is False:
            return CaptionToggle(CAPTION_CLICK, str(b.get("label")))
    return CaptionToggle(CAPTION_UNKNOWN if shortcut_tried else CAPTION_SHORTCUT)


# ============================================
# ログイン判定
# ============================================
_LOGIN_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID"}


def is_logged_in(cookies: Iterable[Any], now: Optional[float] = None) -> bool:
    """
    Google にログイン済みかを Cookie から判定する。

    Google のログインセッションを表す Cookie（`SID` など）が google.com にあり、
    期限切れでなければログイン済みとみなす。値は見ない（名前と期限だけ）。

    Args:
        cookies: CDP の `Storage.getCookies` が返した配列
        now: 期限の比較に使う現在時刻（エポック秒。省略時は今）

    Returns:
        ログイン済みなら True
    """
    current = time.time() if now is None else now
    for cookie in cookies or []:
        if not isinstance(cookie, dict) or cookie.get("name") not in _LOGIN_COOKIES:
            continue
        domain = str(cookie.get("domain") or "")
        if not (domain == "google.com" or domain.endswith(".google.com")):
            continue
        # セッション Cookie は expires が -1。正の値で過去なら期限切れ
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and 0 < expires < current:
            continue
        return True
    return False


# ============================================
# 失敗の種類と文言
# ============================================
class MeetBotError(Exception):
    """
    ボット固有の失敗。

    Attributes:
        kind: 失敗の種類（`MeetBotError.DENIED` など）
        message: 画面に出す説明
    """

    DENIED = "denied"
    REMOVED = "removed"
    CALL_ENDED = "call_ended"
    APPROVAL_TIMEOUT = "approval_timeout"
    GUEST_NOT_APPROVED = "guest_not_approved"
    GUEST_NOT_ALLOWED = "guest_not_allowed"
    NOT_LOGGED_IN = "not_logged_in"
    COULD_NOT_ENTER = "could_not_enter"
    JOIN_BUTTON_NOT_FOUND = "join_button_not_found"
    MEETING_NOT_FOUND = "meeting_not_found"
    CHROME_NOT_FOUND = "chrome_not_found"
    CHROME_NOT_RESPONDING = "chrome_not_responding"
    PORT_BUSY = "port_busy"
    BROWSER_CLOSED = "browser_closed"
    DISCONNECTED = "disconnected"
    BROWSER_FAILED = "browser_failed"

    _MESSAGES = {
        DENIED: "参加を断られました（ホストが拒否したか、外部の参加を受け付けない会議です）",
        REMOVED: "会議から外されました",
        CALL_ENDED: "会議はすでに終わっています",
        APPROVAL_TIMEOUT: "10 分待っても参加を承認されませんでした",
        GUEST_NOT_APPROVED: "主催者に承認されませんでした",
        GUEST_NOT_ALLOWED: "この会議はゲストが入れません。ボットに Google ログインをさせると入れます",
        NOT_LOGGED_IN: (
            "ボット用ブラウザが Google にログインしていません。"
            "「ボット用ブラウザで Google にログイン…」でログインしてから、もう一度参加してください"
        ),
        COULD_NOT_ENTER: "参加ボタンは押せましたが、90 秒たっても会議に入れませんでした",
        JOIN_BUTTON_NOT_FOUND: "参加ボタンが見つかりませんでした（Meet の画面が変わった可能性があります）",
        MEETING_NOT_FOUND: "この会議は見つかりませんでした。URL（会議コード）を確かめてください",
        CHROME_NOT_FOUND: "Google Chrome が必要です",
        CHROME_NOT_RESPONDING: "Chrome が応答しません",
        BROWSER_CLOSED: "ブラウザが終了しました",
        DISCONNECTED: "ブラウザとの接続が切れました",
    }

    def __init__(self, kind: str, message: Optional[str] = None):
        """
        Args:
            kind: 失敗の種類
            message: 既定の文言を使わないときの説明
        """
        self.kind = kind
        self.message = message or self._MESSAGES.get(kind, "会議に参加できませんでした")
        super().__init__(self.message)

    @classmethod
    def port_busy(cls, port: int) -> "MeetBotError":
        """DevTools のポートを別のアプリが使っているときの失敗を作る。"""
        return cls(cls.PORT_BUSY, f"ポート {port} を別のアプリが使っています（Chrome のリモートデバッグなど）")

    @classmethod
    def browser_failed(cls, detail: str) -> "MeetBotError":
        """Chrome を起動できなかったときの失敗を作る。"""
        return cls(cls.BROWSER_FAILED, f"ブラウザを開けません: {detail}")

    @property
    def suggests_login(self) -> bool:
        """ボット用ブラウザで Google にログインすると解決しうる失敗か（画面でログインの導線を出す目印）。"""
        return self.kind in (self.GUEST_NOT_APPROVED, self.GUEST_NOT_ALLOWED, self.NOT_LOGGED_IN)


# ============================================
# 入り方（ログイン済み / ゲスト）
# ============================================
MODE_ACCOUNT = "account"
MODE_GUEST = "guest"


def join_mode(logged_in: Optional[bool]) -> str:
    """
    どの立場で会議に入るかを決める。

    ボット用ブラウザが Google にログインしていればそのアカウントで、していなければ
    ゲストとして名前を入れて参加をリクエストする。

    Args:
        logged_in: ログイン確認の結果（確かめられなかったら None＝ゲスト扱いで試す）

    Returns:
        `MODE_ACCOUNT` か `MODE_GUEST`
    """
    return MODE_ACCOUNT if logged_in is True else MODE_GUEST


def approval_wait_limit(mode: str) -> float:
    """
    主催者の承認を待つ上限（秒）を返す。

    ゲストは 5 分。知らない名前のリクエストは見落とされやすく、長く待っても承認されないことが多いので、
    早めに「承認されませんでした」と知らせる。ログイン済みは 10 分。
    """
    return 5 * 60 if mode == MODE_GUEST else 10 * 60


# 入室待ちの 1 回分の判断
STEP_ENTERED = "entered"
STEP_WAIT_FOR_APPROVAL = "wait_for_approval"
STEP_PRESS_JOIN = "press_join"
STEP_WAIT = "wait"
STEP_FAIL = "fail"


@dataclass(frozen=True)
class JoinStep:
    """入室待ちの 1 回分の判断（fail のときだけ理由が付く）。"""

    action: str
    error: Optional[MeetBotError] = None


def join_step(status: Optional[str], mode: str) -> JoinStep:
    """
    画面の状態から、次にすることを決める。

    ゲストのときは「断られた」「入れない」を理由で分ける（主催者が承認しなかったのか、
    そもそもゲストが入れない会議なのかで、本人が次にやることが違うため）。

    Args:
        status: `page_status` の結果（読めなかったら None）
        mode: どの立場で入ろうとしているか

    Returns:
        次にすること
    """
    guest = mode == MODE_GUEST
    if status == PageStatus.IN_MEETING:
        return JoinStep(STEP_ENTERED)
    if status == PageStatus.WAITING_APPROVAL:
        return JoinStep(STEP_WAIT_FOR_APPROVAL)
    if status == PageStatus.DENIED:
        return JoinStep(STEP_FAIL, MeetBotError(MeetBotError.GUEST_NOT_APPROVED if guest else MeetBotError.DENIED))
    if status in (PageStatus.CANNOT_JOIN, PageStatus.GUEST_BLOCKED):
        return JoinStep(STEP_FAIL, MeetBotError(MeetBotError.GUEST_NOT_ALLOWED if guest else MeetBotError.DENIED))
    if status == PageStatus.REMOVED:
        return JoinStep(STEP_FAIL, MeetBotError(MeetBotError.REMOVED))
    if status == PageStatus.ENDED:
        return JoinStep(STEP_FAIL, MeetBotError(MeetBotError.CALL_ENDED))
    if status == PageStatus.NOT_FOUND:
        return JoinStep(STEP_FAIL, MeetBotError(MeetBotError.MEETING_NOT_FOUND))
    if status == PageStatus.OTHER:
        return JoinStep(STEP_PRESS_JOIN)
    return JoinStep(STEP_WAIT)


def join_timeout(mode: str, waiting_seen: bool, join_button_found: bool) -> MeetBotError:
    """
    入室を待ちきれなかったときの失敗を、実際の状況で出し分ける。

    Args:
        mode: どの立場で入ろうとしていたか
        waiting_seen: 承認待ちの画面を見たか
        join_button_found: 参加ボタンを一度でも押せたか

    Returns:
        失敗
    """
    if waiting_seen:
        return MeetBotError(MeetBotError.GUEST_NOT_APPROVED if mode == MODE_GUEST else MeetBotError.APPROVAL_TIMEOUT)
    return MeetBotError(MeetBotError.COULD_NOT_ENTER if join_button_found else MeetBotError.JOIN_BUTTON_NOT_FOUND)


def left_meeting_page(mode: str) -> MeetBotError:
    """
    会議コードのページから紹介サイトやログイン画面へ飛ばされたときの失敗を返す。

    ゲストなら「ゲストは入れない会議」。ログイン済みのはずなのに飛ばされたなら、ログインが切れている。
    """
    return MeetBotError(MeetBotError.GUEST_NOT_ALLOWED if mode == MODE_GUEST else MeetBotError.NOT_LOGGED_IN)


# ============================================
# 回数・時間の数え方
# ============================================
class TimeoutStreak:
    """
    「Chrome が応答しない」が続いた回数を数える。

    1 回の時間切れで止めると、重い画面の切り替わりで誤って終了してしまう。上限回数続いたら諦める。
    """

    def __init__(self, limit: int = 3):
        """
        Args:
            limit: 何回続いたら諦めるか
        """
        self.limit = limit
        self.count = 0

    def record(self, timed_out: bool) -> bool:
        """
        1 回分の結果を記録する。

        Args:
            timed_out: 今回の命令が時間切れだったか（応答が来たら数え直し）

        Returns:
            上限に達した（諦めるべき）か
        """
        self.count = self.count + 1 if timed_out else 0
        return self.count >= self.limit


class MeetingPresenceWatch:
    """
    記録中の会議画面の見張り（外された・終わったの確定）。

    1 回の読み取りで決めると、画面の切り替わり途中の文字を拾って誤判定する。
    「外された」は 2 回、それ以外の「会議画面でない」は 3 回続いたら確定する。
    """

    REMOVED_LIMIT = 2
    NOT_IN_MEETING_LIMIT = 3

    def __init__(self) -> None:
        self._removed = 0
        self._not_in_meeting = 0

    def observe(self, status: str) -> Optional[str]:
        """
        1 回分の画面の状態を取り込む。

        Args:
            status: `page_status` の結果

        Returns:
            終わりが確定したら "removed" か "ended"（まだなら None）
        """
        if status == PageStatus.IN_MEETING:
            self._removed = 0
            self._not_in_meeting = 0
            return None
        if status == PageStatus.REMOVED:
            self._removed += 1
            self._not_in_meeting += 1
            return "removed" if self._removed >= self.REMOVED_LIMIT else None
        self._removed = 0
        self._not_in_meeting += 1
        return "ended" if self._not_in_meeting >= self.NOT_IN_MEETING_LIMIT else None


class CaptionPresenceWatch:
    """
    「Meet の字幕が読めていない」状態の見張り。

    字幕がオフのまま・Meet の画面が変わった、などで 1 行も取れていないのに「記録中 0 行」のままだと
    本人は会議が終わるまで気づけない。参加してから一定時間ずっと読めなければ 1 回だけ知らせる
    （誰も話さない時間があるたびに知らせると、本物の警告が埋もれるため 1 会議につき 1 回まで）。
    """

    WARN_AFTER = 60.0

    def __init__(self, start: float):
        """
        Args:
            start: 見張りを始めた時刻（参加した時刻）。最初から読めないときもここから数える
        """
        self._missing_since: Optional[float] = start
        self.warning = False
        self._warned = False

    def observe(self, found: bool, now: float) -> Optional[str]:
        """
        1 回分の読み取り結果を取り込む。

        Args:
            found: 字幕がオンで読める状態か（本文の有無ではなく字幕欄・字幕ボタンの状態）
            now: 読み取った時刻

        Returns:
            "warn"（取れていないと知らせる）/ "recovered"（また取れた）/ None（何もしない）
        """
        if found:
            self._missing_since = None
            if not self.warning:
                return None
            self.warning = False
            return "recovered"
        if self._missing_since is None:
            self._missing_since = now
        if self.warning or self._warned or now - self._missing_since < self.WARN_AFTER:
            return None
        self.warning = True
        self._warned = True
        return "warn"


class JoinRetry:
    """
    参加ボタンを押し直す段取り。

    Meet のロビーは組み上がるのが遅く、1 回押すだけでは「ボタンがまだ無い」で終わることがある。
    入室待ちで「その他」の画面が続く間は、数秒おきに上限回数まで押し直す。
    """

    MAX_ATTEMPTS = 5

    def __init__(self, interval: float = 4.0):
        """
        Args:
            interval: 押し直す間隔（秒）
        """
        self.interval = interval
        self.attempts = 0
        self.button_found = False
        self._last_attempt: Optional[float] = None

    def should_press(self, status: Optional[str], now: float) -> bool:
        """
        いま押すべきかを返す。

        Args:
            status: いまの画面の状態（「その他」＝まだロビーにいる間だけ押す）
            now: 現在時刻
        """
        if status != PageStatus.OTHER or self.attempts >= self.MAX_ATTEMPTS:
            return False
        return self._last_attempt is None or now - self._last_attempt >= self.interval

    def record_press(self, found: bool, now: float) -> None:
        """
        押した結果を記録する。

        Args:
            found: 参加ボタン（または名前の入力欄）が見つかって押せたか
            now: 押した時刻
        """
        self.attempts += 1
        self._last_attempt = now
        if found:
            self.button_found = True


# ============================================
# 画面に出す文言
# ============================================
STATE_IDLE = "idle"
STATE_LAUNCHING = "launching"
STATE_JOINING = "joining"
STATE_WAITING_APPROVAL = "waiting_approval"
STATE_RECORDING = "recording"
STATE_ENDED = "ended"
STATE_FAILED = "failed"

CAPTION_MISSING = "字幕が取れていません（Meet の字幕が ON か確認）"
CAPTION_MISSING_NOTICE = (
    "会議の字幕が 1 分以上取れていません。Meet の字幕がオンになっているか確かめてください"
    "（このままだと議事録に何も残りません）。誰も話していないだけなら気にしなくて大丈夫です。"
)
JOIN_WHILE_CLOSING = "前の会議を保存中です。数秒後にもう一度お試しください。"
ALREADY_ACTIVE = "すでに会議に参加しています。"
LOGIN_WHILE_ACTIVE = "会議に参加している間は、ログイン画面を開けません。"
LOGIN_HINT = "「ボット用ブラウザで Google にログイン…」でログインすると入れることがあります。"

LOGIN_UNKNOWN = "未確認"
LOGIN_CHECKING = "確認中…"
LOGIN_DONE = "ログイン済み"
LOGIN_NONE = "ログインしていません"
LOGIN_WAITING = "ログイン待ち"

REASON_LEFT = "会議から退出しました"
REASON_ENDED = "会議が終了しました"
REASON_REMOVED = "会議から外されました"


def state_text(state: str, detail: str = "") -> str:
    """
    状態を画面に出す 1 行にする。

    Args:
        state: `STATE_*`
        detail: 状態ごとの補足（準備の段階・会議名・終了の理由・失敗の説明）

    Returns:
        表示する文
    """
    if state == STATE_LAUNCHING:
        return "ブラウザを準備中…"
    if state == STATE_JOINING:
        return f"参加の準備中 — {detail}"
    if state == STATE_WAITING_APPROVAL:
        return "ホストの承認待ち"
    if state == STATE_RECORDING:
        return f"参加中 — {detail}"
    if state == STATE_ENDED:
        return f"終了 — {detail}"
    if state == STATE_FAILED:
        return f"エラー — {detail}"
    return "参加していません"


def waiting_status_line(mode: str) -> str:
    """承認待ちのあいだの状態行（待つ上限を添える）。"""
    return f"主催者の承認待ち（{int(approval_wait_limit(mode) / 60)} 分まで待ちます）"


def recording_status_line(lines: int, meeting_name: str, warning: bool = False, save_error: Optional[str] = None) -> str:
    """
    記録中の状態行を作る。

    Args:
        lines: これまでに書いた行数
        meeting_name: 会議名
        warning: 字幕が取れていないと知らせている最中か
        save_error: 議事録の書き込みに失敗していれば、その理由

    Returns:
        表示する文
    """
    if save_error:
        return f"議事録を保存できません — {save_error}"
    if warning:
        return f"{CAPTION_MISSING} — {meeting_name}"
    return f"記録中 {lines} 行 — {meeting_name}"


def closing_message(reason: str, file_started: bool, lines: int, save_error: Optional[str]) -> str:
    """
    記録を閉じたときの文言を作る。

    「保存できなかった」と「保存はできたが 0 行だった」を分ける（本人が、字幕が取れていなかったのか
    保存に失敗したのかを見分けられるように）。

    Args:
        reason: 閉じた理由（「会議から退出しました」など）
        file_started: この会議の議事録ファイルを作れたか
        lines: 書き込みがすべて終わった時点の行数
        save_error: 書き込みが失敗していれば、その理由

    Returns:
        表示する文
    """
    if save_error or not file_started:
        return f"{reason}。議事録を保存できませんでした: {save_error or '保存先を作れませんでした'}"
    if lines == 0:
        return f"{reason}。議事録の記録は 0 行でした（Meet の字幕が出ていたか確かめてください）。"
    return f"{reason}。議事録を保存しました（{lines} 行）。"


def waiting_notice(mode: str, display_name: str = BOT_DISPLAY_NAME) -> str:
    """
    承認待ちに入ったときの知らせを作る。

    ゲストのときは、主催者の画面に出る名前と待つ上限を添える（誰のリクエストか分からないと承認されない）。
    """
    if mode == MODE_GUEST:
        return (
            f"ゲストとして参加をリクエストしました。会議の画面で「{display_name}」を承認してください"
            f"（{int(approval_wait_limit(mode) / 60)} 分まで待ちます）。"
        )
    return "ホストの承認を待っています。会議の画面でボットの参加を承認してください。"


def join_failure_notice(error: Exception) -> str:
    """
    参加できなかったときの知らせを作る（理由ごとに、本人が次にやることを添える）。

    Args:
        error: 失敗

    Returns:
        表示する文
    """
    kind = error.kind if isinstance(error, MeetBotError) else None
    if kind == MeetBotError.DENIED:
        return "参加を断られました。ホストに承認してもらうか、会議の URL を確かめてください。"
    if kind == MeetBotError.GUEST_NOT_APPROVED:
        return "主催者に承認されませんでした。もう一度参加して承認してもらってください。" + LOGIN_HINT
    if kind == MeetBotError.GUEST_NOT_ALLOWED:
        return "この会議はゲストが入れません。ボットに Google ログインをさせると入れます。" + LOGIN_HINT
    message = error.message if isinstance(error, MeetBotError) else str(error)
    return f"会議に参加できませんでした: {message}"


def caption_language_label(language: str) -> str:
    """議事録のヘッダに書く言語の表示（Mac 版と同じ形）。"""
    return f"Meet の字幕（{'英語' if language == 'en' else '日本語'}）"
