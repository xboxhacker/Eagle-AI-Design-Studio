"""Images attached to a chat message (pasted, dropped or picked): scaled for the AI, saved next to the chat
(.eagleai/chat_images) and sent as base64.

An attachment is a dict: {"name", "media_type", "data" (bytes), "w", "h", "path" (once saved)}.
Qt is only imported by the functions that read pictures, so the agent can use save()/b64() without a GUI.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Dict, List, Optional

MAX_EDGE = 1568            # Claude's recommended long edge; larger pictures cost more and are downscaled anyway
MAX_BYTES = 3_500_000      # stay well under the 5 MB per-image API limit
MAX_IMAGES = 10
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")


def from_qimage(img, name: str = "pasted") -> Optional[Dict]:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage
    if img is None or img.isNull():
        return None
    if max(img.width(), img.height()) > MAX_EDGE:
        img = img.scaled(MAX_EDGE, MAX_EDGE, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def encode(im, fmt, quality=-1) -> bytes:
        ba = QByteArray(); buf = QBuffer(ba); buf.open(QIODevice.WriteOnly)
        im.save(buf, fmt, quality)
        return bytes(ba.data())
    data, mt = encode(img, "PNG"), "image/png"
    if len(data) > MAX_BYTES:                      # photos / big screenshots: JPEG is far smaller
        data, mt = encode(img.convertToFormat(QImage.Format_RGB888), "JPEG", 85), "image/jpeg"
    return {"name": name, "media_type": mt, "data": data, "w": img.width(), "h": img.height()}


def from_file(path) -> Optional[Dict]:
    from PySide6.QtGui import QImage
    p = Path(path)
    if p.suffix.lower() not in IMAGE_EXT:
        return None
    return from_qimage(QImage(str(p)), p.name)


def tokens(att: Dict) -> int:
    """Rough input tokens for one picture (Anthropic: width x height / 750)."""
    return max(1, int(att.get("w", 0) * att.get("h", 0) / 750))


def b64(att: Dict) -> str:
    return base64.b64encode(att["data"]).decode("ascii")


def data_url(att: Dict) -> str:
    return f"data:{att['media_type']};base64,{b64(att)}"


def save(atts: List[Dict], folder: Path) -> List[str]:
    """Write the attachments to <folder> (once); returns their paths."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = []
    for i, a in enumerate(atts):
        if not a.get("path"):
            ext = ".jpg" if a["media_type"] == "image/jpeg" else ".png"
            f = folder / f"{stamp}_{i + 1}{ext}"
            n = 2
            while f.exists():
                f = folder / f"{stamp}_{i + 1}_{n}{ext}"; n += 1
            f.write_bytes(a["data"])
            a["path"] = str(f)
        out.append(a["path"])
    return out
