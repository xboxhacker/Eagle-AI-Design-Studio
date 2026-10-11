"""Component placement: rules (from the AI / datasheet layout guidelines) -> positions, computed in code.

The AI never invents coordinates. It states intent ("C3 within 2 mm of U1 pin VIN", "J1 on the left edge",
"keep L1 where it is") and this module searches positions/rotations that satisfy the rules, keep parts inside
the board outline and apart from each other, and keep connected pads short (ratsnest length).

Result -> an Eagle script (MOVE/ROTATE/MIRROR, run in the open board through the Eagle link) or, with Eagle
closed, a rewritten .brd (backup kept).
"""
from __future__ import annotations

import math
import re
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .board import BElement, Board, transform

ANGLES = (0.0, 90.0, 180.0, 270.0)


@dataclass
class Placed:
    ref: str
    x: float
    y: float
    angle: float
    mirror: bool
    moved: bool = True
    note: str = ""


@dataclass
class PlacementResult:
    placed: Dict[str, Placed] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    unplaced: List[str] = field(default_factory=list)
    ratsnest_before: float = 0.0
    ratsnest_after: float = 0.0

    def summary(self) -> Dict:
        moved = [p for p in self.placed.values() if p.moved]
        return {"moved": len(moved), "fixed": len(self.placed) - len(moved), "unplaced": self.unplaced,
                "ratsnest_mm": [round(self.ratsnest_before, 1), round(self.ratsnest_after, 1)],
                "positions": "; ".join(f"{p.ref}@({p.x:g},{p.y:g}) {'M' if p.mirror else ''}R{p.angle:g}"
                                       + (f" [{p.note}]" if p.note else "") for p in moved[:60]),
                "warnings": self.warnings or "none"}


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _inside(box, area) -> bool:
    return box[0] >= area[0] and box[1] >= area[1] and box[2] <= area[2] and box[3] <= area[3]


def _snap(v: float, g: float = 0.1) -> float:
    return round(round(v / g) * g, 4)


def ratsnest(board: Board, pos: Dict[str, Tuple[float, float, float, bool]]) -> float:
    """Sum over signals of a minimum-spanning-tree length between the signal's pads (mm)."""
    total = 0.0
    for refs in board.signals.values():
        pts = []
        for e, p in refs:
            el = board.elements.get(e)
            if el is None:
                continue
            x, y, a, m = pos.get(e, (el.x, el.y, el.angle, el.mirror))
            q = el.pad_at(p, x, y, a, m)
            if q:
                pts.append(q)
        if len(pts) < 2:
            continue
        inside, rest = [pts[0]], pts[1:]
        while rest:
            d, j = min((min(math.hypot(r[0] - t[0], r[1] - t[1]) for t in inside), j) for j, r in enumerate(rest))
            total += d
            inside.append(rest.pop(j))
    return total


class Placer:
    def __init__(self, board: Board, rules: Dict, pin_to_pads: Optional[Callable[[str, str], List[str]]] = None):
        self.b = board
        self.rules = rules or {}
        self.margin = float(self.rules.get("clearance", 1.0))   # room between parts for traces
        self.edge = float(self.rules.get("edge_clearance", 1.0))
        self.default_side = self.rules.get("side", "top")
        self.pin_to_pads = pin_to_pads or (lambda ref, pin: [pin])
        self.by_ref: Dict[str, List[Dict]] = {}
        for r in self.rules.get("rules", []):
            self.by_ref.setdefault(str(r.get("ref", "")).upper(), []).append(r)
        self.res = PlacementResult()
        self.pos: Dict[str, Tuple[float, float, float, bool]] = {}
        self.boxes: Dict[str, Tuple[Tuple, bool]] = {}

    # ---------------------------------------------------------------- helpers
    def _pads_for(self, ref: str, pin: str) -> List[str]:
        el = self.b.elements.get(ref)
        if el is None:
            return []
        names = {p.name for p in el.pads}
        if pin in names:
            return [pin]
        return [p for p in self.pin_to_pads(ref, pin) if p in names]

    def _fix(self, ref: str, note: str = ""):
        el = self.b.elements[ref]
        self.pos[ref] = (el.x, el.y, el.angle, el.mirror)
        self.boxes[ref] = (el.box_at(el.x, el.y, el.angle, el.mirror, self.margin / 2), el.mirror)
        self.res.placed[ref] = Placed(ref, el.x, el.y, el.angle, el.mirror, moved=False, note=note)

    def _area(self):
        x0, y0, x1, y1 = self.b.outline
        return x0 + self.edge, y0 + self.edge, x1 - self.edge, y1 - self.edge

    def _free(self, ref, box, mirror) -> bool:
        for other, (ob, om) in self.boxes.items():
            if other != ref and om == mirror and _overlap(box, ob):
                return False
        return True

    def _wire_cost(self, el: BElement, x, y, a, m) -> float:
        cost = 0.0
        for sig, pads in self.b.nets_of(el.name).items():
            others = []
            for e, p in self.b.signals[sig]:
                if e != el.name and e in self.pos:
                    oe = self.b.elements[e]
                    q = oe.pad_at(p, *self.pos[e])
                    if q:
                        others.append(q)
            if not others:
                continue
            for p in pads:
                q = el.pad_at(p, x, y, a, m)
                if q:
                    cost += min(math.hypot(q[0] - o[0], q[1] - o[1]) for o in others)
        return cost

    def _targets(self, ref: str):
        """[(target_xy, own_pads, max_dist, label)] from 'near' rules whose target is already placed."""
        out = []
        for r in self.by_ref.get(ref, []):
            if not r.get("near"):
                continue
            t = str(r["near"]).upper()
            if t not in self.pos:
                continue
            tel = self.b.elements[t]
            tpads = self._pads_for(t, str(r.get("pin", ""))) if r.get("pin") else [p.name for p in tel.pads]
            tpts = [tel.pad_at(p, *self.pos[t]) for p in tpads]
            tpts = [q for q in tpts if q]
            if not tpts:
                continue
            own = self._pads_for(ref, str(r.get("own_pin", ""))) if r.get("own_pin") else []
            if not own:      # the pads of this part that share a signal with the target pads
                shared = {s for s, refs in self.b.signals.items() for e, p in refs if e == t and p in tpads}
                own = [p for s, refs in self.b.signals.items() if s in shared for e, p in refs if e == ref]
            out.append((tpts, own, float(r.get("max_dist", 3.0)), f"{t}.{r.get('pin', '')}"))
        return out

    # ---------------------------------------------------------------- placing one part
    def place(self, ref: str):
        el = self.b.elements[ref]
        rules = self.by_ref.get(ref, [])
        side = next((r["side"] for r in rules if r.get("side")), None) or self.default_side
        mirror = side == "bottom"
        angles = [float(r["angle"]) % 360 for r in rules if r.get("angle") is not None] or list(ANGLES)
        targets = self._targets(ref)
        area = self._area()
        # parts that must hug a pin (decoupling, bootstrap...) may sit closer than the general spacing
        m_half = min(self.margin, 0.5) / 2 if targets else self.margin / 2
        at = next((r["at"] for r in rules if r.get("at")), None)
        edge = next((r["edge"] for r in rules if r.get("edge")), None)
        if at:
            seeds = [(float(at[0]), float(at[1]))]
        elif targets:
            seeds = [t for tg in targets for t in tg[0]]
        else:
            linked = []
            for sig in self.b.nets_of(ref):
                for e, p in self.b.signals[sig]:
                    if e != ref and e in self.pos:
                        q = self.b.elements[e].pad_at(p, *self.pos[e])
                        if q:
                            linked.append(q)
            if linked:
                seeds = [(sum(q[0] for q in linked) / len(linked), sum(q[1] for q in linked) / len(linked))]
            else:
                seeds = [((area[0] + area[2]) / 2, (area[1] + area[3]) / 2)]
        if edge:
            cx, cy = seeds[0]
            ex = {"left": area[0], "right": area[2]}.get(edge)
            ey = {"bottom": area[1], "top": area[3]}.get(edge)
            seeds = [(ex if ex is not None else cx, ey if ey is not None else cy)]
        best, found = None, 0
        maxr = math.hypot(area[2] - area[0], area[3] - area[1])
        r, step = 0.0, 0.5
        while r <= maxr and (found < 40 or best is None):
            n = 1 if r == 0 else max(8, int(2 * math.pi * r / step))
            for sx, sy in seeds:
                for k in range(n):
                    t = 2 * math.pi * k / n
                    for a in angles:
                        # aim the part's body centre at the candidate point
                        c0 = el.centre_at(0, 0, a, mirror)
                        x, y = _snap(sx + r * math.cos(t) - c0[0]), _snap(sy + r * math.sin(t) - c0[1])
                        box = el.box_at(x, y, a, mirror, m_half)
                        if edge:
                            if edge == "left":
                                x = _snap(x + (area[0] - box[0]))
                            elif edge == "right":
                                x = _snap(x + (area[2] - box[2]))
                            elif edge == "bottom":
                                y = _snap(y + (area[1] - box[1]))
                            elif edge == "top":
                                y = _snap(y + (area[3] - box[3]))
                            box = el.box_at(x, y, a, mirror, m_half)
                        if not _inside(box, area) or not self._free(ref, box, mirror):
                            continue
                        cost = self._wire_cost(el, x, y, a, mirror)
                        penalty, worst = 0.0, None
                        for tpts, own, md, label in targets:
                            pts = [el.pad_at(p, x, y, a, mirror) for p in own] or [el.centre_at(x, y, a, mirror)]
                            d = min(math.hypot(p[0] - q[0], p[1] - q[1]) for p in pts if p for q in tpts)
                            penalty += 5 * d + (1000 * (d - md) if d > md else 0)
                            worst = max(worst or 0, d - md)
                        if at:
                            penalty += 10 * math.hypot(x - float(at[0]), y - float(at[1]))
                        score = cost + penalty
                        found += 1
                        if best is None or score < best[0]:
                            best = (score, x, y, a, box, worst)
            r += step if r < 10 else step * 2
        if best is None:
            self.res.unplaced.append(ref)
            self.res.warnings.append(f"{ref}: no free space on the {side} side - board too small or too crowded")
            return
        _s, x, y, a, box, worst = best
        self.pos[ref] = (x, y, a, mirror)
        self.boxes[ref] = (box, mirror)
        note = ""
        if worst is not None and worst > 0:
            self.res.warnings.append(f"{ref}: could only get {worst:.1f} mm further than the rule allows")
            note = "rule not met"
        self.res.placed[ref] = Placed(ref, x, y, a, mirror, True, note)

    # ---------------------------------------------------------------- order
    def run(self) -> PlacementResult:
        els = self.b.elements
        before = {k: (e.x, e.y, e.angle, e.mirror) for k, e in els.items()}
        only = {r.upper() for r in self.rules.get("move_only", [])}
        keep = {r.upper() for r in self.rules.get("keep", [])}
        for ref, rl in self.by_ref.items():
            if any(r.get("fixed") or r.get("keep") for r in rl):
                keep.add(ref)
        movable = []
        for ref, el in els.items():
            if not el.pads:
                continue
            board_shaped = el.body[2] - el.body[0] > 0.6 * (self.b.outline[2] - self.b.outline[0])
            if el.locked or ref.upper() in keep or (only and ref.upper() not in only) or board_shaped:
                self._fix(ref, "locked" if el.locked else "kept")
            else:
                movable.append(ref)
        if not self.b.has_outline:
            self.res.warnings.append("the board has no outline (Dimension layer) - using the area around the parts")
            xs = [v for e in els.values() for v in (e.x - 20, e.x + 20)] or [0, 100]
            ys = [v for e in els.values() for v in (e.y - 20, e.y + 20)] or [0, 80]
            self.b.outline = (min(xs), min(ys), max(xs), max(ys))
        ruled_near = {r for r, rl in self.by_ref.items() if any(x.get("near") for x in rl)}
        targets = {str(x.get("near")).upper() for rl in self.by_ref.values() for x in rl if x.get("near")}
        conn = {r: len(self.b.nets_of(r)) for r in movable}
        first = [r for r in movable if any(x.get("at") or x.get("edge") for x in self.by_ref.get(r.upper(), []))]
        anchors = sorted([r for r in movable if r not in first and (r.upper() in targets and r.upper() not in ruled_near)],
                         key=lambda r: -len(els[r].pads))
        if not anchors and not first and movable and not self.pos:
            anchors = [max(movable, key=lambda r: (len(els[r].pads), conn[r]))]
        near = sorted([r for r in movable if r.upper() in ruled_near and r not in first + anchors],
                      key=lambda r: min(float(x.get("max_dist", 3)) for x in self.by_ref[r.upper()] if x.get("near")))
        rest = [r for r in movable if r not in first + anchors + near]
        order = first + anchors + near
        for ref in order:
            self.place(ref)
        while rest:       # greedy: most-connected-to-already-placed first
            rest.sort(key=lambda r: -sum(1 for s in self.b.nets_of(r) for e, _p in self.b.signals[s] if e in self.pos))
            self.place(rest.pop(0))
        # near-rules whose target was placed later: one more pass
        for ref in near:
            if ref in self.res.placed and self.res.placed[ref].note == "rule not met":
                del self.pos[ref]; del self.boxes[ref]
                self.res.warnings = [w for w in self.res.warnings if not w.startswith(ref + ":")]
                self.place(ref)
        self.res.ratsnest_before = ratsnest(self.b, before)
        self.res.ratsnest_after = ratsnest(self.b, self.pos)
        return self.res


def place(board: Board, rules: Dict, pin_to_pads=None) -> PlacementResult:
    return Placer(board, rules, pin_to_pads).run()


# -------------------------------------------------------------------- output
def _q(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def eagle_script(board: Board, res: PlacementResult) -> str:
    lines = ["GRID MM;"]
    for p in res.placed.values():
        if not p.moved:
            continue
        el = board.elements[p.ref]
        if p.mirror != el.mirror:
            lines.append(f"MIRROR {_q(p.ref)};")
        lines.append(f"ROTATE ={'M' if p.mirror else ''}R{p.angle:g} {_q(p.ref)};")
        lines.append(f"MOVE {_q(p.ref)} ({p.x:.4f} {p.y:.4f});")
    lines += ["RATSNEST;", "GRID LAST;"]
    return "\n".join(lines) + "\n"


def write_brd(path: Path, board: Board, res: PlacementResult) -> Tuple[Path, List[str]]:
    """Rewrite element positions in a saved .brd (Eagle must be closed). Returns (backup, warnings)."""
    path = Path(path)
    backup = path.with_name(f"{path.stem}.before_placement_{time.strftime('%Y%m%d_%H%M%S')}{path.suffix}")
    shutil.copy2(path, backup)
    text = path.read_bytes()
    tree = ET.ElementTree(ET.fromstring(text))
    root = tree.getroot()
    b = root.find("drawing/board")
    warns = []
    routed = {s.get("name") for s in b.findall("signals/signal") if s.find("wire") is not None}
    for e in b.findall("elements/element"):
        p = res.placed.get(e.get("name"))
        if not p or not p.moved:
            continue
        dx, dy = p.x - float(e.get("x", 0)), p.y - float(e.get("y", 0))
        e.set("x", f"{p.x:.4f}"); e.set("y", f"{p.y:.4f}")
        rot = f"{'M' if p.mirror else ''}R{p.angle:g}"
        if rot == "R0":
            e.attrib.pop("rot", None)
        else:
            e.set("rot", rot)
        for a in e.findall("attribute"):           # smashed NAME/VALUE texts travel with the part
            if a.get("x") is not None:
                a.set("x", f"{float(a.get('x')) + dx:.4f}"); a.set("y", f"{float(a.get('y')) + dy:.4f}")
        hit = [s for s, refs in board.signals.items() if s in routed and any(r[0] == p.ref for r in refs)]
        if hit:
            warns.append(f"{p.ref} had routed traces ({', '.join(hit[:4])}) - they no longer reach its pads")
    head = text[:text.find(b"<eagle")] if b"<eagle" in text else b'<?xml version="1.0" encoding="utf-8"?>\n'
    with open(path, "wb") as f:
        f.write(head)
        tree.write(f, encoding="utf-8", xml_declaration=False)
    return backup, warns


def preview_svg(board: Board, res: PlacementResult) -> bytes:
    x0, y0, x1, y1 = board.outline
    pad = 3
    out = [f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='{x0 - pad} {-y1 - pad} {x1 - x0 + 2 * pad} {y1 - y0 + 2 * pad}'>",
           f"<rect x='{x0 - pad}' y='{-y1 - pad}' width='{x1 - x0 + 2 * pad}' height='{y1 - y0 + 2 * pad}' fill='#15171c'/>",
           f"<rect x='{x0}' y='{-y1}' width='{x1 - x0}' height='{y1 - y0}' fill='none' stroke='#d8d8d8' stroke-width='0.2'/>"]
    pos = {}
    for ref, el in board.elements.items():
        p = res.placed.get(ref)
        pos[ref] = (p.x, p.y, p.angle, p.mirror) if p else (el.x, el.y, el.angle, el.mirror)
    # ratsnest (yellow)
    for refs in board.signals.values():
        pts = []
        for e, pn in refs:
            el = board.elements.get(e)
            q = el.pad_at(pn, *pos[e]) if el else None
            if q:
                pts.append(q)
        for a, b in zip(pts, pts[1:]):
            out.append(f"<line x1='{a[0]:.2f}' y1='{-a[1]:.2f}' x2='{b[0]:.2f}' y2='{-b[1]:.2f}' stroke='#e3c341' "
                       f"stroke-width='0.12' opacity='0.7'/>")
    fs = max(0.8, min(2.5, (x1 - x0) / 60))
    for ref, el in board.elements.items():
        x, y, a, m = pos[ref]
        bx = el.box_at(x, y, a, m)
        p = res.placed.get(ref)
        col = "#4f86e0" if m else "#e05252"
        dash = "" if (p and p.moved) else " stroke-dasharray='0.6 0.4'"
        out.append(f"<rect x='{bx[0]:.2f}' y='{-bx[3]:.2f}' width='{bx[2] - bx[0]:.2f}' height='{bx[3] - bx[1]:.2f}' "
                   f"fill='{col}' fill-opacity='0.18' stroke='{col}' stroke-width='0.15'{dash}/>")
        for bp in el.pads:
            q = transform(bp.x, bp.y, x, y, a, m)
            w, h = (bp.h, bp.w) if abs((a % 180) - 90) < 1 else (bp.w, bp.h)
            out.append(f"<rect x='{q[0] - w / 2:.2f}' y='{-q[1] - h / 2:.2f}' width='{w:.2f}' height='{h:.2f}' fill='#3fb950'/>")
        out.append(f"<text x='{(bx[0] + bx[2]) / 2:.2f}' y='{-(bx[1] + bx[3]) / 2:.2f}' font-size='{fs:.2f}' "
                   f"fill='#f2f4f8' text-anchor='middle' dominant-baseline='middle' font-family='Arial'>{ref}</text>")
    out.append("</svg>")
    return "".join(out).encode()
