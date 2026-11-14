"""Window enumeration and Win32 helpers.

All coordinates in this package are physical screen pixels. The process is made
per-monitor DPI aware on import so that window rects, screenshots and mouse
input agree with each other on scaled and mixed-DPI setups.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import asdict, dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

# Must happen before any window or DC call.
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
WDA_NONE = 0
WDA_MONITOR = 1
WDA_EXCLUDEFROMCAPTURE = 0x11
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20
GA_ROOT = 2

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    process: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom (physical px)
    minimized: bool
    cloaked: bool  # on another virtual desktop, or a suspended UWP frame
    foreground: bool
    capture_blocked: bool  # window opted out of capture (WDA_*)
    elevated: bool | None  # None = could not tell

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["size"] = [self.width, self.height]
        return d


def _text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Visible frame bounds, without the invisible resize border DWM adds."""
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)
    ) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def _cloaked(hwnd: int) -> bool:
    v = wintypes.DWORD()
    if dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)
    ) != 0:
        return False
    return bool(v.value)


def display_affinity(hwnd: int) -> int:
    v = wintypes.DWORD()
    if not user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(v)):
        return WDA_NONE
    return v.value


def _pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_proc_cache: dict[int, tuple[str, bool | None]] = {}


def _process_info(pid: int) -> tuple[str, bool | None]:
    """Return (exe name, elevated). Elevation is None when it cannot be read."""
    if pid in _proc_cache:
        return _proc_cache[pid]
    name, elevated = "", None
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
            tok = wintypes.HANDLE()
            if advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
                try:
                    val = wintypes.DWORD()
                    ret = wintypes.DWORD()
                    if advapi32.GetTokenInformation(
                        tok, TokenElevation, ctypes.byref(val), 4, ctypes.byref(ret)
                    ):
                        elevated = bool(val.value)
                finally:
                    kernel32.CloseHandle(tok)
            else:
                # Non-elevated callers are denied the token of elevated processes.
                elevated = True
        finally:
            kernel32.CloseHandle(h)
    _proc_cache[pid] = (name, elevated)
    return name, elevated


def self_elevated() -> bool:
    return bool(_process_info(os.getpid())[1])


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def get_window(hwnd: int) -> WindowInfo:
    if not is_window(hwnd):
        raise ValueError(f"hwnd {hwnd} is not a window (closed?)")
    pid = _pid(hwnd)
    proc, elevated = _process_info(pid)
    return WindowInfo(
        hwnd=hwnd,
        title=_text(hwnd),
        class_name=_class(hwnd),
        pid=pid,
        process=proc,
        rect=window_rect(hwnd),
        minimized=bool(user32.IsIconic(hwnd)),
        cloaked=_cloaked(hwnd),
        foreground=user32.GetForegroundWindow() == hwnd,
        capture_blocked=display_affinity(hwnd) != WDA_NONE,
        elevated=elevated,
    )


_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def list_windows(include_cloaked: bool = False) -> list[WindowInfo]:
    """Top-level windows a user would consider 'an app window', in Z order."""
    out: list[WindowInfo] = []
    me = os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd) or not _text(hwnd):
                return True
            if _class(hwnd) in _SKIP_CLASSES:
                return True
            info = get_window(hwnd)
            if info.pid == me or (info.cloaked and not include_cloaked):
                return True
            if info.width <= 1 and info.height <= 1 and not info.minimized:
                return True
            out.append(info)
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_window(query: str | int) -> WindowInfo:
    """Resolve a window by hwnd, or by case-insensitive substring of title or exe name."""
    if isinstance(query, int) or str(query).isdigit():
        return get_window(int(query))
    q = str(query).lower()
    wins = list_windows(include_cloaked=True)
    for w in wins:  # exact title first
        if w.title.lower() == q:
            return w
    for w in wins:
        if q in w.title.lower() or q in w.process.lower():
            return w
    raise ValueError(f"no window matches {query!r}")


def root_at(x: int, y: int) -> int:
    h = user32.WindowFromPoint(wintypes.POINT(x, y))
    return user32.GetAncestor(h, GA_ROOT) if h else 0


def restore_no_activate(hwnd: int) -> None:
    SW_SHOWNOACTIVATE = 4
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def focus(hwnd: int) -> bool:
    """Bring a window to the foreground. Windows only allows this under some
    conditions; the ALT tap is the documented-in-practice way to satisfy them."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if user32.GetForegroundWindow() == hwnd:
        return True
    VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x2
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    user32.SetForegroundWindow(hwnd)
    return user32.GetForegroundWindow() == hwnd
