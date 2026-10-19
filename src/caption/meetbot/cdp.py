"""
Chrome DevTools Protocol（CDP）の最小クライアント — Mac 版 ChromeDevTools.swift の移植

Google Meet へ入るボットのために、**専用プロファイルの Chrome を 1 つ起動して外から操作する**。
Playwright や Node を同梱すると Chromium ごと数百 MB 抱えることになるため、**すでに入っている Chrome を
DevTools プロトコルで動かす**方式にした（追加依存ゼロ。HTTP は httpx、WebSocket は websockets）。

使うのは:
- HTTP `/json/version`（起動待ち）・`/json/new`（タブを開く・PUT）・`/json`（タブ一覧）
- WebSocket の `Runtime.evaluate`（ページ内で JS を実行）
- ブラウザ全体への `Storage.getCookies`（ログイン済みかの確認）・`Browser.setPermission`（カメラ・マイクを拒否）
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from ...utils.logger import get_logger
from . import rules

logger = get_logger(__name__)

# 命令 1 つあたりの応答待ちの上限（秒）。Chrome が固まると応答が永久に来ないので必ず切る
DEFAULT_TIMEOUT = 10.0


class ChromeDevToolsError(Exception):
    """CDP の失敗（kind: chromeNotFound / devToolsUnavailable / protocol / malformed / timedOut）。"""

    MESSAGES = {
        "chromeNotFound": "Google Chrome が見つかりません",
        "devToolsUnavailable": "Chrome の DevTools に接続できませんでした",
        "malformed": "Chrome から想定外の応答が返りました",
        "timedOut": "Chrome が応答しません",
    }

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind
        message = self.MESSAGES.get(kind) or f"Chrome の操作に失敗しました: {detail}"
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


def chrome_candidates() -> List[str]:
    """Chrome の実行ファイルの候補（標準のインストール先）。"""
    if sys.platform == "darwin":
        return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    if sys.platform != "win32":
        return ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]
    paths = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            paths.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    return paths


def chrome_executable() -> Optional[str]:
    """Chrome の実行ファイル（見つからなければ None）。"""
    for path in chrome_candidates():
        if os.path.isfile(path):
            return path
    return None


def is_chrome_installed() -> bool:
    return chrome_executable() is not None


def launch_args(executable: str, port: int, profile_dir: str, headless: bool,
                initial_url: Optional[str] = None, mute_audio: bool = False) -> List[str]:
    """Chrome の起動引数（純関数・テスト対象）。

    普段使いの Chrome とは**別の --user-data-dir** を使う（作業中のタブを巻き込まない・ログインを分けて持つ）。
    カメラ・マイクの許可は「出さずに拒否」（ボットは聞くだけ。偽の映像・音声を流さない）。
    """
    args = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--deny-permission-prompts",
        "--autoplay-policy=no-user-gesture-required",
    ]
    if headless:
        args.append("--headless=new")
    if mute_audio:
        # 字幕を読むだけなら音は要らない（付けないと会議の音がスピーカーから流れる）。
        # 会議の音声を API で文字起こしする経路は Chrome の音を拾うので付けない
        args.append("--mute-audio")
    if initial_url:
        args.append(initial_url)
    return args


def launch_chrome(port: int, profile_dir: Path, headless: bool, initial_url: Optional[str] = None,
                  mute_audio: bool = False) -> subprocess.Popen:
    """専用プロファイルの Chrome を起動する。"""
    executable = chrome_executable()
    if executable is None:
        raise ChromeDevToolsError("chromeNotFound")
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = launch_args(executable, port, str(profile_dir), headless, initial_url, mute_audio)
    kwargs: Dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                              "stdin": subprocess.DEVNULL}
    if sys.platform == "win32" and headless:
        # 見えない Chrome はコンソールの窓も出さない
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(args, **kwargs)


def _http(path: str, port: int, method: str = "GET", timeout: float = 5.0) -> Any:
    """DevTools の HTTP エンドポイントを叩いて JSON を返す。"""
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        resp = client.request(method, f"http://127.0.0.1:{port}{path}")
        resp.raise_for_status()
        return resp.json()


def wait_for_devtools(port: int, timeout: float = 20.0, should_stop: Callable[[], bool] = lambda: False) -> None:
    """DevTools が応答するまで待つ。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if should_stop():
            raise ChromeDevToolsError("devToolsUnavailable")
        try:
            _http("/json/version", port)
            return
        except Exception:
            time.sleep(0.3)
    raise ChromeDevToolsError("devToolsUnavailable")


def is_answering(port: int) -> bool:
    """そのポートで DevTools が応答しているか（別のアプリがポートを使っていないかの確認）。"""
    try:
        _http("/json/version", port, timeout=1.5)
        return True
    except Exception:
        return False


def open_tab(url: str, port: int) -> str:
    """新しいタブで URL を開き、そのタブの WebSocket URL を返す（/json/new は PUT でないと 405）。"""
    from urllib.parse import quote

    data = _http("/json/new?" + quote(url, safe=":/?&=%#"), port, method="PUT")
    socket = data.get("webSocketDebuggerUrl") if isinstance(data, dict) else None
    if not socket:
        raise ChromeDevToolsError("malformed")
    return socket


def list_page_urls(port: int) -> List[str]:
    """開いているページの URL 一覧（ログイン窓が閉じられたかの判定に使う）。"""
    data = _http("/json", port)
    if not isinstance(data, list):
        raise ChromeDevToolsError("malformed")
    return [str(item.get("url", "")) for item in data if isinstance(item, dict) and item.get("type") == "page"]


def browser_socket_url(port: int) -> str:
    """ブラウザ全体（タブではない）を操作する WebSocket の URL。"""
    data = _http("/json/version", port)
    socket = data.get("webSocketDebuggerUrl") if isinstance(data, dict) else None
    if not socket:
        raise ChromeDevToolsError("malformed")
    return socket


# ----------------------------------------------------------------------
# 取り残された Chrome の片付け
# ----------------------------------------------------------------------

def _process_listing() -> str:
    """全プロセスの "PID コマンドライン" の一覧（Windows は PowerShell の CIM で読む）。"""
    try:
        if sys.platform == "win32":
            command = [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }",
            ]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(command, capture_output=True, text=True, timeout=10, creationflags=flags,
                                    encoding="utf-8", errors="replace")
        else:
            result = subprocess.run(["ps", "-axww", "-o", "pid=,args="], capture_output=True, text=True, timeout=10)
        return result.stdout or ""
    except Exception:
        return ""


def terminate_stale_chrome(profile_dir: Path) -> int:
    """ボット用プロファイルを掴んだまま残っている Chrome を終わらせる（普段の Chrome には絶対に触らない）。

    前回の voicekey が落ちた等で残っていると、新しく起こした Chrome はそちらへ処理を渡してすぐ終了し、
    ボットは何もできなくなる。**同じプロファイルを使っているものだけ**を対象にする。
    """
    pids = rules.stale_chrome_pids(_process_listing(), str(profile_dir), own_pid=os.getpid())
    if not pids:
        return 0
    for pid in pids:
        _terminate_pid(pid)
    return len(pids)


def _terminate_pid(pid: int) -> None:
    try:
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=5,
                           creationflags=flags)
        else:
            import signal

            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def terminate_and_wait(process: Optional[subprocess.Popen], limit: float = 5.0) -> None:
    """Chrome を終わらせ、終わるまで待つ（上限つき）。"""
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=limit)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


# ----------------------------------------------------------------------
# WebSocket（1 つのページ・ブラウザに繋いで命令を流す）
# ----------------------------------------------------------------------

class ChromeDevTools:
    """CDP の WebSocket クライアント（id を振った命令と同じ id の応答の往復）。

    受信は専用スレッドで回し、応答を id ごとの待ち手へ渡す。命令は必ず上限時間つきで待つ。

    Attributes:
        on_disconnect: 接続が向こうから切れた（Chrome が落ちた・タブが閉じられた）。受信スレッドから呼ぶ
    """

    def __init__(self) -> None:
        self._ws = None
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._next_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._closed_by_us = False
        self._thread: Optional[threading.Thread] = None
        self.on_disconnect: Optional[Callable[[], None]] = None

    def connect(self, socket_url: str, open_timeout: float = 5.0) -> None:
        from ...core.live_transcriber import open_websocket

        try:
            self._ws = open_websocket(socket_url, None, open_timeout=open_timeout)
        except Exception as e:
            raise ChromeDevToolsError("devToolsUnavailable") from e
        self._thread = threading.Thread(target=self._receive_loop, daemon=True, name="ChromeDevTools")
        self._thread.start()

    def close(self) -> None:
        with self._lock:
            self._closed_by_us = True
            waiting = list(self._pending.values())
            self._pending.clear()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        for slot in waiting:
            slot["error"] = ChromeDevToolsError("devToolsUnavailable")
            slot["event"].set()

    def send(self, method: str, params: Optional[dict] = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
        """CDP の命令を 1 つ送って結果（result の中身）を待つ。期限を過ぎたら timedOut。"""
        ws = self._ws
        if ws is None:
            raise ChromeDevToolsError("devToolsUnavailable")
        slot: Dict[str, Any] = {"event": threading.Event(), "result": None, "error": None}
        with self._lock:
            message_id = self._next_id
            self._next_id += 1
            self._pending[message_id] = slot
        payload = json.dumps({"id": message_id, "method": method, "params": params or {}})
        try:
            with self._send_lock:
                ws.send(payload)
        except Exception as e:
            with self._lock:
                self._pending.pop(message_id, None)
            raise ChromeDevToolsError("devToolsUnavailable") from e
        if not slot["event"].wait(timeout):
            with self._lock:
                self._pending.pop(message_id, None)
            raise ChromeDevToolsError("timedOut")
        if slot["error"] is not None:
            raise slot["error"]
        return slot["result"] or {}

    def evaluate(self, expression: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
        """ページ内で JS を実行して結果（JSON 化できる値）を返す（Promise は待つ）。"""
        result = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": True,
        }, timeout=timeout)
        if isinstance(result.get("exceptionDetails"), dict):
            text = result["exceptionDetails"].get("text") or "JS の実行に失敗しました"
            raise ChromeDevToolsError("protocol", str(text))
        value = result.get("result")
        if not isinstance(value, dict):
            raise ChromeDevToolsError("malformed")
        return value.get("value")

    def _receive_loop(self) -> None:
        ws = self._ws
        while ws is not None:
            try:
                message = ws.recv()
            except Exception:
                break
            try:
                data = json.loads(message)
            except Exception:
                continue
            if not isinstance(data, dict) or not isinstance(data.get("id"), int):
                continue  # イベント（id 無し）は使っていないので捨てる
            with self._lock:
                slot = self._pending.pop(data["id"], None)
            if slot is None:
                continue
            error = data.get("error")
            if isinstance(error, dict):
                slot["error"] = ChromeDevToolsError("protocol", str(error.get("message") or "unknown"))
            else:
                slot["result"] = data.get("result") if isinstance(data.get("result"), dict) else {}
            slot["event"].set()
        with self._lock:
            by_us = self._closed_by_us
            waiting = list(self._pending.values())
            self._pending.clear()
        for slot in waiting:
            slot["error"] = ChromeDevToolsError("devToolsUnavailable")
            slot["event"].set()
        if not by_us:
            logger.info("DevTools の接続が切れました")
            callback = self.on_disconnect
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass
