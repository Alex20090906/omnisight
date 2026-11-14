# OmniSight

> 繁體中文說明：[README.zh-TW.md](https://github.com/Alex20090906/omnisight/blob/main/README.zh-TW.md)

**Computer use for every Windows app window, for any MCP agent: GPU-rendered,
covered or in the background.**

Agents that drive a desktop through screenshots keep hitting the same walls on
Windows:

- GPU-rendered windows (games, emulators, Chromium / Electron, UWP) come back
  **black** from classic GDI capture.
- A window that is **covered** or in the **background** is not in a full-screen
  screenshot at all.
- Every step sends a **full-screen image** to a vision model just to find one
  button, which is slow and expensive.
- Clicks land in the wrong place on scaled and mixed-DPI monitors.

OmniSight is an MCP server that fixes these at the source. Claude Code, Codex,
Cursor, Cline, Claude Desktop and any other MCP client can use it.

## How it works

1. **Structure first, pixels second.** By default a snapshot returns the UI
   Automation element tree: every element has an id, a position and the actions
   it supports. The whole tree comes back in one cross-process call (a UIA cache
   request), typically in 30–300 ms, and it is text, not an image.
2. **Act on elements without the mouse.** `click(element="e7")` runs the UIA
   Invoke / Toggle / Select pattern and `type_text(replace=true)` sets the value
   directly. This works on background windows and does not steal focus.
3. **Capture one window, with backends that fall back automatically.**
   `printwindow` → `wgc` (Windows.Graphics.Capture) → `bitblt` → `screen`.
   A black or single-colour frame counts as a failure and the next backend is
   tried. The backend that worked is remembered per process.
4. **Image and OCR only when the tree is not enough.** In `auto` mode a
   screenshot and OCR text with positions are added when the tree does not
   describe the content (games, emulators, canvas apps, custom-drawn UIs).
5. **One coordinate space.** Element positions, OCR boxes and the image all use
   window-relative pixels of the snapshot image, so a position read from any of
   them can go straight into `click`. The process is per-monitor DPI aware.

Measured on a Windows 10 machine: a covered emulator window captured in about
100 ms, a 153-element Discord tree in about 130 ms, and repeat captures of a
covered Discord window in under 200 ms end to end over MCP.

## Tools

| Tool | What it does |
|---|---|
| `list_windows` | Windows front to back: hwnd, process, title, size, and flags such as minimized, admin, capture-blocked |
| `snapshot` | `mode` = `tree` / `image` / `ocr` / `auto` / `all` |
| `click` | Click an element id or an `x,y`; `background=true` posts messages instead of moving the mouse |
| `type_text` | Type text in any language, or set a control's value directly |
| `press_key` | Key combinations such as `ctrl+s`; `scancode=true` for games that read hardware scan codes |
| `scroll` / `drag` | Scroll and drag |
| `element_action` | invoke / toggle / expand / select / set_value / focus |
| `find_element` | Find elements by text |
| `wait_for_change` | Wait until the window's pixels change (loading, animations) |
| `screenshot_screen` | Whole-screen capture, for cross-window context |
| `focus_window` | Bring a window to the front |

The `window` argument takes an hwnd, or part of the title or process name.

## Install

Requires Windows 10 1903 or later and [uv](https://docs.astral.sh/uv/).
Nothing to install up front: `uvx` fetches OmniSight from PyPI the first time
the agent starts it.

The `[ocr]` extra adds RapidOCR (about 70 MB). It is far more accurate than the
built-in Windows OCR on small UI fonts and mixed Chinese / English text. Without
it, use `uvx omnisight` and OmniSight falls back to Windows OCR.

## Connect an agent

**Claude Code**

```bash
claude mcp add -s user omnisight -- uvx --from "omnisight[ocr]" omnisight
```

**Codex** (`~/.codex/config.toml`)

```toml
[mcp_servers.omnisight]
command = "uvx"
args = ["--from", "omnisight[ocr]", "omnisight"]
```

**Cursor, Cline, Claude Desktop and other `mcpServers` JSON configs**

```json
{
  "mcpServers": {
    "omnisight": {
      "command": "uvx",
      "args": ["--from", "omnisight[ocr]", "omnisight"]
    }
  }
}
```

**From source**

```bash
git clone https://github.com/Alex20090906/omnisight
cd omnisight
uv sync --extra ocr
claude mcp add -s user omnisight -- uv run --directory <path> omnisight
```

## Environment variables

| Variable | Meaning |
|---|---|
| `OMNISIGHT_OCR_ENGINE` | `auto` (default: RapidOCR if installed), `rapid` or `windows` |
| `OMNISIGHT_OCR_LANG` | Language for Windows OCR, such as `zh-Hant-TW` or `en-US` |

## What it deliberately does not do

- **It does not bypass `SetWindowDisplayAffinity`.** Password managers, banking
  apps and DRM video opt out of capture on purpose. OmniSight says so clearly
  and suggests the element tree, instead of returning a black image the agent
  would retry forever.
- **It does not evade anti-cheat or anti-bot detection**, and it does not touch
  the UAC secure desktop or the lock screen.
- **Admin windows:** Windows (UIPI) drops input sent to an elevated window from
  a non-elevated process. OmniSight reports this. To control such a window,
  start the agent from an elevated terminal.

## Known limitations

- On Windows 10, the WGC backend draws a yellow border around the captured
  window (Windows 11 can turn it off). That is why `printwindow` is tried first
  and WGC is used only when PrintWindow returns a black frame.
- Minimized windows have no pixels. `snapshot(restore_minimized=true)` restores
  the window without activating it.
- Background key presses (`background=true`) are reliable for single keys only:
  most apps read the real keyboard state for Ctrl / Alt / Shift.
- Background clicks work for classic Win32 and WinForms. Chromium and most
  games ignore them; use an element id, or leave `background` off.
- Chromium and Electron build their accessibility tree only after a client asks
  for it. `snapshot` retries once automatically.

## Layout

```
src/omnisight/
  winutil.py   window enumeration, DPI awareness, elevation, display affinity
  capture.py   capture backend chain, black-frame detection, WGC session pool
  uia.py       UI Automation tree (one cache request) and actions
  ocr.py       RapidOCR / Windows OCR
  inputs.py    SendInput and background message input
  server.py    MCP tools
```

## Development

```bash
uv sync --extra ocr --group dev
uv run pytest
```

## License

[Apache-2.0](https://github.com/Alex20090906/omnisight/blob/main/LICENSE)
