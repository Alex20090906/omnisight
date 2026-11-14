"""Text with positions, for windows that expose no element tree (games,
emulators, canvas apps, remote desktops): the agent gets text plus click
coordinates without having to read them off an image.

Engines: RapidOCR when installed (accurate on UI text and mixed CJK/Latin),
otherwise the built-in Windows OCR (Windows.Media.Ocr, no extra download).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
from dataclasses import dataclass

import numpy as np

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)


@dataclass
class OcrLine:
    text: str
    rect: tuple[int, int, int, int]  # image coords: left, top, right, bottom


_engines: dict[str, object] = {}


def available_languages() -> list[str]:
    from winrt.windows.media.ocr import OcrEngine

    return [l.language_tag for l in OcrEngine.available_recognizer_languages]


def _engine(lang: str | None):
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine

    key = lang or ""
    if key not in _engines:
        eng = (OcrEngine.try_create_from_language(Language(lang)) if lang
               else OcrEngine.try_create_from_user_profile_languages())
        if eng is None:
            raise RuntimeError(f"No Windows OCR language pack for {lang or 'the user profile'}. "
                               f"Installed: {available_languages()}")
        _engines[key] = eng
    return _engines[key]


async def _recognize(img: np.ndarray, lang: str | None):
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    h, w = img.shape[:2]
    bgra = np.empty((h, w, 4), dtype=np.uint8)
    bgra[:, :, :3] = img[:, :, ::-1]
    bgra[:, :, 3] = 255
    writer = DataWriter()
    writer.write_bytes(bgra.tobytes())
    bmp = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
    return await _engine(lang).recognize_async(bmp)


def _default_lang() -> str | None:
    """A CJK engine if one is installed (they read Latin text too, the reverse is
    not true); otherwise the user's profile languages."""
    for tag in available_languages():
        if tag.split("-")[0] in ("zh", "ja", "ko"):
            return tag
    return None


_rapid = []


def _rapid_engine():
    """RapidOCR (PaddleOCR models on ONNX Runtime): far better than Windows OCR on
    small UI fonts and mixed Chinese/English. Optional: `pip install omnisight[ocr]`."""
    if not _rapid:
        from rapidocr_onnxruntime import RapidOCR

        _rapid.append(RapidOCR())
    return _rapid[0]


def engine_name() -> str:
    choice = os.environ.get("OMNISIGHT_OCR_ENGINE", "auto")
    if choice in ("auto", "rapid"):
        try:
            import rapidocr_onnxruntime  # noqa: F401

            return "rapid"
        except ImportError:
            if choice == "rapid":
                raise
    return "windows"


def _recognize_rapid(img: np.ndarray) -> list[OcrLine]:
    res, _ = _rapid_engine()(np.ascontiguousarray(img[:, :, ::-1]))  # expects BGR
    out = []
    for box, text, _score in res or []:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        out.append(OcrLine(text, (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))))
    return out


def recognize(img: np.ndarray, lang: str | None = None, scale: float = 1.0) -> list[OcrLine]:
    """img is RGB. scale > 1 upsamples first (Windows engine), which helps with small fonts."""
    if engine_name() == "rapid" and not lang:
        return _pool.submit(_recognize_rapid, img).result()
    if scale != 1.0:
        from PIL import Image

        im = Image.fromarray(img)
        img = np.asarray(im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS))
    lang = lang or os.environ.get("OMNISIGHT_OCR_LANG") or _default_lang()
    # Own thread: the MCP server already runs an event loop on the calling thread.
    result = _pool.submit(asyncio.run, _recognize(img, lang)).result()
    out = []
    for line in result.lines:
        words = list(line.words)
        if not words:
            continue
        l = min(wd.bounding_rect.x for wd in words)
        t = min(wd.bounding_rect.y for wd in words)
        r = max(wd.bounding_rect.x + wd.bounding_rect.width for wd in words)
        b = max(wd.bounding_rect.y + wd.bounding_rect.height for wd in words)
        # Windows OCR puts spaces between CJK characters; drop them.
        text = ""
        for wd in words:
            if text and not (text[-1] > "⺀" and wd.text[:1] > "⺀"):
                text += " "
            text += wd.text
        out.append(OcrLine(text, tuple(int(v / scale) for v in (l, t, r, b))))
    return out
