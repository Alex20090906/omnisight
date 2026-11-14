"""Checks that need no real windows: frame classification, coordinate mapping
and the MCP tool surface."""

import asyncio

import numpy as np

from omnisight import capture, server


def test_flat_frame_is_blank():
    assert capture.is_blank(np.zeros((200, 300, 3), np.uint8))
    assert capture.is_blank(np.full((200, 300, 3), 240, np.uint8))


def test_textured_frame_is_not_blank():
    img = np.random.default_rng(0).integers(0, 255, (200, 300, 3), dtype=np.uint8)
    assert not capture.is_blank(img)


def test_black_ratio_flags_gdi_style_black_frame():
    # GDI capture of a GPU window: a drawn title bar above a black client area.
    img = np.zeros((400, 600, 3), np.uint8)
    img[:20] = 200
    assert capture.black_ratio(img) > capture.SUSPECT_BLACK
    # A dark game with letterboxing is mostly, but not almost entirely, black.
    img[:, 200:400] = 90
    assert capture.black_ratio(img) < capture.SUSPECT_BLACK


def test_view_round_trip():
    v = server._View.__new__(server._View)
    v.origin, v.scale = (100, 50), 0.5
    assert v.to_screen(10, 20) == (120, 90)
    assert v.from_screen(120, 90) == (10, 20)


def test_tools_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"list_windows", "snapshot", "click", "type_text", "press_key", "scroll", "drag",
            "element_action", "find_element", "wait_for_change", "screenshot_screen",
            "focus_window"} <= names
