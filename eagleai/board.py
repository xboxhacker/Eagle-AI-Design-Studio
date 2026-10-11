"""A board as the placement engine sees it: outline, elements, pads (relative to the element origin at R0, not
mirrored), and which pads share a signal. Read from a saved Eagle 7 .brd, or from the live dump studio.ulp writes.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class BPad:
    name: str
    x: float          # relative to element origin, element at R0 and not mirrored (mm)
    y: float
    w: float
    h: float
    tht: bool = False     # through-hole (both layers)


@dataclass
class BElement:
    name: str
    value: str
    package: str
    library: str
    x: float
    y: float
    angle: float = 0.0
    mirror: bool = False
    pads: List[BPad] = field(default_factory=list)
    body: Tuple[float, float, float, float] = (0, 0, 0, 0)   # relative bbox at R0 (pads + silk), mm
    locked: bool = False

    def box_at(self, x: float, y: float, angle: float, mirror: bool, margin: float = 0.0):
        x0, y0, x1, y1 = self.body
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        out = [transform(px, py, x, y, angle, mirror) for px, py in pts]
        xs, ys = [p[0] for p in out], [p[1] for p in out]
        return min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin

    def pad_at(self, pad: str, x=None, y=None, angle=None, mirror=None):
        p = next((q for q in self.pads if q.name == pad), None)
        if p is None:
            return None
        return transform(p.x, p.y, self.x if x is None else x, self.y if y is None else y,
                         self.angle if angle is None else angle, self.mirror if mirror is None else mirror)

    def centre_at(self, x, y, angle, mirror):
        x0, y0, x1, y1 = self.body
        return transform((x0 + x1) / 2, (y0 + y1) / 2, x, y, angle, mirror)


def transform(px, py, ox, oy, angle, mirror):
    if mirror:
        px = -px
    a = math.radians(angle)
    return ox + px * math.cos(a) - py * math.sin(a), oy + px * math.sin(a) + py * math.cos(a)


def untransform(ax, ay, ox, oy, angle, mirror):
    a = math.radians(-angle)
    dx, dy = ax - ox, ay - oy
    px, py = dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)
    return (-px if mirror else px), py


@dataclass
class Board:
    path: str = ""
    outline: Tuple[float, float, float, float] = (0, 0, 100, 80)
    has_outline: bool = False
    elements: Dict[str, BElement] = field(default_factory=dict)
    signals: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)   # name -> [(element, pad)]
    source: str = ""          # "brd" | "eagle"
    wires: List[Tuple[str, int, float, float, float, float, float]] = field(default_factory=list)  # net, layer, x1,y1,x2,y2, w
    vias: List[Tuple[str, float, float, float, float]] = field(default_factory=list)              # net, x, y, dia, drill
    pkg_copper: List[Tuple[str, int, float, float, float, float, float]] = field(default_factory=list)  # elem, layer, ...

    def nets_of(self, elem: str) -> Dict[str, List[str]]:
        """{signal: [pads of elem on it]}"""
        out: Dict[str, List[str]] = {}
        for s, refs in self.signals.items():
            for e, p in refs:
                if e == elem:
                    out.setdefault(s, []).append(p)
        return out


def parse_rot(rot: str) -> Tuple[float, bool]:
    rot = rot or "R0"
    m = re.search(r"R(-?[\d.]+)", rot)
    return (float(m.group(1)) % 360 if m else 0.0), ("M" in rot)


def _body_from_pads(pads: List[BPad], extra=()) -> Tuple[float, float, float, float]:
    xs = [p.x - p.w / 2 for p in pads] + [p.x + p.w / 2 for p in pads] + [e[0] for e in extra]
    ys = [p.y - p.h / 2 for p in pads] + [p.y + p.h / 2 for p in pads] + [e[1] for e in extra]
    if not xs:
        return (-0.5, -0.5, 0.5, 0.5)
    return min(xs), min(ys), max(xs), max(ys)


def read_brd(path: Path) -> Board:
    root = ET.parse(path).getroot()
    b = root.find("drawing/board")
    if b is None:
        raise ValueError(f"{Path(path).name} is not an Eagle board")
    pk: Dict[Tuple[str, str], ET.Element] = {}
    for lib in b.findall("libraries/library"):
        for p in lib.findall("packages/package"):
            pk[(lib.get("name", ""), p.get("name", ""))] = p
    board = Board(path=str(path), source="brd")
    segs = [(float(w.get("x1")), float(w.get("y1")), float(w.get("x2")), float(w.get("y2")))
            for w in b.findall("plain/wire") if w.get("layer") == "20"]
    for e in b.findall("elements/element"):
        angle, mirror = parse_rot(e.get("rot"))
        p = pk.get((e.get("library", ""), e.get("package", "")))
        pads, silk, pkg_outline, pkg_cu = [], [], [], []
        if p is not None:
            for s in p.findall("smd"):
                w, h = float(s.get("dx")), float(s.get("dy"))
                if re.search(r"R(90|270)", s.get("rot", "") or ""):
                    w, h = h, w
                pads.append(BPad(s.get("name"), float(s.get("x")), float(s.get("y")), w, h))
            for s in p.findall("pad"):
                d = float(s.get("diameter") or float(s.get("drill", 0.8)) * 1.7)
                pads.append(BPad(s.get("name"), float(s.get("x")), float(s.get("y")), d, d, True))
            for w in p.findall("wire"):
                pt = [(float(w.get("x1")), float(w.get("y1"))), (float(w.get("x2")), float(w.get("y2")))]
                if w.get("layer") in ("1", "16"):
                    pkg_cu.append((int(w.get("layer")), pt, float(w.get("width", 0.2))))
                if w.get("layer") in ("21", "51"):
                    silk += pt
                if w.get("layer") == "20":
                    pkg_outline.append(pt)
        el = BElement(e.get("name"), e.get("value", ""), e.get("package", ""), e.get("library", ""),
                      float(e.get("x", 0)), float(e.get("y", 0)), angle, mirror, pads,
                      _body_from_pads(pads, silk), e.get("locked") == "yes")
        for a, c in pkg_outline:           # outline drawn inside a package (e.g. a PCB coil) counts too
            (x1, y1), (x2, y2) = (transform(*a, el.x, el.y, angle, mirror), transform(*c, el.x, el.y, angle, mirror))
            segs.append((x1, y1, x2, y2))
        board.elements[el.name] = el
        for layer, (a, c), w in pkg_cu:
            (x1, y1), (x2, y2) = transform(*a, el.x, el.y, angle, mirror), transform(*c, el.x, el.y, angle, mirror)
            board.pkg_copper.append((el.name, (17 - layer) if mirror else layer, x1, y1, x2, y2, w))
    for s in b.findall("signals/signal"):
        refs = [(c.get("element"), c.get("pad")) for c in s.findall("contactref")]
        if refs:
            board.signals[s.get("name")] = refs
        for w in s.findall("wire"):
            if w.get("layer") in ("1", "16"):
                board.wires.append((s.get("name"), int(w.get("layer")), float(w.get("x1")), float(w.get("y1")),
                                    float(w.get("x2")), float(w.get("y2")), float(w.get("width", 0.2))))
        for v in s.findall("via"):
            drill = float(v.get("drill", 0.3))
            board.vias.append((s.get("name"), float(v.get("x")), float(v.get("y")),
                               float(v.get("diameter") or drill * 2), drill))
    if segs:
        xs = [v for s in segs for v in (s[0], s[2])]
        ys = [v for s in segs for v in (s[1], s[3])]
        board.outline, board.has_outline = (min(xs), min(ys), max(xs), max(ys)), True
    return board


def read_state(path: Path) -> Board:
    """Parse the tab-separated dump studio.ulp writes (absolute pad positions -> made relative here)."""
    board = Board(source="eagle")
    segs, pads_abs = [], {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        f = line.split("\t")
        if not f or not f[0]:
            continue
        k = f[0]
        try:
            if k == "BOARD":
                board.path = f[1]
            elif k == "O":
                segs.append(tuple(float(v) for v in f[1:5]))
            elif k == "E":
                board.elements[f[1]] = BElement(f[1], f[2], f[3], f[4], float(f[5]), float(f[6]),
                                                float(f[7]) % 360, f[8].strip() not in ("0", ""))
            elif k == "P":
                pads_abs.setdefault(f[1], []).append((f[2], float(f[3]), float(f[4]), float(f[5]), float(f[6]),
                                                      len(f) > 7 and f[7].strip() == "T"))
            elif k == "W":
                board.wires.append((f[1], int(f[2]), *(float(v) for v in f[3:8])))
            elif k == "V":
                board.vias.append((f[1], float(f[2]), float(f[3]), float(f[4]), float(f[5])))
            elif k == "PW":
                board.pkg_copper.append((f[1], int(f[2]), *(float(v) for v in f[3:8])))
            elif k == "S":
                board.signals.setdefault(f[1], []).append((f[2], f[3]))
        except (IndexError, ValueError):
            continue
    for name, el in board.elements.items():
        for pn, ax, ay, w, h, tht in pads_abs.get(name, []):
            rx, ry = untransform(ax, ay, el.x, el.y, el.angle, el.mirror)
            if abs((el.angle % 180) - 90) < 1:      # dump gives dx/dy as placed; store at R0
                w, h = h, w
            el.pads.append(BPad(pn, rx, ry, w, h, tht))
        el.body = _body_from_pads(el.pads)
    if segs:
        xs = [v for s in segs for v in (s[0], s[2])]
        ys = [v for s in segs for v in (s[1], s[3])]
        board.outline, board.has_outline = (min(xs), min(ys), max(xs), max(ys)), True
    return board
