"""OmniSight MCP server: see and operate any Windows app window.

Coordinate space
----------------
Every x/y this server returns or accepts is relative to the top-left corner of
the target window, in the pixel grid of that window's snapshot image (images
are scaled down to `image_max_side`). Element positions, OCR boxes and the
image all use this one space, so a position read from any of them can be
passed to click/scroll/drag unchanged.
"""

from __future__ import annotations

import io
import time

import numpy as np
from mcp.server.mcpserver import Image, MCPServer
from PIL import Image as PILImage

from . import capture as cap
from . import inputs as inp
from . import ocr
from . import uia
from . import winutil as wu

mcp = MCPServer(
    "omnisight",
    instructions=(
        "Windows desktop control that works on GPU-rendered, covered and background windows. "
        "Workflow: list_windows -> snapshot(window) -> act by element id (preferred, no mouse "
        "needed) or by x,y from the same snapshot. Prefer mode='tree' (fast, text only); use "
        "'image' or 'ocr' when the tree is empty or does not show what you need (games, "
        "emulators, canvas apps). All coordinates are window-relative snapshot pixels."
    ),
)

DEFAULT_MAX_SIDE = 1280


class _View:
    """How snapshot coordinates map to the screen for one window."""

    def __init__(self, hwnd: int, max_side: int):
        l, t, r, b = wu.window_rect(hwnd)
        self.origin = (l, t)
        self.scale = min(1.0, max_side / max(1, r - l, b - t))

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        return (round(self.origin[0] + x / self.scale), round(self.origin[1] + y / self.scale))

    def from_screen(self, sx: float, sy: float) -> tuple[int, int]:
        return (round((sx - self.origin[0]) * self.scale), round((sy - self.origin[1]) * self.scale))


_views: dict[int, _View] = {}
_last_frame: dict[int, np.ndarray] = {}


def _resolve(window: str) -> wu.WindowInfo:
    return wu.find_window(window)


def _view(hwnd: int, refresh: bool = False, max_side: int | None = None) -> _View:
    v = _views.get(hwnd)
    if v is None or refresh or max_side is not None:
        v = _views[hwnd] = _View(hwnd, max_side or DEFAULT_MAX_SIDE)
    else:
        # Window may have moved since the snapshot; keep the scale, refresh the origin.
        v.origin = wu.window_rect(hwnd)[:2]
    return v


def _encode(img: np.ndarray, scale: float, fmt: str) -> Image:
    im = PILImage.fromarray(img)
    if scale < 1.0:
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                       PILImage.LANCZOS)
    buf = io.BytesIO()
    if fmt == "png":
        im.save(buf, "PNG", optimize=True)
    else:
        im.save(buf, "JPEG", quality=85)
        fmt = "jpeg"
    return Image(data=buf.getvalue(), format=fmt)


def _window_line(w: wu.WindowInfo) -> str:
    flags = [f for f, on in [("minimized", w.minimized), ("foreground", w.foreground),
                             ("capture-blocked", w.capture_blocked), ("admin", w.elevated),
                             ("other-desktop", w.cloaked)] if on]
    return (f"{w.hwnd}  {w.process}  \"{w.title[:80]}\"  {w.width}x{w.height}"
            + (f"  [{', '.join(flags)}]" if flags else ""))


def _useful(els: list[uia.Element]) -> int:
    return sum(1 for e in els if (e.name or e.value) and e.ctype not in ("Window", "Pane", "TitleBar"))


def _opaque_area(els: list[uia.Element], hwnd: int) -> float:
    """Largest share of the window covered by one element with nothing inside it,
    e.g. a game viewport, emulator screen or canvas."""
    l, t, r, b = wu.window_rect(hwnd)
    area = max(1, (r - l) * (b - t))
    best = 0.0
    for i, e in enumerate(els):
        if i + 1 < len(els) and els[i + 1].depth > e.depth:
            continue  # has children in the snapshot
        el, et, er, eb = e.rect
        w, h = min(er, r) - max(el, l), min(eb, b) - max(et, t)
        if w > 0 and h > 0:
            best = max(best, w * h / area)
    return best


def _tree_text(hwnd: int, view: _View, max_elements: int) -> tuple[str, int, float]:
    els = uia.snapshot(hwnd, max_elements=max_elements)
    if _useful(els) < 3:
        # Chromium/Electron build their accessibility tree only after a client asks.
        time.sleep(0.4)
        els = uia.snapshot(hwnd, max_elements=max_elements)
    lines = []
    for e in els:
        l, t = view.from_screen(e.rect[0], e.rect[1])
        r, b = view.from_screen(e.rect[2], e.rect[3])
        parts = ["  " * min(e.depth, 8) + f"[{e.id}] {e.ctype}"]
        if e.name:
            parts.append(f'"{e.name}"')
        if e.value not in (None, "") and e.value != e.name:
            parts.append(f"value={e.value!r}")
        parts.append(f"@{l},{t} {r - l}x{b - t}")
        if e.actions:
            parts.append("{" + ",".join(e.actions) + "}")
        if e.state:
            parts.append("(" + ",".join(e.state) + ")")
        lines.append(" ".join(parts))
    return "\n".join(lines), _useful(els), _opaque_area(els, hwnd)


def _ocr_text(img: np.ndarray, view: _View, lang: str | None) -> str:
    lines = ocr.recognize(img, lang=lang)
    out = []
    for ln in lines:
        l, t, r, b = (round(v * view.scale) for v in ln.rect)
        out.append(f'"{ln.text}" @{l},{t} {r - l}x{b - t}')
    return "\n".join(out) if out else "(no text found)"


# ===================================================================== tools


@mcp.tool()
def list_windows(filter: str = "") -> str:
    """List top-level app windows (front to back). Each line: hwnd, process, title, size, flags.
    Pass the hwnd (or a unique part of the title or process name) as `window` to other tools."""
    ws = wu.list_windows()
    if filter:
        f = filter.lower()
        ws = [w for w in ws if f in w.title.lower() or f in w.process.lower()]
    me = "admin" if wu.self_elevated() else "not admin"
    return f"(server is {me})\n" + ("\n".join(_window_line(w) for w in ws) or "no windows")


@mcp.tool()
def snapshot(window: str, mode: str = "auto", max_elements: int = 300,
             image_max_side: int = DEFAULT_MAX_SIDE, image_format: str = "jpeg",
             restore_minimized: bool = False, ocr_lang: str = "") -> list:
    """Observe a window without needing it to be in front.

    mode:
      tree  - UI Automation elements with ids, positions and available actions. Fastest.
      image - screenshot of just this window (works for GPU, covered and background windows).
      ocr   - text lines with positions, from the built-in Windows OCR.
      auto  - tree; adds image + ocr when the tree has too little in it (games, canvas apps).
      all   - tree + ocr + image.
    Coordinates in the result are window-relative pixels of the returned image.
    """
    info = _resolve(window)
    hwnd = info.hwnd
    view = _view(hwnd, max_side=image_max_side)
    header = [_window_line(info)]
    out: list = []

    want_tree = mode in ("tree", "auto", "all")
    want_img = mode in ("image", "all")
    want_ocr = mode in ("ocr", "all")

    if want_tree:
        t0 = time.perf_counter()
        try:
            text, useful, opaque = _tree_text(hwnd, view, max_elements)
            header.append(f"elements ({useful} named, {(time.perf_counter() - t0) * 1000:.0f} ms):")
            out.append(text)
        except Exception as e:  # noqa: BLE001
            useful, opaque = 0, 1.0
            header.append(f"element tree unavailable: {e}")
        if mode == "auto" and (useful < 5 or opaque > 0.3):
            want_img = want_ocr = True
            header.append("tree does not describe the window content (game, emulator, canvas or "
                          "custom-drawn app); adding image and OCR")

    if want_img or want_ocr:
        try:
            c = cap.capture_window(hwnd, restore_minimized=restore_minimized)
        except cap.CaptureError as e:
            header.append(f"capture failed: {e}")
        else:
            view = _view(hwnd, max_side=image_max_side)  # restore may have moved it
            _last_frame[hwnd] = c.image
            iw, ih = round(c.image.shape[1] * view.scale), round(c.image.shape[0] * view.scale)
            header.append(f"captured via {c.backend} in {c.elapsed_ms:.0f} ms; image {iw}x{ih}"
                          + (f" (skipped: {'; '.join(c.attempts)})" if c.attempts else ""))
            if want_ocr:
                try:
                    out.append("ocr:\n" + _ocr_text(c.image, view, ocr_lang or None))
                except Exception as e:  # noqa: BLE001
                    header.append(f"ocr failed: {e}")
            if want_img:
                out.append(_encode(c.image, view.scale, image_format))

    return ["\n".join(header)] + out


@mcp.tool()
def screenshot_screen(monitor: int = 0, image_max_side: int = DEFAULT_MAX_SIDE) -> list:
    """Whole-screen capture (monitor=0 for all monitors, 1.. for one). Use only for
    cross-window context; per-window snapshot is faster and works when covered.
    Coordinates are NOT window-relative here: they are screen pixels times the reported scale."""
    mons = cap.monitors()
    if monitor:
        m = mons[monitor - 1]
        region = (m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])
    else:
        region = cap.virtual_screen()
    c = cap.capture_screen(region)
    scale = min(1.0, image_max_side / max(c.image.shape[:2]))
    return [f"origin {region[0]},{region[1]} scale {scale:.4f}; monitors: {mons}",
            _encode(c.image, scale, "jpeg")]


def _target_point(hwnd: int, element: str, x: float | None, y: float | None):
    view = _view(hwnd)
    if element:
        el = uia.get(hwnd, element)
        return el, el.center()
    if x is None or y is None:
        raise ValueError("give either element or both x and y")
    return None, view.to_screen(x, y)


@mcp.tool()
def click(window: str, element: str = "", x: float | None = None, y: float | None = None,
          button: str = "left", count: int = 1, background: bool = False) -> str:
    """Click an element (by id from snapshot) or a point (x, y from snapshot).

    An element with an invoke/toggle/select action is activated through UI Automation:
    no mouse movement, works on background windows. Otherwise:
      background=false  bring window to front and click with real mouse input (works everywhere)
      background=true   post click messages; no focus change, but many apps ignore them
    """
    info = _resolve(window)
    hwnd = info.hwnd
    el, (sx, sy) = _target_point(hwnd, element, x, y)
    if el is not None and button == "left" and count == 1:
        act = uia.default_action(el)
        if act:
            try:
                uia.do(hwnd, el.id, act)
                return f"{act} {el.ctype} \"{el.name}\" via UI Automation"
            except Exception as e:  # noqa: BLE001 - fall back to a real click
                note = f" (UIA {act} failed: {e})"
        else:
            note = ""
    else:
        note = ""
    inp.check_uipi(hwnd)
    if background:
        target = inp.post_click(hwnd, sx, sy, button, count)
        return f"posted {button} click x{count} to child window {target}{note}"
    inp.ensure_foreground(hwnd)
    if wu.root_at(sx, sy) != hwnd:
        raise inp.InputBlocked("another window covers that point even after focusing the target "
                               "(always-on-top window?). Use background=true or an element id.")
    inp.click(sx, sy, button, count)
    return f"clicked {button} x{count} at screen {sx},{sy}{note}"


@mcp.tool()
def type_text(window: str, text: str, element: str = "", replace: bool = False,
              background: bool = False) -> str:
    """Type text (any language). With an element that supports set_value and replace=true, the
    value is set directly through UI Automation (fast, background-safe). Otherwise the element
    (if given) is focused and the text is typed as Unicode key events."""
    info = _resolve(window)
    hwnd = info.hwnd
    if element:
        el = uia.get(hwnd, element)
        if replace and "set_value" in el.actions:
            uia.do(hwnd, element, "set_value", text)
            return f"set value of {el.ctype} \"{el.name}\" via UI Automation"
    inp.check_uipi(hwnd)
    if background:
        if element:
            uia.do(hwnd, element, "focus")
        target = inp.post_text(hwnd, text)
        return f"posted {len(text)} chars to window {target}"
    inp.ensure_foreground(hwnd)
    if element:
        uia.do(hwnd, element, "focus")
        if replace:
            inp.key("ctrl+a")
    inp.type_text(text)
    return f"typed {len(text)} chars"


@mcp.tool()
def press_key(window: str, keys: str, background: bool = False, scancode: bool = False,
              hold_ms: int = 0) -> str:
    """Press a key or combination, e.g. 'enter', 'ctrl+s', 'alt+f4', 'shift+tab', 'w'.
    scancode=true sends hardware scan codes (needed by many games); hold_ms keeps keys down.
    background=true is reliable for single keys only; most apps ignore modifiers sent that way."""
    info = _resolve(window)
    inp.check_uipi(info.hwnd)
    if background:
        target = inp.post_key(info.hwnd, keys)
        return f"posted {keys} to window {target}"
    inp.ensure_foreground(info.hwnd)
    inp.key(keys, scancode=scancode, hold_ms=hold_ms)
    return f"pressed {keys}"


@mcp.tool()
def scroll(window: str, amount: int, element: str = "", x: float | None = None,
           y: float | None = None, horizontal: bool = False, background: bool = False) -> str:
    """Scroll at an element or point. amount: wheel notches, positive = up/left... negative =
    down. Without element/x/y, scrolls at the window centre."""
    info = _resolve(window)
    hwnd = info.hwnd
    if not element and (x is None or y is None):
        l, t, r, b = info.rect
        sx, sy = (l + r) // 2, (t + b) // 2
    else:
        _, (sx, sy) = _target_point(hwnd, element, x, y)
    inp.check_uipi(hwnd)
    if background and not horizontal:
        inp.post_scroll(hwnd, sx, sy, amount)
        return f"posted scroll {amount}"
    inp.ensure_foreground(hwnd)
    inp.scroll(sx, sy, amount, horizontal)
    return f"scrolled {amount}"


@mcp.tool()
def drag(window: str, x1: float, y1: float, x2: float, y2: float, button: str = "left") -> str:
    """Drag with the real mouse from (x1, y1) to (x2, y2), snapshot coordinates."""
    info = _resolve(window)
    inp.check_uipi(info.hwnd)
    view = _view(info.hwnd)
    a, b = view.to_screen(x1, y1), view.to_screen(x2, y2)
    inp.ensure_foreground(info.hwnd)
    inp.drag(*a, *b, button=button)
    return f"dragged {a} -> {b}"


@mcp.tool()
def element_action(window: str, element: str, action: str, value: str = "") -> str:
    """Run a UI Automation action on an element: invoke, toggle, expand, select, set_value,
    focus. Works on background windows without moving the mouse."""
    info = _resolve(window)
    el = uia.get(info.hwnd, element)
    uia.do(info.hwnd, element, action, value)
    return f"{action} on {el.ctype} \"{el.name}\" done"


@mcp.tool()
def find_element(window: str, text: str, control_type: str = "", refresh: bool = True) -> str:
    """Find elements whose name, value or automation id contains `text`.
    control_type filters, e.g. Button, Edit, MenuItem, ListItem, Hyperlink."""
    info = _resolve(window)
    if refresh:
        uia.snapshot(info.hwnd)
    view = _view(info.hwnd)
    hits = uia.find(info.hwnd, text, control_type or None)[:20]
    if not hits:
        return "no match; try snapshot(mode='ocr') if the app draws its own UI"
    out = []
    for e in hits:
        x, y = view.from_screen(*e.center())
        out.append(f"[{e.id}] {e.ctype} \"{e.name}\" center {x},{y} {{{','.join(e.actions)}}}")
    return "\n".join(out)


@mcp.tool()
def wait_for_change(window: str, timeout_s: float = 5.0, threshold: float = 0.002) -> str:
    """Wait until the window's pixels change (after an action that loads or animates).
    Returns when at least `threshold` of pixels differ, or on timeout."""
    info = _resolve(window)
    hwnd = info.hwnd
    base = _last_frame.get(hwnd)
    if base is None:
        base = cap.capture_window(hwnd).image
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        time.sleep(0.15)
        img = cap.capture_window(hwnd).image
        if img.shape != base.shape:
            _last_frame[hwnd] = img
            return "changed (window resized)"
        diff = float((np.abs(img[::4, ::4].astype(np.int16) - base[::4, ::4]).max(axis=2) > 24).mean())
        if diff >= threshold:
            _last_frame[hwnd] = img
            return f"changed ({diff:.1%} of pixels)"
    return "no change before timeout"


@mcp.tool()
def focus_window(window: str) -> str:
    """Bring a window to the front (restores it if minimized)."""
    info = _resolve(window)
    ok = wu.focus(info.hwnd)
    return "focused" if ok else "Windows refused the focus change; ask the user to click it"


def main() -> None:
    # Load the OCR model now so the first OCR snapshot is not 2 s slower.
    if ocr.engine_name() == "rapid":
        ocr._pool.submit(ocr._recognize_rapid, np.full((32, 32, 3), 255, np.uint8))
    mcp.run()


if __name__ == "__main__":
    main()
