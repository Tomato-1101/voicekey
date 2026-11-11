"""
ボット用 Chrome の起動と DevTools（CDP）の通信

Playwright や Selenium は入れない（ブラウザごと数百 MB になるため）。入っている Chrome を
リモートデバッグ付きで起動し、DevTools の HTTP と WebSocket を直接たたく。使うのはこれだけ:
- HTTP `/json/version`（起動待ち・ブラウザ全体の接続先）・`/json/new`（タブを開く）・`/json`（タブを数える）
- WebSocket の `Runtime.evaluate`（ページ内で JS を実行）
- ブラウザ全体への `Storage.getCookies`（ログイン済みかの確認）・`Browser.setPermission`
  （カメラ・マイクを拒否）・`Browser.close`（Cookie を保存させてから終了）

ボット用 Chrome は**必ず専用プロファイル**で起動する。普段使いの Chrome を巻き込まない。

通信は呼び出したスレッドの上で「送って、その返事が来るまで読む」だけの同期処理にしてある。
ボットは 1 本の作業スレッドから順番に命令を出すので、受信用のスレッドや非同期の仕組みは要らない。
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..utils.logger import get_logger

logger = get_logger(__name__)

# DevTools の待ち受けポート（Mac 版と同じ。普段の Chrome のリモートデバッグ既定 9222 とは分ける）
DEVTOOLS_PORT = 9333
# 命令の応答待ちの上限（秒）
COMMAND_TIMEOUT = 10.0
# HTTP の応答待ちの上限（秒）
_HTTP_TIMEOUT = 5.0

# OS のプロキシ設定を通さない（相手はこの PC の中の Chrome。プロキシ経由だと届かない）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ChromeNotFound(Exception):
    """Google Chrome が見つからない。"""

    def __init__(self) -> None:
        super().__init__("Google Chrome が見つかりません")


class CDPError(Exception):
    """DevTools の通信・命令の失敗。"""


class CDPTimeout(CDPError):
    """命令の応答が時間内に来なかった。"""

    def __init__(self) -> None:
        super().__init__("Chrome が応答しません")


class CDPDisconnected(CDPError):
    """Chrome との接続が切れた（Chrome が終了した・タブが閉じた）。"""

    def __init__(self) -> None:
        super().__init__("ブラウザとの接続が切れました")


# ============================================
# Chrome の場所と専用プロファイル
# ============================================
def _registry_chrome_path() -> Optional[str]:
    """Windows のレジストリ（App Paths）に登録された chrome.exe の場所を返す。"""
    try:
        import winreg
    except ImportError:
        return None
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, key_path) as key:
                value, _ = winreg.QueryValueEx(key, None)
        except OSError:
            continue
        if value and os.path.isfile(value):
            return str(value)
    return None


def chrome_candidates() -> List[str]:
    """
    Chrome の実行ファイルがありそうな場所を、探す順に返す。

    Returns:
        候補のパス（存在するとは限らない）
    """
    if sys.platform == "win32":
        tail = os.path.join("Google", "Chrome", "Application", "chrome.exe")
        roots = [os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        return [os.path.join(root, tail) for root in roots if root]
    if sys.platform == "darwin":
        tail = "Google Chrome.app/Contents/MacOS/Google Chrome"
        return [f"/Applications/{tail}", str(Path.home() / "Applications" / tail)]
    return [path for path in (shutil.which("google-chrome"), shutil.which("google-chrome-stable")) if path]


def find_chrome() -> Optional[str]:
    """
    Chrome の実行ファイルを探す。

    Windows はレジストリの登録（インストーラーが書く）を先に見て、無ければ標準の場所を見る。

    Returns:
        実行ファイルのパス（見つからなければ None）
    """
    if sys.platform == "win32":
        registered = _registry_chrome_path()
        if registered:
            return registered
    for candidate in chrome_candidates():
        if os.path.isfile(candidate):
            return candidate
    return None


def profile_directory() -> Path:
    """
    ボット用 Chrome の専用プロファイルの場所を返す。

    Returns:
        Windows は `%APPDATA%\\voicekey\\meetbot-profile`、Mac は
        `~/Library/Application Support/voicekey/meetbot-profile`
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "voicekey" / "meetbot-profile"


def launch_arguments(port: int, profile: Path, headless: bool, mute: bool, initial_url: Optional[str]) -> List[str]:
    """
    Chrome の起動引数を作る。

    Args:
        port: DevTools の待ち受けポート
        profile: 専用プロファイルのフォルダ
        headless: 画面を出さないか（会議への参加は出さない・ログインは出す）
        mute: 音を出さないか（字幕を読むだけなので、会議の音を PC から鳴らさない）
        initial_url: 最初に開く URL（ログイン画面を出すとき）

    Returns:
        実行ファイルを除いた引数
    """
    arguments = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--deny-permission-prompts",
        "--autoplay-policy=no-user-gesture-required",
    ]
    if headless:
        arguments.append("--headless=new")
    if mute:
        arguments.append("--mute-audio")
    if initial_url:
        arguments.append(initial_url)
    return arguments


# ============================================
# DevTools の HTTP
# ============================================
def http_json(port: int, path: str, method: str = "GET") -> Any:
    """
    DevTools の HTTP を呼んで JSON を読む。

    Args:
        port: DevTools のポート
        path: `/json/version` など
        method: HTTP メソッド（`/json/new` は PUT でないと 405 を返す）

    Returns:
        応答の JSON

    Raises:
        CDPError: つながらない・JSON でない
    """
    # localhost ではなく 127.0.0.1（Windows は localhost が IPv6 に解決されて届かないことがある）
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with _OPENER.open(request, timeout=_HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError):
        raise CDPError("Chrome の DevTools に接続できませんでした") from None
    except ValueError:
        raise CDPError("Chrome から想定外の応答が返りました") from None


def _connect_websocket(url: str) -> Any:
    """DevTools の WebSocket へつなぐ（既存依存の websockets の同期クライアント）。"""
    from websockets.sync.client import connect

    # max_size=None: 画面の文字をまとめて返す応答は既定の 1MB を超えうる
    return connect(url, max_size=None, open_timeout=_HTTP_TIMEOUT, proxy=None)


# ============================================
# DevTools の WebSocket
# ============================================
class CDPSession:
    """
    DevTools の WebSocket 1 本（タブ 1 つ、またはブラウザ全体）。

    1 本のスレッドから順番に使う。命令を送ったら、その返事（同じ id）が来るまで読み、
    あいだに届くイベント（id の無い知らせ）は捨てる。
    """

    def __init__(self, url: str, connect: Optional[Callable[[str], Any]] = None):
        """
        Args:
            url: WebSocket の接続先（`webSocketDebuggerUrl`）
            connect: 接続の作り方（テストで偽物に差し替える）

        Raises:
            CDPError: つながらなかった
        """
        try:
            self._socket = (connect or _connect_websocket)(url)
        except Exception:
            raise CDPError("Chrome の DevTools に接続できませんでした") from None
        self._next_id = 0
        self._closed = False

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = COMMAND_TIMEOUT) -> Dict[str, Any]:
        """
        命令を 1 つ送り、返事を待つ。

        Args:
            method: `Runtime.evaluate` など
            params: パラメータ
            timeout: 応答待ちの上限（秒）

        Returns:
            返事の `result`

        Raises:
            CDPTimeout: 時間内に返事が来なかった
            CDPDisconnected: 接続が切れた
            CDPError: Chrome が命令を失敗として返した
        """
        if self._closed:
            raise CDPDisconnected()
        self._next_id += 1
        command_id = self._next_id
        try:
            self._socket.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
        except Exception:
            raise CDPDisconnected() from None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPTimeout()
            try:
                raw = self._socket.recv(timeout=remaining)
            except TimeoutError:
                raise CDPTimeout() from None
            except Exception:
                raise CDPDisconnected() from None
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            # イベント（id 無し）と、時間切れで諦めた前の命令の遅れた返事は捨てる
            if not isinstance(message, dict) or message.get("id") != command_id:
                continue
            if "error" in message:
                error = message["error"]
                detail = error.get("message") if isinstance(error, dict) else error
                raise CDPError(f"Chrome の操作に失敗しました: {detail}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def evaluate(self, java_script: str, timeout: float = COMMAND_TIMEOUT) -> Any:
        """
        ページ内で JavaScript を実行し、値を受け取る。

        Args:
            java_script: 実行する式（Promise を返す式は解決を待つ）
            timeout: 応答待ちの上限（秒）

        Returns:
            式の値（JSON にできるもの）

        Raises:
            CDPError: 通信の失敗、またはスクリプトが例外を投げた
        """
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": java_script,
                "returnByValue": True,
                "awaitPromise": True,
                # ボタンを押す操作を「利用者の操作」として扱わせる（Meet が自動操作を無視しないように）
                "userGesture": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise CDPError("Chrome の操作に失敗しました: スクリプトが例外を投げました")
        value = result.get("result")
        return value.get("value") if isinstance(value, dict) else None

    def close(self) -> None:
        """接続を閉じる（何度呼んでもよい・別のスレッドから呼んでもよい）。"""
        self._closed = True
        try:
            self._socket.close()
        except Exception:
            pass


# ============================================
# ボット用 Chrome
# ============================================
class Browser:
    """
    ボット用 Chrome 1 つ（専用プロファイル＋リモートデバッグ）。

    Attributes:
        port: DevTools のポート
        profile: 専用プロファイルのフォルダ
    """

    def __init__(
        self,
        port: int = DEVTOOLS_PORT,
        profile: Optional[Path] = None,
        executable: Optional[str] = None,
        popen: Callable[..., Any] = subprocess.Popen,
        http: Callable[..., Any] = http_json,
        connect: Optional[Callable[[str], Any]] = None,
    ):
        """
        Args:
            port: DevTools のポート
            profile: 専用プロファイルのフォルダ（省略時は `profile_directory()`）
            executable: Chrome の実行ファイル（省略時は起動のときに探す）
            popen: プロセスの起こし方（テストで差し替える）
            http: DevTools の HTTP の呼び方（テストで差し替える）
            connect: WebSocket の接続の作り方（テストで差し替える）
        """
        self.port = port
        self.profile = Path(profile) if profile is not None else profile_directory()
        self._executable = executable
        self._popen = popen
        self._http = http
        self._connect = connect
        self._process: Any = None
        self._close_lock = threading.Lock()

    # ---- 起動 ----
    def launch(self, headless: bool, mute: bool, initial_url: Optional[str] = None) -> None:
        """
        Chrome を起動する。

        Args:
            headless: 画面を出さないか
            mute: 音を出さないか
            initial_url: 最初に開く URL

        Raises:
            ChromeNotFound: Chrome が入っていない
            OSError: プロファイルを作れない・起動できない
        """
        executable = self._executable or find_chrome()
        if not executable:
            raise ChromeNotFound()
        self.profile.mkdir(parents=True, exist_ok=True)
        arguments = [executable] + launch_arguments(self.port, self.profile, headless, mute, initial_url)
        self._process = self._popen(
            arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info(f"ボット用 Chrome を起動しました（headless={headless}）")

    def is_running(self) -> bool:
        """このオブジェクトが起動した Chrome が動いているか。"""
        return self._process is not None and self._process.poll() is None

    def is_answering(self) -> bool:
        """DevTools のポートが応答しているか（起動待ち・ポートの先客の確認に使う）。"""
        try:
            self._http(self.port, "/json/version")
            return True
        except CDPError:
            return False

    def close_leftover(self) -> bool:
        """
        前回の実行で取り残されたボット用 Chrome を終了する。

        アプリが落ちるとボット用 Chrome だけが残り、同じプロファイルを掴んだままになる。
        ポートの先にいるのが**このプロファイルで起動した Chrome**だと確かめられたときだけ終了させる
        （Chrome はリモートデバッグ付きで起動すると、プロファイルの `DevToolsActivePort` に
        ポートと接続先を書く。それがポートの応答と一致すれば自分の取り残し）。
        プロセスを名前で探して落とすことはしない＝普段使いの Chrome には触らない。

        Returns:
            終了させたら True
        """
        try:
            lines = (self.profile / "DevToolsActivePort").read_text(encoding="utf-8").split()
            if len(lines) < 2 or lines[0] != str(self.port):
                return False
            version = self._http(self.port, "/json/version")
            socket_url = str(version.get("webSocketDebuggerUrl") or "") if isinstance(version, dict) else ""
            if not socket_url.endswith(lines[1]):
                return False
            session = CDPSession(socket_url, self._connect)
            try:
                session.call("Browser.close", timeout=_HTTP_TIMEOUT)
            except CDPDisconnected:
                pass  # 閉じる途中で切れるのは成功
            finally:
                session.close()
        except (OSError, CDPError):
            return False
        logger.info("取り残されたボット用 Chrome を終了しました")
        # ポートが空くまで少し待つ（すぐ次を起動すると、まだ掴まれていて起動に失敗する）
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and self.is_answering():
            time.sleep(0.2)
        return True

    # ---- ブラウザ全体への命令 ----
    def _browser_call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """ブラウザ全体（タブではない）へ命令を 1 つ送る。"""
        version = self._http(self.port, "/json/version")
        socket_url = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
        if not socket_url:
            raise CDPError("Chrome から想定外の応答が返りました")
        session = CDPSession(str(socket_url), self._connect)
        try:
            return session.call(method, params)
        finally:
            session.close()

    def cookies(self) -> List[Dict[str, Any]]:
        """
        ボット用プロファイルの Cookie を読む（ログイン済みかの確認用。値は使わない）。

        Returns:
            Cookie の配列

        Raises:
            CDPError: 読めなかった
        """
        cookies = self._browser_call("Storage.getCookies").get("cookies")
        return cookies if isinstance(cookies, list) else []

    def deny_media(self, origin: str) -> None:
        """
        カメラとマイクを、そのサイトで拒否にする（ボットは聞くだけ。許可の確認も出させない）。

        Args:
            origin: 対象のサイト（`https://meet.google.com`）

        Raises:
            CDPError: 設定できなかった
        """
        for name in ("camera", "microphone"):
            self._browser_call(
                "Browser.setPermission",
                {"permission": {"name": name}, "setting": "denied", "origin": origin},
            )

    # ---- タブ ----
    def open_page(self, url: str) -> CDPSession:
        """
        新しいタブで URL を開き、そのタブへつなぐ。

        Args:
            url: 開く URL

        Returns:
            タブへの接続

        Raises:
            CDPError: 開けなかった・つながらなかった
        """
        encoded = urllib.parse.quote(url, safe=":/?&=%#@+,;~-._")
        tab = self._http(self.port, f"/json/new?{encoded}", "PUT")
        socket_url = tab.get("webSocketDebuggerUrl") if isinstance(tab, dict) else None
        if not socket_url:
            raise CDPError("Chrome から想定外の応答が返りました")
        return CDPSession(str(socket_url), self._connect)

    def page_count(self) -> int:
        """
        開いているタブの数を返す（ログイン画面が閉じられたかの確認用）。

        Raises:
            CDPError: 読めなかった
        """
        tabs = self._http(self.port, "/json")
        if not isinstance(tabs, list):
            raise CDPError("Chrome から想定外の応答が返りました")
        return sum(1 for tab in tabs if isinstance(tab, dict) and tab.get("type") == "page")

    # ---- 終了 ----
    def close(self) -> None:
        """
        Chrome を終了する（何度呼んでもよい・別のスレッドから呼んでもよい）。

        まず `Browser.close` で自分から終わらせる（ログインの Cookie をディスクへ書かせるため）。
        数秒で終わらなければプロセスを止める。
        """
        with self._close_lock:
            process, self._process = self._process, None
            if process is None or process.poll() is not None:
                return
            try:
                self._browser_call("Browser.close")
            except CDPError:
                pass  # 応答しない・閉じる途中で切れた、はどちらも下の待ちで拾う
            try:
                process.wait(timeout=3.0)
            except Exception:
                try:
                    process.terminate()
                    process.wait(timeout=2.0)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
            logger.info("ボット用 Chrome を終了しました")
