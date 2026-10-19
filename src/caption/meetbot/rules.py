"""
Meet ボットの判断（純ロジック）— Mac 版 MeetBotScripts.swift の MeetBotRules と MeetBotPolicies.swift、
MeetBotService.swift の URL 判定・世代・エラー文言の移植

Meet の**表示文言への依存**はここと scripts.py だけに置く（壊れたらこの 2 つだけ直す）。
回数・時間の数え方と通知の文言もここに切り出し、ブラウザ無しで回帰テストできるようにしてある。
"""

import re
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

# ----------------------------------------------------------------------
# 会議画面の状態
# ----------------------------------------------------------------------

IN_MEETING = "inMeeting"
WAITING_APPROVAL = "waitingApproval"
DENIED = "denied"
REMOVED = "removed"
ENDED = "ended"
NOT_FOUND = "notFound"
OTHER = "other"


def _matches(text: str, words: Sequence[str]) -> bool:
    lowered = (text or "").lower()
    return any(word.lower() in lowered for word in words)


def page_status(in_meeting: bool, text: str) -> str:
    """画面の文字から会議の状態を分類する（承認待ちと拒否を取り違えないよう、先に「終わり」系を見る）。"""
    if in_meeting:
        return IN_MEETING
    if _matches(text, [
        "参加リクエストが拒否", "リクエストが拒否されました", "参加を拒否", "この通話に参加できません",
        "denied your request", "You can't join this call", "can't join this video call",
    ]):
        return DENIED
    # 「削除されました」だけだとチャットのメッセージ削除でも当たる。会議・通話から外された文言だけに絞る
    if _matches(text, [
        "通話から削除されました", "会議から削除されました", "退出させられました",
        "You've been removed from", "You have been removed from", "removed you from the",
    ]):
        return REMOVED
    if _matches(text, ["会議コードを確認", "無効なビデオ通話名", "Check your meeting code", "Invalid video call name"]):
        return NOT_FOUND
    if _matches(text, [
        "通話は終了しました", "会議は終了しました", "通話から退出しました", "通話を終了しました",
        "The call has ended", "This call has ended", "You left the meeting", "You've left the meeting",
        "The meeting has ended",
    ]):
        return ENDED
    if _matches(text, [
        "参加をリクエストしています", "参加を許可すると", "承認されるまで", "参加の承認を待って",
        "Asking to be let in", "when someone lets you in", "Waiting for the host", "Someone will let you in",
    ]):
        return WAITING_APPROVAL
    return OTHER


def caption_toggle(buttons: List[Tuple[str, Optional[bool]]], region_visible: bool,
                   shortcut_tried: bool) -> Tuple[str, Optional[str]]:
    """字幕ボタンの状態から、押すべきかを決める（間違えてオフにしないことを最優先）。

    Args:
        buttons: 字幕に関係するボタン (ラベル, aria-pressed)
        region_visible: 字幕の表示欄が見えているか
        shortcut_tried: ショートカット（c）を既に試したか

    Returns:
        ("alreadyOn", None) / ("click", ラベル) / ("shortcut", None) / ("unknown", None)
    """
    if region_visible:
        return ("alreadyOn", None)
    related = [b for b in buttons if _matches(b[0], ["字幕", "caption", "subtitle"])]
    off_words = ["オフにする", "turn off", "hide", "無効にする"]
    if any(pressed is True or _matches(label, off_words) for label, pressed in related):
        return ("alreadyOn", None)
    on_words = ["オンにする", "turn on", "show", "有効にする"]
    for label, pressed in related:
        if _matches(label, on_words) or pressed is False:
            return ("click", label)
    return ("unknown", None) if shortcut_tried else ("shortcut", None)


def is_logged_in(cookies: List[dict], now: float) -> bool:
    """Google にログイン済みかを Cookie の名前と期限だけで判定する（値は見ない・ログに出さない）。"""
    names = {"SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID"}
    for cookie in cookies or []:
        if not isinstance(cookie, dict) or cookie.get("name") not in names:
            continue
        domain = str(cookie.get("domain") or "")
        if not (domain == "google.com" or domain.endswith(".google.com")):
            continue
        expires = cookie.get("expires")
        # セッション Cookie は expires が -1。正の値で過去なら期限切れ
        try:
            if expires is not None and float(expires) > 0 and float(expires) < now:
                continue
        except (TypeError, ValueError):
            pass
        return True
    return False


# ----------------------------------------------------------------------
# 回数・時間の数え方（MeetBotPolicies.swift）
# ----------------------------------------------------------------------

class TimeoutStreak:
    """「Chrome が応答しない」が続いた回数（上限回数続いたら諦める。1 回では止めない）。"""

    def __init__(self, limit: int = 3) -> None:
        self.limit = limit
        self.count = 0

    def record(self, timed_out: bool) -> bool:
        self.count = self.count + 1 if timed_out else 0
        return self.count >= self.limit


class MeetingPresenceWatch:
    """記録中の会議画面の見張り（「外された」は 2 回、それ以外の「会議画面でない」は 3 回続いたら確定）。"""

    REMOVED_LIMIT = 2
    NOT_IN_MEETING_LIMIT = 3

    def __init__(self) -> None:
        self._removed = 0
        self._not_in_meeting = 0

    def observe(self, status: str) -> Optional[str]:
        """終わりが確定したら REMOVED / ENDED を返す（まだなら None）。"""
        if status == IN_MEETING:
            self._removed = 0
            self._not_in_meeting = 0
            return None
        if status == REMOVED:
            self._removed += 1
            self._not_in_meeting += 1
            return REMOVED if self._removed >= self.REMOVED_LIMIT else None
        self._removed = 0
        self._not_in_meeting += 1
        return ENDED if self._not_in_meeting >= self.NOT_IN_MEETING_LIMIT else None


class CaptionPresenceWatch:
    """「Meet の字幕が読めていない」の見張り（参加から 60 秒ずっと読めなければ 1 会議につき 1 回だけ知らせる）。"""

    WARN_AFTER = 60.0

    def __init__(self, start: float) -> None:
        self._missing_since: Optional[float] = start
        self.warning = False
        self._warned = False

    def observe(self, found: bool, now: float) -> Optional[str]:
        """"warn" / "recovered" / None を返す（found は本文の有無ではなく字幕がオンか）。"""
        if found:
            self._missing_since = None
            if not self.warning:
                return None
            self.warning = False
            return "recovered"
        since = self._missing_since if self._missing_since is not None else now
        self._missing_since = since
        if self.warning or self._warned or now - since < self.WARN_AFTER:
            return None
        self.warning = True
        self._warned = True
        return "warn"


class JoinRetry:
    """参加ボタンを押し直す段取り（ロビーにいる間、4 秒おきに最大 5 回まで）。"""

    MAX_ATTEMPTS = 5
    INTERVAL = 4.0

    def __init__(self) -> None:
        self.attempts = 0
        self._last: Optional[float] = None
        self.button_found = False

    def should_press(self, status: str, now: float) -> bool:
        if status != OTHER or self.attempts >= self.MAX_ATTEMPTS:
            return False
        return self._last is None or now - self._last >= self.INTERVAL

    def record_press(self, found: bool, now: float) -> None:
        self.attempts += 1
        self._last = now
        if found:
            self.button_found = True


class JoinGeneration:
    """参加の試行の世代（退出・再参加のあとに古い試行の続きを捨てるため）。"""

    def __init__(self) -> None:
        self.current = 0

    def begin(self) -> int:
        self.current += 1
        return self.current

    def invalidate(self) -> None:
        self.current += 1

    def is_current(self, generation: int) -> bool:
        return generation == self.current


# ----------------------------------------------------------------------
# 失敗の種類と文言
# ----------------------------------------------------------------------

class MeetBotError(Exception):
    """ボット固有の失敗（通知の文言を出し分けるため種類を持つ）。"""

    MESSAGES: Dict[str, str] = {
        "superseded": "取り消されました",
        # 通知でも設定画面の状態行でも出すので、「この画面」のような今いる場所を指す言い方はしない
        "notLoggedIn": ("ボット用ブラウザが Google にログインしていません。"
                        "タスクトレイの「Meet 議事録ボット」→「ボット用ブラウザで Google にログイン…」でログインしてから、"
                        "もう一度参加してください"),
        "denied": "参加を断られました（ホストが拒否したか、外部の参加を受け付けない会議です）",
        "removed": "会議から外されました",
        "callEnded": "会議はすでに終わっています",
        "approvalTimeout": "10 分待っても参加を承認されませんでした",
        "couldNotEnter": "参加ボタンは押せましたが、90 秒たっても会議に入れませんでした",
        "joinButtonNotFound": "参加ボタンが見つかりませんでした（Meet の画面が変わった可能性があります）",
        "meetingNotFound": "この会議は見つかりませんでした。URL（会議コード）を確かめてください",
        "chromeNotResponding": "Chrome が応答しません",
    }

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind
        message = self.MESSAGES.get(kind, detail or kind)
        if kind == "portBusy":
            message = f"ポート {detail} を別のアプリが使っています（Chrome のリモートデバッグなど）"
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


def join_timeout_error(waiting_seen: bool, join_button_found: bool) -> MeetBotError:
    """入室を待ちきれなかったときの失敗を、実際の状況で出し分ける。"""
    if waiting_seen:
        return MeetBotError("approvalTimeout")
    return MeetBotError("couldNotEnter" if join_button_found else "joinButtonNotFound")


def closing_message(reason: str, file_started: bool, lines: int, save_error: Optional[str]) -> str:
    """記録を閉じたときの通知（「保存できなかった」と「0 行だった」を分ける）。"""
    if not file_started:
        return f"{reason}。議事録の保存はオフでした。"
    if save_error:
        return f"{reason}。議事録を保存できませんでした: {save_error}"
    if lines == 0:
        return f"{reason}。議事録の記録は 0 行でした（Meet の字幕が出ていたか確かめてください）。"
    return f"{reason}。議事録を保存しました（{lines} 行）。"


CAPTION_MISSING = "字幕が取れていません（Meet の字幕が ON か確認）"
JOIN_WHILE_CLOSING = "前の会議を保存中です。数秒後にもう一度お試しください。"
CAPTION_MISSING_NOTICE = (
    "会議の字幕が 1 分以上取れていません。Meet の字幕がオンになっているか確かめてください"
    "（このままだと議事録に何も残りません）。誰も話していないだけなら気にしなくて大丈夫です。"
)


# ----------------------------------------------------------------------
# URL
# ----------------------------------------------------------------------

_CODE_RE = re.compile(r"^/[a-z]{3}-?[a-z]{4}-?[a-z]{3}/?$", re.IGNORECASE)
_LOOKUP_RE = re.compile(r"^/lookup/[A-Za-z0-9_-]+/?$")


def is_meeting_path(path: str) -> bool:
    """会議を指すパスか（`/abc-defg-hij` か `/lookup/<名前>`）。トップや /landing では起動しない。"""
    return bool(_CODE_RE.match(path or "") or _LOOKUP_RE.match(path or ""))


def normalized_meet_url(raw: str) -> Optional[str]:
    """Meet の会議 URL として正しいものだけを通す（scheme 無しでも受ける）。"""
    text = (raw or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlparse(text)
    except Exception:
        return None
    if parsed.hostname != "meet.google.com" or not is_meeting_path(parsed.path):
        return None
    return text


def meeting_code(url: str) -> Optional[str]:
    """URL から会議コードを取り出す（`abc-defg-hij`）。"""
    try:
        code = urlparse(url).path.strip("/")
    except Exception:
        return None
    return code or None


# ----------------------------------------------------------------------
# 取り残された Chrome の判定
# ----------------------------------------------------------------------

def stale_chrome_pids(listing: str, profile_path: str, own_pid: int = -1) -> List[int]:
    """プロセス一覧（"PID コマンドライン" の行）から、ボット用プロファイルを使う Chrome の PID を拾う。

    `--user-data-dir=<パス>`（引用符つきも）が**完全一致**するものだけ。`meetbot-profile2` のような似た名前や、
    プロファイル指定の無い普段の Chrome は拾わない。直後が行末か次のフラグの始まり（` -`）であることを確かめる。
    Windows のパスは大文字小文字を区別しないので小文字で比べる。
    """
    needles = [f"--user-data-dir={profile_path}".lower(), f'--user-data-dir="{profile_path}"'.lower()]
    pids: List[int] = []
    for raw in (listing or "").splitlines():
        line = raw.strip()
        if " " not in line:
            continue
        head, args = line.split(" ", 1)
        try:
            pid = int(head)
        except ValueError:
            continue
        if pid == own_pid or "chrome" not in args.lower():
            continue
        lowered = args.lower()
        found = False
        for needle in needles:
            start = 0
            while True:
                index = lowered.find(needle, start)
                if index < 0:
                    break
                after = lowered[index + len(needle):]
                if not after or after.startswith(" -") or after.startswith(" --"):
                    found = True
                    break
                start = index + len(needle)
            if found:
                break
        if found:
            pids.append(pid)
    return pids
