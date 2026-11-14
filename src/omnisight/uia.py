"""UI Automation element tree.

The whole subtree is fetched in one cross-process call (a UIA cache request
with TreeScope_Subtree), which is what makes this fast enough to use on every
step instead of a screenshot. Elements get short ids (e1, e2, ...) that stay
valid until the next snapshot of the same window.
"""

from __future__ import annotations

import concurrent.futures
import os
import tempfile
import threading
from dataclasses import dataclass

import uiautomation as auto
import uiautomation.uiautomation as _ua

from . import winutil as wu

# uiautomation logs errors to @AutomationLog.txt in the working directory, which
# for an MCP server is whatever project the agent is in.
auto.Logger.SetLogFile(os.path.join(tempfile.gettempdir(), "omnisight_uia.log"))

# Property ids (UIAutomationClient.h)
P_RECT, P_CTYPE, P_NAME = 30001, 30003, 30005
P_HASFOCUS, P_FOCUSABLE, P_ENABLED, P_AUTOID, P_CLASS = 30008, 30009, 30010, 30011, 30012
P_OFFSCREEN, P_PASSWORD = 30022, 30019
P_HAS_EXPAND, P_HAS_INVOKE, P_HAS_SCROLL = 30028, 30031, 30034
P_HAS_SELITEM, P_HAS_TEXT, P_HAS_TOGGLE, P_HAS_VALUE = 30036, 30040, 30041, 30043
P_VALUE, P_VALUE_RO, P_TOGGLE_STATE = 30045, 30046, 30086
P_EXPAND_STATE, P_SELECTED = 30070, 30079
PROPS = [P_RECT, P_CTYPE, P_NAME, P_HASFOCUS, P_FOCUSABLE, P_ENABLED, P_AUTOID, P_CLASS,
         P_OFFSCREEN, P_PASSWORD, P_HAS_EXPAND, P_HAS_INVOKE, P_HAS_SCROLL, P_HAS_SELITEM,
         P_HAS_TEXT, P_HAS_TOGGLE, P_HAS_VALUE, P_VALUE, P_VALUE_RO, P_TOGGLE_STATE,
         P_EXPAND_STATE, P_SELECTED]
TREESCOPE_SUBTREE = 7

_INTERACTIVE_TYPES = {
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem", "MenuItem",
    "RadioButton", "ScrollBar", "Slider", "Spinner", "SplitButton", "Tab", "TabItem",
    "TreeItem", "DataItem", "Document", "HeaderItem", "MenuBar",
}
_STRUCTURAL_TYPES = {"Window", "Pane", "Group", "Custom", "TitleBar", "ToolBar", "List",
                     "Tree", "Table", "DataGrid", "Menu", "StatusBar", "Header"}

# ---------------------------------------------------------------- COM thread
# UIA objects must be used from the apartment that created them. All UIA work
# runs on this one thread so cached elements stay usable between tool calls.


_com_init = []  # holds the initializer; it calls CoUninitialize when collected


def _init_com():
    threading.current_thread().name = "omnisight-uia"
    _com_init.append(_ua.UIAutomationInitializerInThread())


_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, initializer=_init_com)


def run(fn, *args, **kwargs):
    return _executor.submit(fn, *args, **kwargs).result()


# ---------------------------------------------------------------- snapshot


@dataclass
class Element:
    id: str
    ctype: str
    name: str
    rect: tuple[int, int, int, int]  # screen: left, top, right, bottom
    depth: int
    actions: list[str]
    state: list[str]
    value: str | None
    automation_id: str
    raw: object  # IUIAutomationElement, valid on the COM thread only

    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)


# hwnd -> {id: Element}
_registry: dict[int, dict[str, Element]] = {}


def _cached(el, pid):
    v = el.GetCachedPropertyValue(pid)
    # Unsupported properties come back as a truthy "NotSupported" sentinel object.
    return v if isinstance(v, (bool, int, str)) else None


def _snapshot(hwnd: int, max_elements: int, max_depth: int, include_offscreen: bool) -> list[Element]:
    client = _ua._AutomationClient.instance()
    iuia = client.IUIAutomation
    cr = iuia.CreateCacheRequest()
    for p in PROPS:
        cr.AddProperty(p)
    cr.TreeScope = TREESCOPE_SUBTREE
    cr.TreeFilter = iuia.ControlViewCondition
    root = iuia.ElementFromHandleBuildCache(hwnd, cr)

    l0, t0, r0, b0 = wu.window_rect(hwnd)
    out: list[Element] = []

    def visit(el, depth: int):
        if len(out) >= max_elements or depth > max_depth:
            return
        try:
            rect = el.CachedBoundingRectangle
            rect = (rect.left, rect.top, rect.right, rect.bottom)
            ctype = auto.ControlTypeNames.get(el.CachedControlType, "Unknown").replace("Control", "")
            name = (el.CachedName or "").strip()
            offscreen = bool(_cached(el, P_OFFSCREEN))
        except Exception:
            return
        has_area = rect[2] > rect[0] and rect[3] > rect[1]
        visible = has_area and not offscreen and rect[2] > l0 and rect[0] < r0 and rect[3] > t0 and rect[1] < b0
        actions = []
        if _cached(el, P_HAS_INVOKE):
            actions.append("invoke")
        if _cached(el, P_HAS_TOGGLE):
            actions.append("toggle")
        if _cached(el, P_HAS_EXPAND):
            actions.append("expand")
        if _cached(el, P_HAS_SELITEM):
            actions.append("select")
        if _cached(el, P_HAS_VALUE) and not _cached(el, P_VALUE_RO):
            actions.append("set_value")
        if _cached(el, P_HAS_SCROLL):
            actions.append("scroll")
        state = []
        if not _cached(el, P_ENABLED):
            state.append("disabled")
        if _cached(el, P_HASFOCUS):
            state.append("focused")
        if _cached(el, P_HAS_TOGGLE):
            state.append(["off", "on", "mixed"][_cached(el, P_TOGGLE_STATE) or 0])
        if _cached(el, P_HAS_SELITEM) and _cached(el, P_SELECTED):
            state.append("selected")
        if _cached(el, P_HAS_EXPAND):
            es = _cached(el, P_EXPAND_STATE)
            state.append({0: "collapsed", 1: "expanded"}.get(es, ""))
        value = None
        if _cached(el, P_HAS_VALUE):
            value = "••••" if _cached(el, P_PASSWORD) else str(_cached(el, P_VALUE) or "")[:200]

        if ctype in _STRUCTURAL_TYPES and not name and not value:
            # Chromium marks many anonymous containers invokable; they only add noise.
            interesting = False
        else:
            interesting = bool(actions or ctype in _INTERACTIVE_TYPES
                               or (name and ctype not in _STRUCTURAL_TYPES)
                               or (name and depth <= 2))
        keep = interesting and (visible or (include_offscreen and has_area))
        child_depth = depth
        if keep:
            out.append(Element(f"e{len(out) + 1}", ctype, name[:120], rect, depth, actions,
                               [s for s in state if s], value, el.CachedAutomationId or "", el))
            child_depth = depth + 1
        kids = el.GetCachedChildren()
        if not kids:  # leaf: comtypes returns a NULL pointer, not None
            return
        for i in range(kids.Length):
            visit(kids.GetElement(i), child_depth)

    visit(root, 0)
    _registry[hwnd] = {e.id: e for e in out}
    return out


def snapshot(hwnd: int, max_elements: int = 400, max_depth: int = 40,
             include_offscreen: bool = False) -> list[Element]:
    return run(_snapshot, hwnd, max_elements, max_depth, include_offscreen)


def format_elements(elements: list[Element], origin: tuple[int, int]) -> str:
    """One line per element, coordinates relative to the window image."""
    ox, oy = origin
    lines = []
    for e in elements:
        l, t, r, b = e.rect
        parts = ["  " * min(e.depth, 8) + f"[{e.id}] {e.ctype}"]
        if e.name:
            parts.append(f'"{e.name}"')
        if e.value not in (None, "") and e.value != e.name:
            parts.append(f"value={e.value!r}")
        parts.append(f"@{l - ox},{t - oy} {r - l}x{b - t}")
        if e.actions:
            parts.append("{" + ",".join(e.actions) + "}")
        if e.state:
            parts.append("(" + ",".join(e.state) + ")")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def get(hwnd: int, element_id: str) -> Element:
    el = _registry.get(hwnd, {}).get(element_id)
    if el is None:
        raise ValueError(f"unknown element {element_id!r} for window {hwnd}; take a new snapshot")
    return el


def find(hwnd: int, text: str, control_type: str | None = None) -> list[Element]:
    """Search the last snapshot (taking one if needed) by name/value/automation id."""
    if hwnd not in _registry:
        snapshot(hwnd)
    q = text.lower()
    hits = [e for e in _registry[hwnd].values()
            if q in e.name.lower() or q in (e.value or "").lower() or q == e.automation_id.lower()]
    if control_type:
        hits = [e for e in hits if e.ctype.lower() == control_type.lower()]
    hits.sort(key=lambda e: (e.name.lower() != q, not e.actions, len(e.name)))
    return hits


# ---------------------------------------------------------------- actions


def _control(el: Element):
    return auto.Control.CreateControlFromElement(el.raw)


def _do(hwnd: int, element_id: str, action: str, value: str | None):
    el = get(hwnd, element_id)
    c = _control(el)
    if action == "invoke":
        c.GetInvokePattern().Invoke()
    elif action == "toggle":
        c.GetTogglePattern().Toggle()
    elif action == "expand":
        p = c.GetExpandCollapsePattern()
        p.Collapse() if p.ExpandCollapseState == 1 else p.Expand()
    elif action == "select":
        c.GetSelectionItemPattern().Select()
    elif action == "set_value":
        c.GetValuePattern().SetValue(value or "")
    elif action == "focus":
        c.SetFocus()
    else:
        raise ValueError(f"unknown action {action!r}")


def do(hwnd: int, element_id: str, action: str, value: str | None = None) -> None:
    run(_do, hwnd, element_id, action, value)


def default_action(el: Element) -> str | None:
    for a in ("invoke", "toggle", "select", "expand"):
        if a in el.actions:
            return a
    return None
