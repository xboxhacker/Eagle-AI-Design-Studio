"""Symbol placement shared by the .sch writer and the SVG preview."""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .library import Device, Pin

GRID = 2.54
PASSIVE_PREFIXES = ("R", "C", "L", "D", "LED", "F", "FB")


def snap(v: float, step: float = GRID) -> float:
    return round(round(v / step) * step, 4)


def symbol_bbox(sym: ET.Element) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for el in sym:
        t = el.tag
        try:
            if t in ("wire", "rectangle"):
                xs += [float(el.get("x1")), float(el.get("x2"))]
                ys += [float(el.get("y1")), float(el.get("y2"))]
            elif t == "circle":
                x, y, r = float(el.get("x")), float(el.get("y")), float(el.get("radius"))
                xs += [x - r, x + r]
                ys += [y - r, y + r]
            elif t == "pin":
                xs.append(float(el.get("x")))
                ys.append(float(el.get("y")))
            elif t == "vertex":
                xs.append(float(el.get("x")))
                ys.append(float(el.get("y")))
            elif t == "polygon":
                for v in el.findall("vertex"):
                    xs.append(float(v.get("x")))
                    ys.append(float(v.get("y")))
        except (TypeError, ValueError):
            continue
    if not xs:
        return (-5.08, -5.08, 5.08, 5.08)
    return (min(xs), min(ys), max(xs), max(ys))


def pin_inward(rot: str) -> Tuple[int, int]:
    """Direction the pin line points (into the symbol body)."""
    digits = re.sub(r"\D", "", rot or "0") or "0"
    ang = int(digits) % 360
    return {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}.get(ang, (1, 0))


def pin_stub_dir(pin: Pin) -> Tuple[int, int]:
    dx, dy = pin_inward(pin.rot)
    return (-dx, -dy)


@dataclass
class Block:
    ref: str
    gate: str
    device: Device
    ox: float
    oy: float
    bbox: Tuple[float, float, float, float]


@dataclass
class Layout:
    blocks: List[Block] = field(default_factory=list)
    extents: Tuple[float, float, float, float] = (0, 0, 100, 100)

    def block(self, ref: str, gate: str) -> Block:
        return next(b for b in self.blocks if b.ref == ref and b.gate == gate)

    def pin_point(self, ref: str, pin: Pin) -> Tuple[float, float]:
        b = self.block(ref, pin.gate)
        return (round(b.ox + pin.x, 4), round(b.oy + pin.y, 4))


def _order_key(ref: str) -> Tuple[int, str, int]:
    pre = re.sub(r"\d", "", ref)
    num = int(re.sub(r"\D", "", ref) or 0)
    group = 1 if pre in PASSIVE_PREFIXES else 0
    return (group, pre, num)


def build_layout(devices: Dict[str, Device], max_width: float = 330.0, pad: float = 17.78,
                 gap: float = 5.08) -> Layout:
    lay = Layout()
    cur_x = cur_y = 0.0
    row_h = 0.0
    for ref in sorted(devices, key=_order_key):
        dev = devices[ref]
        for gate in dev.gates:
            sym = dev.symbols.get(gate.symbol)
            if sym is None:
                continue
            x0, y0, x1, y1 = symbol_bbox(sym)
            w, h = (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad
            if cur_x > 0 and cur_x + w > max_width:
                cur_x = 0.0
                cur_y -= row_h + gap
                row_h = 0.0
            ox = snap(cur_x + pad - x0)
            oy = snap(cur_y - pad - y1)
            lay.blocks.append(Block(ref, gate.name, dev, ox, oy, (x0, y0, x1, y1)))
            cur_x += w + gap
            row_h = max(row_h, h)
    if lay.blocks:
        xs0 = [b.ox + b.bbox[0] for b in lay.blocks]
        ys0 = [b.oy + b.bbox[1] for b in lay.blocks]
        xs1 = [b.ox + b.bbox[2] for b in lay.blocks]
        ys1 = [b.oy + b.bbox[3] for b in lay.blocks]
        lay.extents = (min(xs0) - pad, min(ys0) - pad, max(xs1) + pad, max(ys1) + pad)
    return lay
