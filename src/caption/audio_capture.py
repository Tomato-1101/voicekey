"""
システム音声の取り込み（Windows の WASAPI ループバック）

Mac 版 SystemAudioTap.swift / CaptureScope.swift の Windows 版。字幕と Meet ボットが使う。

取り方は 2 段構え:
1. **Process Loopback**（Windows 10 2004 以降の `ActivateAudioInterfaceAsync` + `VAD\\Process_Loopback`）。
   アプリ（プロセスの木）単位で拾える。「最前面のアプリだけ」（既定）はこれで最前面のアプリの音だけを拾い、
   「すべてのアプリ」は **voicekey 自身を除外**して拾う（読み上げ・効果音を拾い直すループを作らない）。
   Meet ボットはボット用 Chrome のプロセスの木だけを拾う。
2. 上が使えない（古い Windows・初期化失敗）ときは **既定の出力デバイス全体のループバック**（PyAudioWPatch）。
   この場合は voicekey 自身の音も混ざりうる（字幕は音声入力中は隠れるので実害は小さい）。

**作り直しにはすべて上限を置く**（Mac の HAL 事故の教訓。ループさせない）:
最前面の切り替えに追従するときは、同じアプリが 1 秒続いてから・前回の作り直しから 2 秒以上あけて張り替える。
取り込みが落ちたときの作り直しは 5 回まで（指数バックオフ）。

**無音＝壊れているとは判定しない**（ループバックは誰も鳴らしていない間パケットを 1 つも配らない）。

出力は 16kHz モノラル float32（[-1, 1]）。コールバックは取り込みスレッドから呼ぶ。
"""

import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np

from ..utils.logger import get_logger

logger = get_logger(__name__)

TARGET_RATE = 16000

AudioCallback = Callable[[np.ndarray], None]


class CaptureUnavailable(Exception):
    """この環境ではシステム音声を取り込めない（ユーザー向けの理由を持つ）。"""


# ----------------------------------------------------------------------
# 変換（純ロジック）
# ----------------------------------------------------------------------

class Resampler:
    """任意レート・チャンネルの int16 / float32 → 16kHz モノラル float32（チャンク境界で段差を作らない）。

    整数比（48k→16k など）は区間平均（簡易ローパス）で間引き、それ以外（44.1k など）は線形補間する。
    取り込みスレッドからだけ呼ぶ（ロック不要）。
    """

    def __init__(self, rate: int, channels: int) -> None:
        self.rate = int(rate)
        self.channels = max(1, int(channels))
        self._carry = np.zeros(0, dtype=np.float32)   # 整数比の間引きで余った端数
        self._pos = 0.0                               # 線形補間の次の出力位置（前チャンク末尾が 0）
        self._prev = 0.0

    def mono(self, data) -> np.ndarray:
        """生データ（bytes なら int16 とみなす）をモノラル float32 にする。"""
        if isinstance(data, (bytes, bytearray, memoryview)):
            samples = np.frombuffer(bytes(data), dtype="<i2").astype(np.float32) / 32768.0
        else:
            samples = np.asarray(data, dtype=np.float32)
        if self.channels > 1:
            usable = samples.size - samples.size % self.channels
            samples = samples[:usable].reshape(-1, self.channels).mean(axis=1)
        return samples.astype(np.float32, copy=False)

    def process(self, data) -> np.ndarray:
        samples = self.mono(data)
        if samples.size == 0 or self.rate == TARGET_RATE:
            return samples
        if self.rate % TARGET_RATE == 0:
            factor = self.rate // TARGET_RATE
            buf = np.concatenate((self._carry, samples)) if self._carry.size else samples
            usable = buf.size - buf.size % factor
            self._carry = buf[usable:].copy()
            if usable == 0:
                return np.zeros(0, dtype=np.float32)
            return buf[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)
        # 線形補間（前チャンク末尾のサンプルを左端に持ち越す）
        step = self.rate / float(TARGET_RATE)
        buf = np.concatenate(([np.float32(self._prev)], samples))
        limit = float(buf.size - 1)
        if self._pos >= limit:
            self._pos -= samples.size
            self._prev = float(samples[-1])
            return np.zeros(0, dtype=np.float32)
        count = int(np.ceil((limit - self._pos) / step))
        positions = self._pos + np.arange(count, dtype=np.float64) * step
        positions = positions[positions < limit]
        idx = positions.astype(np.int64)
        frac = (positions - idx).astype(np.float32)
        out = buf[idx] * (1 - frac) + buf[idx + 1] * frac
        next_pos = (positions[-1] + step) if positions.size else self._pos
        self._pos = next_pos - samples.size
        self._prev = float(samples[-1])
        return out.astype(np.float32)


# ----------------------------------------------------------------------
# 最前面アプリの追従（純ロジック）
# ----------------------------------------------------------------------

# 対象にしないプロセス（デスクトップ・タスクバーを触っただけで対象を外さない）
_IGNORED_FOREGROUND = frozenset({"explorer.exe", "shellexperiencehost.exe", "searchhost.exe",
                                 "searchapp.exe", "startmenuexperiencehost.exe", "lockapp.exe"})
# 中身のアプリと別プロセスの枠（UWP アプリ）。どのアプリか分からないので「すべて（自分以外）」に倒す
_FRAME_HOSTS = frozenset({"applicationframehost.exe"})

ALL_EXCEPT_SELF = 0  # 対象 PID の代わりに使う印（自分以外のすべて）


@dataclass(frozen=True)
class Target:
    """取り込み対象（pid=ALL_EXCEPT_SELF なら自分以外のすべて）。"""

    pid: int
    name: str


class FrontmostTargetPolicy:
    """最前面のアプリを追いかけて、張り替えるべき対象を決める（時刻は呼び出し側から渡す）。

    - 自分（voicekey）・デスクトップ・タスクバーが前面に来ても対象を変えない（設定を触るたびに外れない）
    - 同じアプリが STABLE_SECONDS 続いてから張り替える（Alt+Tab で通り過ぎただけのアプリにしない）
    - 前回の張り替えから MIN_INTERVAL 以上あける（作り直しをループさせない）
    """

    STABLE_SECONDS = 1.0
    MIN_INTERVAL = 2.0

    def __init__(self, own_pid: int) -> None:
        self.own_pid = own_pid
        self.current: Optional[Target] = None
        self._candidate: Optional[Target] = None
        self._candidate_since = 0.0
        self._last_switch = -1e9

    def classify(self, pid: Optional[int], exe_name: str) -> Optional[Target]:
        """前面のプロセスを対象の候補にする（対象にしないなら None）。"""
        if not pid or pid == self.own_pid:
            return None
        name = (exe_name or "").lower()
        if name in _IGNORED_FOREGROUND:
            return None
        if name in _FRAME_HOSTS:
            return Target(ALL_EXCEPT_SELF, "すべてのアプリ")
        return Target(int(pid), display_name(exe_name))

    def observe(self, pid: Optional[int], exe_name: str, now: float) -> Optional[Target]:
        """1 回分の前面を取り込み、張り替えるべき新しい対象を返す（変えないなら None）。"""
        candidate = self.classify(pid, exe_name)
        if candidate is None:
            self._candidate = None
            return None
        if self.current is not None and candidate.pid == self.current.pid:
            self._candidate = None
            return None
        if self._candidate is None or self._candidate.pid != candidate.pid:
            self._candidate = candidate
            self._candidate_since = now
            # 最初の対象は待たずに決める（開始直後に何も拾わない時間を作らない）
            if self.current is not None:
                return None
        if self.current is not None and now - self._candidate_since < self.STABLE_SECONDS:
            return None
        if now - self._last_switch < self.MIN_INTERVAL:
            return None
        self.current = candidate
        self._candidate = None
        self._last_switch = now
        return candidate


def display_name(exe_name: str) -> str:
    """実行ファイル名から表示名を作る（chrome.exe → Chrome）。"""
    base = os.path.basename(exe_name or "")
    if base.lower().endswith(".exe"):
        base = base[:-4]
    known = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "firefox": "Firefox",
             "zoom": "Zoom", "teams": "Microsoft Teams", "ms-teams": "Microsoft Teams",
             "vlc": "VLC", "spotify": "Spotify", "slack": "Slack", "discord": "Discord"}
    return known.get(base.lower(), base or "アプリ")


class RebuildLimiter:
    """取り込みの作り直しの上限（連続 5 回まで・指数バックオフ）。成功が 30 秒続けば数え直す。"""

    MAX_ATTEMPTS = 5
    BASE_DELAY = 0.5
    MAX_DELAY = 8.0
    HEALTHY_SECONDS = 30.0

    def __init__(self) -> None:
        self.failures = 0
        self._started_at: Optional[float] = None

    def note_started(self, now: float) -> None:
        self._started_at = now

    def next_delay(self, now: float) -> Optional[float]:
        """落ちた。次に作り直すまでの待ち時間（もう諦めるなら None）。"""
        if self._started_at is not None and now - self._started_at >= self.HEALTHY_SECONDS:
            self.failures = 0
        self._started_at = None
        self.failures += 1
        if self.failures > self.MAX_ATTEMPTS:
            return None
        return min(self.BASE_DELAY * (2 ** (self.failures - 1)), self.MAX_DELAY)


# ----------------------------------------------------------------------
# Windows: 前面プロセス
# ----------------------------------------------------------------------

def foreground_process() -> Tuple[Optional[int], str]:
    """前面ウィンドウのプロセス（PID, 実行ファイル名）。取れなければ (None, "")。"""
    if sys.platform != "win32":
        return None, ""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None, ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None, ""
        return pid.value, process_image_name(pid.value)
    except Exception:
        return None, ""


def process_image_name(pid: int) -> str:
    """PID の実行ファイル名（chrome.exe など。取れなければ空）。"""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


# ----------------------------------------------------------------------
# Windows: Process Loopback（ctypes で Core Audio を直接叩く）
# ----------------------------------------------------------------------

class ProcessLoopbackCapture:
    """1 プロセスの木（含める／除外する）の音を拾う（Windows 10 2004 以降）。

    `ActivateAudioInterfaceAsync` の完了通知は COM のコールバック（別スレッド）で届くので、
    ctypes で最小の COM オブジェクト（IActivateAudioInterfaceCompletionHandler + IAgileObject）を組んで受ける。
    """

    # 試す取り込み形式（自動変換に任せる）。最初の 16kHz モノラルが通れば変換が要らない
    _FORMATS = ((16000, 1), (48000, 2), (44100, 2))

    def __init__(self, pid: int, include_tree: bool, on_audio: AudioCallback) -> None:
        """
        Args:
            pid: 対象のプロセス ID
            include_tree: True ならそのプロセスの木だけを拾う／False ならそれ以外のすべて
            on_audio: 16kHz モノラル float32 を受け取る
        """
        self.pid = int(pid)
        self.include_tree = include_tree
        self.on_audio = on_audio
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self.failed = threading.Event()

    def start(self, timeout: float = 5.0) -> None:
        """取り込みスレッドを起動し、初期化できるまで待つ（失敗は CaptureUnavailable）。"""
        if sys.platform != "win32":
            raise CaptureUnavailable("Windows 以外ではシステム音声を取り込めません")
        self._thread = threading.Thread(target=self._run, daemon=True, name="ProcessLoopback")
        self._thread.start()
        if not self._ready.wait(timeout):
            self._stop.set()
            raise CaptureUnavailable("音声の取り込みの準備が時間内に終わりませんでした")
        if self._error:
            raise CaptureUnavailable(self._error)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)

    def _fail_init(self, message: str) -> None:
        self._error = message
        self._ready.set()

    def _run(self) -> None:
        try:
            self._capture()
        except Exception as e:
            logger.warning(f"Process Loopback の取り込みでエラー: {type(e).__name__}: {e}")
            if not self._ready.is_set():
                self._fail_init(f"音声の取り込みを開始できません（{type(e).__name__}）")
            else:
                self.failed.set()

    def _capture(self) -> None:  # pragma: no cover - Windows 実機でしか動かない
        import ctypes
        from ctypes import wintypes

        ole32 = ctypes.WinDLL("ole32")
        kernel32 = ctypes.WinDLL("kernel32")
        # ハンドルは 64 ビットなので既定の int（32 ビット）で受けない
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        COINIT_MULTITHREADED = 0
        ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        audio_client = None
        capture_client = None
        event = None
        try:
            audio_client = _activate_process_loopback(self.pid, self.include_tree)
            if audio_client is None:
                self._fail_init("この Windows ではアプリ単位の音声取り込み（Process Loopback）を使えません")
                return
            fmt_rate, fmt_channels = self._initialize(audio_client)
            if fmt_rate is None:
                self._fail_init("音声の取り込み形式を設定できませんでした")
                return
            event = kernel32.CreateEventW(None, False, False, None)
            _call(audio_client, 13, ctypes.c_long, wintypes.HANDLE)(event)  # SetEventHandle
            capture_ptr = ctypes.c_void_p()
            hr = _call(audio_client, 14, ctypes.c_long, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(capture_ptr))  # GetService
            if hr < 0 or not capture_ptr.value:
                self._fail_init(f"音声の取り込みを開始できません（GetService 0x{hr & 0xFFFFFFFF:08X}）")
                return
            capture_client = capture_ptr.value
            hr = _call(audio_client, 10, ctypes.c_long)()  # Start
            if hr < 0:
                self._fail_init(f"音声の取り込みを開始できません（Start 0x{hr & 0xFFFFFFFF:08X}）")
                return
            logger.info(f"Process Loopback を開始（{'対象の木だけ' if self.include_tree else '自分以外'}・"
                        f"{fmt_rate}Hz/{fmt_channels}ch）")
            self._ready.set()

            resampler = Resampler(fmt_rate, fmt_channels)
            block_align = 2 * fmt_channels
            get_next = _call(capture_client, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_uint32))
            get_buffer = _call(capture_client, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p),
                               ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
                               ctypes.c_void_p, ctypes.c_void_p)
            release_buffer = _call(capture_client, 4, ctypes.c_long, ctypes.c_uint32)
            AUDCLNT_BUFFERFLAGS_SILENT = 0x2
            while not self._stop.is_set():
                kernel32.WaitForSingleObject(event, 100)
                while not self._stop.is_set():
                    packet = ctypes.c_uint32(0)
                    if get_next(ctypes.byref(packet)) < 0 or packet.value == 0:
                        break
                    data_ptr = ctypes.c_void_p()
                    frames = ctypes.c_uint32(0)
                    flags = ctypes.c_uint32(0)
                    if get_buffer(ctypes.byref(data_ptr), ctypes.byref(frames), ctypes.byref(flags), None, None) < 0:
                        break
                    count = frames.value
                    if count:
                        if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data_ptr.value:
                            raw = bytes(count * block_align)
                        else:
                            raw = ctypes.string_at(data_ptr.value, count * block_align)
                    else:
                        raw = b""
                    release_buffer(count)
                    if raw:
                        out = resampler.process(raw)
                        if out.size:
                            try:
                                self.on_audio(out)
                            except Exception:
                                pass
        finally:
            if audio_client is not None:
                try:
                    _call(audio_client, 11, ctypes.c_long)()  # Stop
                except Exception:
                    pass
            for ptr in (capture_client, audio_client):
                if ptr:
                    try:
                        _call(ptr, 2, ctypes.c_ulong)()  # Release
                    except Exception:
                        pass
            if event:
                kernel32.CloseHandle(event)
            ole32.CoUninitialize()
            if not self._ready.is_set():
                self._fail_init("音声の取り込みを開始できませんでした")

    def _initialize(self, audio_client) -> Tuple[Optional[int], int]:  # pragma: no cover - Windows 実機のみ
        import ctypes

        AUDCLNT_SHAREMODE_SHARED = 0
        AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
        AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
        AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
        AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
        flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                 | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
        initialize = _call(audio_client, 3, ctypes.c_long, ctypes.c_int, ctypes.c_uint32, ctypes.c_longlong,
                           ctypes.c_longlong, ctypes.POINTER(_WAVEFORMATEX), ctypes.c_void_p)
        for rate, channels in self._FORMATS:
            fmt = _WAVEFORMATEX()
            fmt.wFormatTag = 1  # WAVE_FORMAT_PCM
            fmt.nChannels = channels
            fmt.nSamplesPerSec = rate
            fmt.wBitsPerSample = 16
            fmt.nBlockAlign = channels * 2
            fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign
            fmt.cbSize = 0
            # バッファは 200ms（100ns 単位）。共有モードなので周期は 0
            hr = initialize(AUDCLNT_SHAREMODE_SHARED, flags, 2_000_000, 0, ctypes.byref(fmt), None)
            if hr >= 0:
                return rate, channels
            logger.info(f"Process Loopback の形式 {rate}Hz/{channels}ch は使えません（0x{hr & 0xFFFFFFFF:08X}）")
        return None, 0


def _call(obj_ptr, index: int, restype, *argtypes):  # pragma: no cover - Windows 実機のみ
    """COM オブジェクトの vtable の index 番目のメソッドを呼べる関数にする。"""
    import ctypes

    vtbl = ctypes.cast(ctypes.c_void_p(obj_ptr), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = prototype(vtbl[index])
    return lambda *args: fn(obj_ptr, *args)


if sys.platform == "win32":  # pragma: no cover - Windows 実機のみ
    import ctypes as _ct

    class _GUID(_ct.Structure):
        _fields_ = [("Data1", _ct.c_uint32), ("Data2", _ct.c_uint16), ("Data3", _ct.c_uint16),
                    ("Data4", _ct.c_ubyte * 8)]

    def _guid(text: str) -> "_GUID":
        import uuid

        u = uuid.UUID(text)
        g = _GUID()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        for i, b in enumerate(u.bytes[8:]):
            g.Data4[i] = b
        return g

    def _guid_equal(a_ptr, b: "_GUID") -> bool:
        a = _ct.cast(a_ptr, _ct.POINTER(_GUID)).contents
        return bytes(a) == bytes(b)

    class _WAVEFORMATEX(_ct.Structure):
        _pack_ = 1
        _fields_ = [("wFormatTag", _ct.c_uint16), ("nChannels", _ct.c_uint16),
                    ("nSamplesPerSec", _ct.c_uint32), ("nAvgBytesPerSec", _ct.c_uint32),
                    ("nBlockAlign", _ct.c_uint16), ("wBitsPerSample", _ct.c_uint16), ("cbSize", _ct.c_uint16)]

    class _BLOB(_ct.Structure):
        _fields_ = [("cbSize", _ct.c_ulong), ("pBlobData", _ct.c_void_p)]

    class _PROPVARIANT(_ct.Structure):
        _fields_ = [("vt", _ct.c_ushort), ("r1", _ct.c_ushort), ("r2", _ct.c_ushort), ("r3", _ct.c_ushort),
                    ("blob", _BLOB)]

    class _LOOPBACK_PARAMS(_ct.Structure):
        _fields_ = [("TargetProcessId", _ct.c_uint32), ("ProcessLoopbackMode", _ct.c_int)]

    class _ACTIVATION_PARAMS(_ct.Structure):
        _fields_ = [("ActivationType", _ct.c_int), ("ProcessLoopbackParams", _LOOPBACK_PARAMS)]

    # 完了通知のコールバック一式（解放すると、遅れて来た通知で落ちる）
    _KEEP_ALIVE: list = []

    IID_IUnknown = _guid("00000000-0000-0000-C000-000000000046")
    IID_IAgileObject = _guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")
    IID_IAudioClient = _guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
    IID_IAudioCaptureClient = _guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317")
    IID_ICompletionHandler = _guid("41D949AB-9862-444A-80F6-C261334DA5EB")

    _QI = _ct.WINFUNCTYPE(_ct.c_long, _ct.c_void_p, _ct.c_void_p, _ct.POINTER(_ct.c_void_p))
    _ADDREF = _ct.WINFUNCTYPE(_ct.c_ulong, _ct.c_void_p)
    _RELEASE = _ct.WINFUNCTYPE(_ct.c_ulong, _ct.c_void_p)
    _COMPLETED = _ct.WINFUNCTYPE(_ct.c_long, _ct.c_void_p, _ct.c_void_p)

    class _HandlerVtbl(_ct.Structure):
        _fields_ = [("QueryInterface", _QI), ("AddRef", _ADDREF), ("Release", _RELEASE),
                    ("ActivateCompleted", _COMPLETED)]

    class _Handler(_ct.Structure):
        _fields_ = [("lpVtbl", _ct.POINTER(_HandlerVtbl))]

    def _activate_process_loopback(pid: int, include_tree: bool):
        """Process Loopback の IAudioClient を得る（使えなければ None）。"""
        done = threading.Event()
        result = {"client": None, "hr": 0}

        def query_interface(this, riid, ppv):
            if any(_guid_equal(riid, iid) for iid in (IID_IUnknown, IID_IAgileObject, IID_ICompletionHandler)):
                ppv[0] = this
                return 0
            ppv[0] = None
            return 0x80004002 - 0x100000000  # E_NOINTERFACE

        def add_ref(this):
            return 1

        def release(this):
            return 1

        def completed(this, operation):
            try:
                hr_activate = _ct.c_long(0)
                unknown = _ct.c_void_p()
                hr = _call(operation, 3, _ct.c_long, _ct.POINTER(_ct.c_long), _ct.POINTER(_ct.c_void_p))(
                    _ct.byref(hr_activate), _ct.byref(unknown))
                result["hr"] = hr if hr < 0 else hr_activate.value
                if hr >= 0 and hr_activate.value >= 0 and unknown.value:
                    result["client"] = unknown.value
            except Exception as e:
                logger.warning(f"Process Loopback の完了通知でエラー: {e}")
            finally:
                done.set()
            return 0

        vtbl = _HandlerVtbl(_QI(query_interface), _ADDREF(add_ref), _RELEASE(release), _COMPLETED(completed))
        handler = _Handler(_ct.pointer(vtbl))

        params = _ACTIVATION_PARAMS()
        params.ActivationType = 1  # AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.ProcessLoopbackParams.TargetProcessId = int(pid)
        # 0 = INCLUDE_TARGET_PROCESS_TREE / 1 = EXCLUDE_TARGET_PROCESS_TREE
        params.ProcessLoopbackParams.ProcessLoopbackMode = 0 if include_tree else 1
        prop = _PROPVARIANT()
        prop.vt = 65  # VT_BLOB
        prop.blob.cbSize = _ct.sizeof(params)
        prop.blob.pBlobData = _ct.cast(_ct.pointer(params), _ct.c_void_p)

        try:
            activate = _ct.WinDLL("Mmdevapi").ActivateAudioInterfaceAsync
        except Exception:
            return None
        activate.restype = _ct.c_long
        activate.argtypes = [_ct.c_wchar_p, _ct.POINTER(_GUID), _ct.POINTER(_PROPVARIANT), _ct.c_void_p,
                             _ct.POINTER(_ct.c_void_p)]
        operation = _ct.c_void_p()
        hr = activate("VAD\\Process_Loopback", _ct.byref(IID_IAudioClient), _ct.byref(prop),
                      _ct.cast(_ct.pointer(handler), _ct.c_void_p), _ct.byref(operation))
        if hr < 0:
            logger.info(f"ActivateAudioInterfaceAsync が失敗（0x{hr & 0xFFFFFFFF:08X}）")
            return None
        # 完了通知を待つ。時間切れの後に通知が来ても落ちないよう、コールバックの参照はずっと持っておく
        # （作り直しには上限があるので、溜まるのは高々数十個）
        _KEEP_ALIVE.append((vtbl, handler, params, prop))
        finished = done.wait(5.0)
        if operation.value:
            try:
                _call(operation.value, 2, _ct.c_ulong)()
            except Exception:
                pass
        if not finished:
            logger.info("Process Loopback の準備が時間内に終わりませんでした")
            return None
        if result["client"] is None:
            logger.info(f"Process Loopback を使えません（0x{result['hr'] & 0xFFFFFFFF:08X}）")
        return result["client"]
else:
    def _activate_process_loopback(pid: int, include_tree: bool):
        return None


# ----------------------------------------------------------------------
# 既定の出力デバイス全体のループバック（PyAudioWPatch）
# ----------------------------------------------------------------------

class SystemLoopbackCapture:
    """既定の出力デバイス全体の音を拾う（Process Loopback が使えないときの代わり）。"""

    def __init__(self, on_audio: AudioCallback) -> None:
        self.on_audio = on_audio
        self._pa = None
        self._stream = None
        self._resampler: Optional[Resampler] = None
        self.failed = threading.Event()

    def start(self, timeout: float = 5.0) -> None:
        try:
            import pyaudiowpatch as pyaudio  # Windows 専用（requirements は win32 のときだけ）
        except Exception:
            raise CaptureUnavailable("システム音声の取り込み部品（PyAudioWPatch）がありません")
        try:
            self._pa = pyaudio.PyAudio()
            info = self._pa.get_default_wasapi_loopback()
            rate = int(info.get("defaultSampleRate") or 48000)
            channels = max(1, int(info.get("maxInputChannels") or 2))
            self._resampler = Resampler(rate, channels)

            def callback(in_data, frame_count, time_info, status):
                try:
                    out = self._resampler.process(in_data)
                    if out.size:
                        self.on_audio(out)
                except Exception:
                    pass
                return (None, pyaudio.paContinue)

            self._stream = self._pa.open(
                format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
                input_device_index=int(info["index"]), frames_per_buffer=int(rate // 50),
                stream_callback=callback,
            )
            self._stream.start_stream()
            logger.info(f"システム全体のループバックを開始（{info.get('name', '?')}・{rate}Hz/{channels}ch）")
        except CaptureUnavailable:
            raise
        except Exception as e:
            self.stop()
            raise CaptureUnavailable(f"システム音声の取り込みを開始できません（{type(e).__name__}）")

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        pa, self._pa = self._pa, None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass


# ----------------------------------------------------------------------
# 取り込みの統括（対象の追従・作り直しの上限・代替への切り替え）
# ----------------------------------------------------------------------

class CaptureController:
    """対象を決めて取り込みを張り、最前面に追従し、落ちたら上限つきで作り直す。

    Attributes:
        on_target_changed: 対象のアプリ名が変わった（"すべてのアプリ" / アプリ名）
        on_failed: 作り直しを諦めた（ユーザー向けの理由）
        on_notice: 代わりの取り方に切り替えたなど、知らせたいこと
    """

    POLL_SECONDS = 0.5

    def __init__(
        self,
        on_audio: AudioCallback,
        frontmost: bool = True,
        fixed_pid: Optional[int] = None,
        own_pid: Optional[int] = None,
        process_factory: Optional[Callable[[int, bool, AudioCallback], object]] = None,
        system_factory: Optional[Callable[[AudioCallback], object]] = None,
        foreground: Callable[[], Tuple[Optional[int], str]] = foreground_process,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            on_audio: 16kHz モノラル float32 を受け取る（取り込みスレッドから）
            frontmost: 最前面のアプリだけを拾うか（False ならすべて＝自分以外）
            fixed_pid: このプロセスの木だけを拾う（Meet ボットの Chrome）。代わりの取り方へは落とさない
            own_pid: 自分の PID（除外・対象外にする）
            process_factory / system_factory / foreground / clock: テストで差し替える
        """
        self.on_audio = on_audio
        self.frontmost = frontmost
        self.fixed_pid = fixed_pid
        self.own_pid = own_pid if own_pid is not None else os.getpid()
        self._process_factory = process_factory or (lambda pid, include, cb: ProcessLoopbackCapture(pid, include, cb))
        self._system_factory = system_factory or (lambda cb: SystemLoopbackCapture(cb))
        self._foreground = foreground
        self._clock = clock
        self._policy = FrontmostTargetPolicy(self.own_pid)
        self._limiter = RebuildLimiter()
        self._lock = threading.Lock()
        self._capture = None
        self._using_system = False
        self._stop = threading.Event()
        self._watcher: Optional[threading.Thread] = None
        self.target_name: Optional[str] = None
        self.on_target_changed: Optional[Callable[[str], None]] = None
        self.on_failed: Optional[Callable[[str], None]] = None
        self.on_notice: Optional[Callable[[str], None]] = None
        self.rebuilds = 0

    # --- 外から使う口 ---

    def start(self) -> None:
        """最初の取り込みを張る（どの取り方も使えなければ CaptureUnavailable）。"""
        if self.fixed_pid is not None:
            self._open_process(int(self.fixed_pid), include=True, name="ボット用 Chrome")
        else:
            target = None
            if self.frontmost:
                pid, exe = self._safe_foreground()
                target = self._policy.observe(pid, exe, self._clock())
            if target is None:
                target = Target(ALL_EXCEPT_SELF, "すべてのアプリ")
                if self.frontmost:
                    self._policy.current = target
            self._open_target(target)
        self._limiter.note_started(self._clock())
        self._watcher = threading.Thread(target=self._watch, daemon=True, name="CaptureWatch")
        self._watcher.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.stop()
            except Exception:
                pass
        watcher = self._watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(2.0)

    @property
    def uses_system_loopback(self) -> bool:
        return self._using_system

    # --- 内部 ---

    def _safe_foreground(self) -> Tuple[Optional[int], str]:
        try:
            return self._foreground()
        except Exception:
            return None, ""

    def _deliver(self, samples: np.ndarray) -> None:
        if self._stop.is_set():
            return
        try:
            self.on_audio(samples)
        except Exception:
            pass

    def _swap(self, capture) -> None:
        with self._lock:
            old, self._capture = self._capture, capture
        if old is not None and old is not capture:
            try:
                old.stop()
            except Exception:
                pass

    def _open_process(self, pid: int, include: bool, name: str) -> None:
        capture = self._process_factory(pid, include, self._deliver)
        capture.start()
        self._swap(capture)
        self._using_system = False
        self._set_target_name(name)

    def _open_system(self) -> None:
        capture = self._system_factory(self._deliver)
        capture.start()
        self._swap(capture)
        self._using_system = True
        self._set_target_name("すべてのアプリ（システム全体）")

    def _open_target(self, target: Target) -> None:
        """対象を張る。アプリ単位が使えなければ「自分以外」→「システム全体」の順に代わりを使う。"""
        try:
            if target.pid == ALL_EXCEPT_SELF:
                self._open_process(self.own_pid, include=False, name=target.name)
            else:
                self._open_process(target.pid, include=True, name=target.name)
            return
        except CaptureUnavailable as e:
            logger.info(f"アプリ単位の取り込みを使えません: {e}")
            first_error = e
        except Exception as e:
            first_error = CaptureUnavailable(f"音声の取り込みを開始できません（{type(e).__name__}）")
        if target.pid != ALL_EXCEPT_SELF:
            try:
                self._open_process(self.own_pid, include=False, name="すべてのアプリ")
                return
            except Exception:
                pass
        try:
            self._open_system()
        except CaptureUnavailable:
            raise first_error
        self.frontmost = False  # システム全体では最前面に追従できない
        self._notify("この Windows ではアプリ単位で音を拾えないため、パソコン全体の音を拾います")

    def _set_target_name(self, name: str) -> None:
        if name == self.target_name:
            return
        self.target_name = name
        callback = self.on_target_changed
        if callback is not None:
            try:
                callback(name)
            except Exception:
                pass

    def _notify(self, text: str) -> None:
        callback = self.on_notice
        if callback is not None:
            try:
                callback(text)
            except Exception:
                pass

    def _watch(self) -> None:
        """最前面の追従と、落ちた取り込みの作り直し（どちらも上限つき）。"""
        retry_at: Optional[float] = None
        while not self._stop.wait(self.POLL_SECONDS):
            now = self._clock()
            with self._lock:
                capture = self._capture
            failed = capture is not None and getattr(capture, "failed", None) is not None and capture.failed.is_set()
            if failed and retry_at is None:
                delay = self._limiter.next_delay(now)
                if delay is None:
                    self._report_failed("音声の取り込みが何度も止まったため、字幕を止めました")
                    return
                logger.warning(f"音声の取り込みが止まりました。{delay:.1f} 秒後に作り直します")
                retry_at = now + delay
            if retry_at is not None and now >= retry_at:
                retry_at = None
                try:
                    self.rebuilds += 1
                    if self.fixed_pid is not None:
                        self._open_process(int(self.fixed_pid), include=True, name="ボット用 Chrome")
                    elif self._using_system:
                        self._open_system()
                    else:
                        self._open_target(self._policy.current or Target(ALL_EXCEPT_SELF, "すべてのアプリ"))
                    self._limiter.note_started(now)
                except Exception as e:
                    logger.warning(f"音声の取り込みを作り直せませんでした: {e}")
                    with self._lock:
                        self._capture = _FailedCapture()
                continue
            if self.fixed_pid is not None or not self.frontmost or self._using_system:
                continue
            pid, exe = self._safe_foreground()
            target = self._policy.observe(pid, exe, now)
            if target is None:
                continue
            try:
                self.rebuilds += 1
                self._open_target(target)
                logger.info(f"字幕の対象を切り替えました: {target.name}")
            except Exception as e:
                logger.warning(f"字幕の対象を切り替えられませんでした: {e}")

    def _report_failed(self, reason: str) -> None:
        callback = self.on_failed
        if callback is not None:
            try:
                callback(reason)
            except Exception:
                pass


class _FailedCapture:
    """作り直しに失敗した印（次の見回りでまた上限つきで作り直す）。"""

    def __init__(self) -> None:
        self.failed = threading.Event()
        self.failed.set()

    def stop(self) -> None:
        pass
