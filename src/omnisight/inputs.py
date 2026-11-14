"""Mouse and keyboard input.

Two delivery paths:

  foreground  SendInput. Works with everything that reads real input (games,
              Chromium, Qt), but the target must be the foreground window and the
              mouse really moves. Keys can be sent as scan codes for games that
              read DirectInput / Raw Input.
  background  PostMessage of WM_* messages to the window under the point. Does
              not move the mouse or steal focus. Classic Win32 / WinForms / many
              Qt apps accept it; Chromium and most games ignore it.

Windows blocks both paths into an elevated (admin) window when this process is
not elevated (UIPI). That is reported, not worked around here.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from . import winutil as wu

user32 = wu.user32

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x4, 0x8
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x8, 0x10
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x20, 0x40
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x800, 0x1000
WHEEL_DELTA = 120

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x200, 0x201, 0x202, 0x203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP = 0x204, 0x205, 0x207, 0x208
WM_MOUSEWHEEL = 0x20A
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x100, 0x101, 0x102
MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x1, 0x2, 0x10
CWP_SKIPINVISIBLE, CWP_SKIPTRANSPARENT = 0x1, 0x4
MAPVK_VK_TO_VSC = 0


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                       wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
SMTO_ABORTIFHUNG = 0x2
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ChildWindowFromPointEx.argtypes = [wintypes.HWND, wintypes.POINT, wintypes.UINT]
user32.ChildWindowFromPointEx.restype = wintypes.HWND
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short


class InputBlocked(Exception):
    pass


def check_uipi(hwnd: int) -> None:
    info = wu.get_window(hwnd)
    if info.elevated and not wu.self_elevated():
        raise InputBlocked(
            f"{info.process} runs as administrator and this server does not. Windows (UIPI) "
            "drops input sent to it. Restart the MCP server from an elevated terminal to "
            "control this window.")


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) != len(inputs):
        raise InputBlocked(f"SendInput was blocked (error {ctypes.get_last_error()}); "
                           "the secure desktop (UAC, lock screen) may be active.")


def _mouse(flags: int, data: int = 0) -> INPUT:
    return INPUT(INPUT_MOUSE, _U(mi=MOUSEINPUT(0, 0, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)))


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON),
}


def cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def ensure_foreground(hwnd: int) -> None:
    if not wu.focus(hwnd):
        raise InputBlocked("Windows refused to bring the window to the foreground. "
                           "Use background=true, or ask the user to click the window once.")
    time.sleep(0.05)


# ---------------------------------------------------------------- foreground


def click(x: int, y: int, button: str = "left", count: int = 1) -> None:
    down, up = _BUTTONS[button][:2]
    user32.SetCursorPos(x, y)
    time.sleep(0.01)
    for _ in range(count):
        _send(_mouse(down), _mouse(up))
        time.sleep(0.03)


def move(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)


def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 20) -> None:
    down, up = _BUTTONS[button][:2]
    user32.SetCursorPos(x1, y1)
    _send(_mouse(down))
    time.sleep(0.05)
    for i in range(1, steps + 1):
        user32.SetCursorPos(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps)
        time.sleep(0.01)
    time.sleep(0.05)
    _send(_mouse(up))


def scroll(x: int, y: int, clicks: int, horizontal: bool = False) -> None:
    user32.SetCursorPos(x, y)
    _send(_mouse(MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL, clicks * WHEEL_DELTA))


def type_text(text: str) -> None:
    """Unicode injection: layout independent, works for CJK and emoji."""
    events = []
    for ch in text:
        if ch == "\n":
            events += [_key(0x0D), _key(0x0D, flags=KEYEVENTF_KEYUP)]
            continue
        for unit in ch.encode("utf-16-le").hex(" ", 2).split():
            code = int.from_bytes(bytes.fromhex(unit), "little")
            events += [_key(0, code, KEYEVENTF_UNICODE), _key(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    for i in range(0, len(events), 64):
        _send(*events[i: i + 64])
        time.sleep(0.005)


VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "printscreen": 0x2C, "menu": 0x5D, "apps": 0x5D,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
_EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B, 0x5D}


def _vk(name: str) -> int:
    n = name.lower()
    if n in VK:
        return VK[n]
    if len(name) == 1:
        r = user32.VkKeyScanW(name)
        if r != -1:
            return r & 0xFF
    raise ValueError(f"unknown key {name!r}")


def key(combo: str, scancode: bool = False, hold_ms: int = 0) -> None:
    """Press a combination like 'ctrl+shift+s'. scancode=true for games."""
    vks = [_vk(k) for k in combo.replace(" ", "").split("+") if k]

    def ev(vk, release):
        flags = KEYEVENTF_KEYUP if release else 0
        if vk in _EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        if scancode:
            return _key(0, user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC), flags | KEYEVENTF_SCANCODE)
        return _key(vk, 0, flags)

    _send(*[ev(v, False) for v in vks])
    time.sleep(max(hold_ms, 20) / 1000)
    _send(*[ev(v, True) for v in reversed(vks)])


# ---------------------------------------------------------------- background


def _deepest_child(hwnd: int, sx: int, sy: int) -> tuple[int, int, int]:
    """Deepest child window at a screen point, plus the point in its client coords."""
    target = hwnd
    while True:
        p = wintypes.POINT(sx, sy)
        user32.ScreenToClient(target, ctypes.byref(p))
        child = user32.ChildWindowFromPointEx(target, p, CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT)
        if not child or child == target:
            return target, p.x, p.y
        target = child


def _lparam(x: int, y: int) -> int:
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


def post_click(hwnd: int, sx: int, sy: int, button: str = "left", count: int = 1) -> int:
    _, _, wm_down, wm_up, mk = _BUTTONS[button]
    target, cx, cy = _deepest_child(hwnd, sx, sy)
    lp = _lparam(cx, cy)
    user32.PostMessageW(target, WM_MOUSEMOVE, 0, lp)
    for i in range(count):
        down = WM_LBUTTONDBLCLK if (i == 1 and button == "left") else wm_down
        user32.PostMessageW(target, down, mk, lp)
        user32.PostMessageW(target, wm_up, 0, lp)
    return target


def _focus_child(hwnd: int) -> int:
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    gi = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if user32.GetGUIThreadInfo(tid, ctypes.byref(gi)) and gi.hwndFocus:
        return gi.hwndFocus
    return hwnd


def _send_msg(hwnd: int, msg: int, wp: int, lp: int) -> None:
    # Synchronous, so the text is in place when the call returns; the timeout
    # keeps a hung target from hanging the server.
    res = ctypes.c_size_t()
    user32.SendMessageTimeoutW(hwnd, msg, wp, lp, SMTO_ABORTIFHUNG, 1000, ctypes.byref(res))


def post_text(hwnd: int, text: str) -> int:
    target = _focus_child(hwnd)
    for ch in text.replace("\n", "\r"):
        for unit in ch.encode("utf-16-le").hex(" ", 2).split():
            _send_msg(target, WM_CHAR, int.from_bytes(bytes.fromhex(unit), "little"), 1)
    return target


def post_key(hwnd: int, combo: str) -> int:
    """Background key press. Modifiers are sent as messages too, but most apps check
    the real keyboard state for Ctrl/Alt/Shift, so combinations often arrive as the
    bare key."""
    target = _focus_child(hwnd)
    vks = [_vk(k) for k in combo.replace(" ", "").split("+") if k]
    for v in vks:
        sc = user32.MapVirtualKeyW(v, MAPVK_VK_TO_VSC)
        _send_msg(target, WM_KEYDOWN, v, 1 | sc << 16)
    for v in reversed(vks):
        sc = user32.MapVirtualKeyW(v, MAPVK_VK_TO_VSC)
        _send_msg(target, WM_KEYUP, v, 1 | sc << 16 | 0xC0000000)
    return target


def post_scroll(hwnd: int, sx: int, sy: int, clicks: int) -> int:
    target, _, _ = _deepest_child(hwnd, sx, sy)
    # WM_MOUSEWHEEL takes screen coordinates.
    user32.PostMessageW(target, WM_MOUSEWHEEL, (clicks * WHEEL_DELTA & 0xFFFF) << 16, _lparam(sx, sy))
    return target
