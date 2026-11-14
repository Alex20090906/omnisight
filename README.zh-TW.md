# OmniSight

> English: [README.md](README.md)

**讓任何 MCP 代理都能操作 Windows 上的任意應用程式視窗：GPU 繪製的、被擋住的、在背景的都可以。**

靠截圖操作桌面的代理，在 Windows 上一直撞到同樣幾道牆：

- GPU 繪製的視窗（遊戲、模擬器、Chromium / Electron、UWP）用傳統 GDI 截圖會是**黑畫面**。
- 視窗被**擋住**或在**背景**時，全螢幕截圖根本看不到它。
- 每一步都把**整個螢幕的圖片**送給視覺模型，只為了找一個按鈕，又慢又貴。
- 在有縮放或混合 DPI 的螢幕上，點擊位置會偏掉。

OmniSight 是一個 MCP server，從根源解決這些問題。Claude Code、Codex、Cursor、Cline、Claude Desktop 等所有 MCP 用戶端都能使用。

## 運作方式

1. **先讀結構，再看像素。** 快照預設回傳 UI Automation 元素樹：每個元素都有 id、位置和可用的動作。整棵樹用一次跨行程呼叫取得（UIA cache request），通常 30–300 ms，而且是文字，不是圖片。
2. **不動滑鼠就能操作元素。** `click(element="e7")` 直接執行 UIA 的 Invoke / Toggle / Select；`type_text(replace=true)` 直接設定控制項的值。視窗在背景也能用，不會搶走焦點。
3. **只截目標視窗，截圖後端自動切換。** `printwindow` → `wgc`（Windows.Graphics.Capture）→ `bitblt` → `screen`。全黑或單一顏色的畫面視為失敗，改用下一個後端；每個行程會記住上次成功的後端。
4. **元素樹不夠時才加上截圖和 OCR。** `auto` 模式下，如果元素樹描述不了視窗內容（遊戲、模擬器、canvas、自繪介面），會自動加上截圖和帶位置的 OCR 文字。
5. **同一套座標。** 元素位置、OCR 方框和圖片都用「相對視窗左上角、快照圖片的像素」，從任何一個讀到的位置都能直接傳給 `click`。程式本身是 per-monitor DPI aware。

在一台 Windows 10 電腦上的實測：被擋住的模擬器視窗約 100 ms 截到；Discord 153 個元素的元素樹約 130 ms；透過 MCP 端到端重複截取被擋住的 Discord 視窗不到 200 ms。

## 工具

| 工具 | 用途 |
|---|---|
| `list_windows` | 由前到後列出視窗：hwnd、行程、標題、尺寸，以及 minimized / admin / capture-blocked 等標記 |
| `snapshot` | `mode` = `tree` / `image` / `ocr` / `auto` / `all` |
| `click` | 用元素 id 或 `x,y` 點擊；`background=true` 改用訊息傳送，不移動滑鼠 |
| `type_text` | 輸入任何語言的文字，或直接設定控制項的值 |
| `press_key` | 組合鍵，例如 `ctrl+s`；`scancode=true` 給讀取硬體掃描碼的遊戲用 |
| `scroll` / `drag` | 捲動、拖曳 |
| `element_action` | invoke / toggle / expand / select / set_value / focus |
| `find_element` | 用文字找元素 |
| `wait_for_change` | 等視窗畫面變化（載入、動畫結束） |
| `screenshot_screen` | 整個螢幕的截圖，需要跨視窗資訊時使用 |
| `focus_window` | 把視窗帶到前景 |

`window` 參數接受 hwnd，或標題、行程名稱的一部分。

## 安裝

需要 Windows 10 1903 以上和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/Alex20090906/omnisight
cd omnisight
uv sync --extra ocr
```

`--extra ocr` 會安裝 RapidOCR（約 70 MB）。它在小字級介面文字和中英混排上，比 Windows 內建 OCR 準確得多。不裝也能用，會改用 Windows 內建 OCR。

## 接上代理

把 `<路徑>` 換成這個資料夾的絕對路徑。

**Claude Code**

```bash
claude mcp add -s user omnisight -- uv run --directory <路徑> omnisight
```

**Codex**（`~/.codex/config.toml`）

```toml
[mcp_servers.omnisight]
command = "uv"
args = ["run", "--directory", "<路徑>", "omnisight"]
```

**Cursor、Cline、Claude Desktop 等使用 `mcpServers` JSON 的用戶端**

```json
{
  "mcpServers": {
    "omnisight": {
      "command": "uv",
      "args": ["run", "--directory", "<路徑>", "omnisight"]
    }
  }
}
```

## 環境變數

| 變數 | 說明 |
|---|---|
| `OMNISIGHT_OCR_ENGINE` | `auto`（預設：有裝 RapidOCR 就用）、`rapid` 或 `windows` |
| `OMNISIGHT_OCR_LANG` | Windows OCR 的語言，例如 `zh-Hant-TW`、`en-US` |

## 刻意不做的事

- **不繞過 `SetWindowDisplayAffinity`。** 密碼管理器、網銀、DRM 影片會主動禁止截圖。OmniSight 會明確說明，並建議改用元素樹，不會回傳一張讓代理一直重試的黑圖。
- **不規避反作弊或反機器人偵測**，也不碰 UAC 安全桌面和鎖定畫面。
- **系統管理員權限的視窗：** Windows（UIPI）會丟掉非提權行程送給提權視窗的輸入。OmniSight 會直接回報；需要操作這類視窗時，請從系統管理員終端機啟動代理。

## 已知限制

- 在 Windows 10 上，WGC 後端會在被截取的視窗周圍畫黃框（Windows 11 可以關掉）。所以先試 `printwindow`，只有它截到黑畫面時才用 WGC。
- 最小化的視窗沒有畫面。`snapshot(restore_minimized=true)` 會在不啟用視窗的情況下把它還原。
- 背景按鍵（`background=true`）只對單一按鍵可靠：大多數程式會讀取真實鍵盤狀態來判斷 Ctrl / Alt / Shift。
- 背景點擊對傳統 Win32 和 WinForms 有效，Chromium 和多數遊戲會忽略。這時請改用元素 id，或不要開 `background`。
- Chromium 和 Electron 要在第一次被查詢後才建立無障礙樹；`snapshot` 會自動重試一次。

## 結構

```
src/omnisight/
  winutil.py   視窗列舉、DPI 感知、提權偵測、display affinity
  capture.py   截圖後端鏈、黑畫面偵測、WGC 工作階段池
  uia.py       UI Automation 元素樹（單次 cache request）與動作
  ocr.py       RapidOCR / Windows OCR
  inputs.py    SendInput 與背景訊息輸入
  server.py    MCP 工具
```

## 開發

```bash
uv sync --extra ocr --group dev
uv run pytest
```

## 授權

[Apache-2.0](LICENSE)
