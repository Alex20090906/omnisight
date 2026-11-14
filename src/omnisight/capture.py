"""Window capture with several backends and automatic fallback.

Order tried for a window (unless a backend that worked before for the same
process is cached):

  printwindow  PrintWindow(PW_RENDERFULLCONTENT). Works for most GDI and many
               DirectComposition windows (Chromium, Electron), including covered ones.
  wgc          Windows.Graphics.Capture. Sees GPU-rendered content (games,
               UWP) and windows covered by other windows. Cannot see minimized
               windows.
  bitblt       Classic window DC copy. Cheap; black for GPU content.
  screen       Crop of the desktop at the window rect. Only valid when nothing
               covers the window; checked before use.

A frame that is (almost) one flat color is treated as a failed capture and the
next backend is tried.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import numpy as np

from . import winutil as wu

gdi32 = ctypes.WinDLL("gdi32")
user32 = wu.user32

PW_RENDERFULLCONTENT = 0x2
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0

user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class CaptureError(Exception):
    pass


@dataclass
class Capture:
    image: np.ndarray  # H x W x 3, RGB, uint8
    origin: tuple[int, int]  # screen position of image pixel (0, 0)
    backend: str
    elapsed_ms: float
    attempts: list[str] = field(default_factory=list)  # "backend: why it was rejected"


def is_blank(img: np.ndarray) -> bool:
    if img.size == 0:
        return True
    small = img[:: max(1, img.shape[0] // 64), :: max(1, img.shape[1] // 64)]
    return float(small.reshape(-1, small.shape[-1]).std(axis=0).max()) < 1.5


SUSPECT_BLACK = 0.9


def black_ratio(img: np.ndarray) -> float:
    small = img[:: max(1, img.shape[0] // 128), :: max(1, img.shape[1] // 128)]
    return float((small.max(axis=2) < 8).mean())


def _bgra_to_rgb(buf: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(buf[:, :, 2::-1])


# --------------------------------------------------------------------- GDI


def _gdi_grab(hwnd: int, use_printwindow: bool) -> tuple[np.ndarray, tuple[int, int]]:
    wr = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    w, h = wr.right - wr.left, wr.bottom - wr.top
    if w <= 0 or h <= 0:
        raise CaptureError("window has no size")
    wdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(wdc)
    bmp = gdi32.CreateCompatibleBitmap(wdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        if use_printwindow:
            ok = user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
        else:
            ok = gdi32.BitBlt(mdc, 0, 0, w, h, wdc, 0, 0, SRCCOPY)
        if not ok:
            raise CaptureError("call failed")
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = np.empty((h, w, 4), dtype=np.uint8)
        if not gdi32.GetDIBits(mdc, bmp, 0, h, buf.ctypes.data, ctypes.byref(bi), DIB_RGB_COLORS):
            raise CaptureError("GetDIBits failed")
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, wdc)
    # Trim the invisible DWM resize border so the image matches the visible frame.
    l, t, r, b = wu.window_rect(hwnd)
    ox, oy = max(0, l - wr.left), max(0, t - wr.top)
    buf = buf[oy: oy + (b - t), ox: ox + (r - l)]
    return _bgra_to_rgb(buf), (l, t)


# --------------------------------------------------------------------- WGC


class _WgcSession:
    """A live Windows.Graphics.Capture session for one window.

    WGC only delivers a frame when the window content changes, so the newest
    frame is kept and reused; repeated snapshots of a static window cost
    nothing. Sessions close after IDLE_SECONDS without use.
    """

    def __init__(self, hwnd: int):
        from windows_capture import WindowsCapture

        self.hwnd = hwnd
        self.frame: np.ndarray | None = None
        self.frame_time = 0.0
        self.closed = False
        self.last_used = time.monotonic()
        self.ready = threading.Event()
        self._lock = threading.Lock()
        # minimum_update_interval / draw_border are Windows 11 only; leave them unset.
        cap = WindowsCapture(cursor_capture=False, window_hwnd=hwnd)

        @cap.event
        def on_frame_arrived(frame, control):
            if self.closed:
                control.stop()
                return
            img = frame.frame_buffer.copy()  # native buffer is only valid inside the callback
            with self._lock:
                self.frame, self.frame_time = img, time.monotonic()
            self.ready.set()

        @cap.event
        def on_closed():
            self.closed = True
            self.ready.set()

        self.control = cap.start_free_threaded()

    def latest(self, timeout: float) -> tuple[np.ndarray, float]:
        self.last_used = time.monotonic()
        if not self.ready.wait(timeout):
            raise CaptureError("no frame within timeout")
        with self._lock:
            if self.frame is None:
                raise CaptureError("session closed before first frame")
            return self.frame, self.frame_time

    def stop(self) -> None:
        self.closed = True
        try:
            self.control.stop()
        except Exception:
            pass


class _WgcPool:
    IDLE_SECONDS = 20.0

    def __init__(self):
        self.sessions: dict[int, _WgcSession] = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    def get(self, hwnd: int) -> _WgcSession:
        with self.lock:
            s = self.sessions.get(hwnd)
            if s is None or s.closed:
                s = self.sessions[hwnd] = _WgcSession(hwnd)
            return s

    def drop(self, hwnd: int) -> None:
        with self.lock:
            s = self.sessions.pop(hwnd, None)
        if s:
            s.stop()

    def _reaper(self):
        while True:
            time.sleep(5)
            now = time.monotonic()
            with self.lock:
                dead = [h for h, s in self.sessions.items()
                        if s.closed or now - s.last_used > self.IDLE_SECONDS or not wu.is_window(h)]
                stale = [self.sessions.pop(h) for h in dead]
            for s in stale:
                s.stop()


_wgc_pool = _WgcPool()


def _wgc_grab(hwnd: int, timeout: float = 1.5) -> tuple[np.ndarray, tuple[int, int]]:
    session = _wgc_pool.get(hwnd)
    buf, _ = session.latest(timeout)
    l, t, r, b = wu.window_rect(hwnd)
    img = _bgra_to_rgb(buf)
    # Frame may include the invisible border; centre-crop to the visible frame.
    w, h = r - l, b - t
    if img.shape[1] > w or img.shape[0] > h:
        ox = max(0, (img.shape[1] - w) // 2)
        img = img[: h, ox: ox + w]
    return img, (l, t)


# ------------------------------------------------------------------- screen


def grab_screen_region(left: int, top: int, right: int, bottom: int) -> np.ndarray:
    import mss

    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
        return _bgra_to_rgb(np.asarray(shot))


def virtual_screen() -> tuple[int, int, int, int]:
    import mss

    with mss.mss() as sct:
        m = sct.monitors[0]
        return (m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])


def monitors() -> list[dict]:
    import mss

    with mss.mss() as sct:
        return [dict(index=i, left=m["left"], top=m["top"], width=m["width"], height=m["height"])
                for i, m in enumerate(sct.monitors[1:], start=1)]


def _unoccluded(hwnd: int) -> bool:
    l, t, r, b = wu.window_rect(hwnd)
    pts = [((l + r) // 2, (t + b) // 2)]
    for fx in (0.1, 0.5, 0.9):
        for fy in (0.1, 0.5, 0.9):
            pts.append((int(l + (r - l) * fx), int(t + (b - t) * fy)))
    return all(wu.root_at(x, y) == hwnd for x, y in pts)


def _screen_grab(hwnd: int) -> tuple[np.ndarray, tuple[int, int]]:
    if not _unoccluded(hwnd):
        raise CaptureError("window is covered by another window")
    l, t, r, b = wu.window_rect(hwnd)
    return grab_screen_region(l, t, r, b), (l, t)


# ------------------------------------------------------------------- driver

BACKENDS = {
    "wgc": _wgc_grab,
    "printwindow": lambda h: _gdi_grab(h, True),
    "bitblt": lambda h: _gdi_grab(h, False),
    "screen": _screen_grab,
}
# PrintWindow first: on Windows 10 a WGC session costs 150-600 ms to start and
# draws a yellow border around the window while it lives.
DEFAULT_ORDER = ["printwindow", "wgc", "bitblt", "screen"]

# exe name -> backend that last produced a usable frame
_preferred: dict[str, str] = {}


def capture_window(hwnd: int, backend: str = "auto", restore_minimized: bool = False) -> Capture:
    info = wu.get_window(hwnd)
    if info.capture_blocked:
        raise CaptureError(
            "This window has opted out of screen capture (SetWindowDisplayAffinity). "
            "It is a deliberate protection, e.g. a password manager or DRM video; "
            "use the element tree instead, or ask the user to read it.")
    if info.minimized:
        if not restore_minimized:
            raise CaptureError("Window is minimized. Retry with restore_minimized=true to "
                               "restore it without stealing focus.")
        wu.restore_no_activate(hwnd)
        time.sleep(0.25)
    if info.cloaked:
        raise CaptureError("Window is cloaked (on another virtual desktop or suspended).")

    if backend != "auto":
        order = [backend]
    else:
        order = list(DEFAULT_ORDER)
        pref = _preferred.get(info.process)
        if pref in order:
            order.remove(pref)
            order.insert(0, pref)

    attempts: list[str] = []
    t0 = time.perf_counter()
    suspects: list[tuple[float, Capture]] = []
    for name in order:
        try:
            img, origin = BACKENDS[name](hwnd)
        except Exception as e:  # noqa: BLE001 - every backend failure is just "try the next one"
            attempts.append(f"{name}: {e}")
            if name == "wgc":
                _wgc_pool.drop(hwnd)
            continue
        cap = Capture(img, origin, name, (time.perf_counter() - t0) * 1000, attempts)
        black = black_ratio(img)
        if is_blank(img) or black > SUSPECT_BLACK:
            # GPU content read through GDI comes back black, often with only the
            # title bar drawn. Keep it as a fallback; maybe the window is just dark.
            attempts.append(f"{name}: {'blank' if is_blank(img) else f'{black:.0%} black'} frame")
            suspects.append((1.0 if is_blank(img) else black, cap))
            continue
        _preferred[info.process] = name
        return cap
    if suspects:
        return min(suspects, key=lambda s: s[0])[1]
    raise CaptureError("all capture backends failed: " + "; ".join(attempts))


def capture_screen(region: tuple[int, int, int, int] | None = None) -> Capture:
    t0 = time.perf_counter()
    region = region or virtual_screen()
    img = grab_screen_region(*region)
    return Capture(img, (region[0], region[1]), "screen", (time.perf_counter() - t0) * 1000)
