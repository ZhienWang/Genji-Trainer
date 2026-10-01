"""Frame sources: live screen capture, a folder of screenshots, or a recorded video.

Screen capture only. Nothing here reads game memory or sends input, which keeps
the app on the right side of Blizzard's rules for third-party tools.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from PIL import Image

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


@dataclass
class Frame:
    jpeg: bytes
    timestamp: float  # seconds; wall clock for live, position for video
    label: str = ""


def encode(img: Image.Image, width: int, quality: int = 80) -> bytes:
    img = img.convert("RGB")
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class ScreenSource:
    def __init__(self, monitor: int, width: int):
        import mss  # imported lazily so tests and replay mode don't need a display

        self._sct = mss.mss()
        if monitor >= len(self._sct.monitors):
            raise ValueError(f"Monitor {monitor} not found; you have {len(self._sct.monitors) - 1}")
        self._monitor = self._sct.monitors[monitor]
        self._width = width

    def grab(self) -> Frame:
        shot = self._sct.grab(self._monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return Frame(encode(img, self._width), time.time(), "live")


def folder_frames(folder: Path, width: int) -> Iterator[Frame]:
    paths = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    for i, p in enumerate(paths):
        with Image.open(p) as img:
            yield Frame(encode(img, width), float(i), p.name)


def video_frames(path: Path, width: int, every_seconds: float) -> Iterator[Frame]:
    try:
        import cv2
    except ImportError as e:
        raise SystemExit("Reviewing a video needs OpenCV: pip install opencv-python") from e
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise SystemExit(f"Couldn't open video {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(fps * every_seconds))
    index = 0
    try:
        while True:
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, bgr = cap.read()
            if not ok:
                break
            img = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            seconds = index / fps
            yield Frame(encode(img, width), seconds, f"{int(seconds // 60)}:{int(seconds % 60):02d}")
            index += step
    finally:
        cap.release()
