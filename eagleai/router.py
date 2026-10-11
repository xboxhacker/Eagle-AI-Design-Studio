"""Two-layer grid router (A*) that obeys the design rules. Code routes; the AI only decides intent
(net classes / widths from current, routing order, nets to leave for a ground pour).

Model: a grid of pitch g on Top (1) and Bottom (16). Every piece of copper marks
  core  - the cells it covers (its own net may always use them), and
  halo  - every cell whose centre is closer than (its half width + clearance + widest route half width + g/2):
          a cell is usable by net n only if its halo owner is free or n.
So any path through usable cells keeps the clearance to everything else, for every class width.
Vias need their whole pad (+ clearance) usable on both layers. Paths may run at 45 degrees.
Nets are routed shortest first; failed nets get a second pass with the blockers ripped up and re-queued.
Every result is checked again with exact geometry (check_drc) before it's offered.
"""
from __future__ import annotations

import heapq
import math
import time
from array import array
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .board import Board, transform
from .rules import DesignRules

LAYERS = (1, 16)
FREE, HARD = 0, -1


@dataclass
class Seg:
    net: str
    layer: int
    x1: float
    y1: float
    x2: float
    y2: float
    width: float


@dataclass
class Via:
    net: str
    x: float
    y: float
    dia: float
    drill: float


@dataclass
class RouteResult:
    segs: List[Seg] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)
    routed: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    skipped: List[str] = field(default_factory=list)
    drc: List[str] = field(default_factory=list)
    pour: Optional[Dict] = None
    seconds: float = 0.0
    grid: float = 0.0

    def summary(self) -> Dict:
        length = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in self.segs)
        return {"routed": len(self.routed), "failed": self.failed or "none", "skipped": self.skipped,
                "vias": len(self.vias), "trace_length_mm": round(length, 1),
                "drc": self.drc or "clean", "pour": self.pour or "none",
                "grid_mm": self.grid, "seconds": round(self.seconds, 1)}


# ------------------------------------------------------------------ geometry
def _pt_seg(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _seg_seg(a, b) -> float:
    (ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2) = a, b

    def ccw(x1, y1, x2, y2, x3, y3):
        return (x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)
    d1, d2 = ccw(bx1, by1, bx2, by2, ax1, ay1), ccw(bx1, by1, bx2, by2, ax2, ay2)
    d3, d4 = ccw(ax1, ay1, ax2, ay2, bx1, by1), ccw(ax1, ay1, ax2, ay2, bx2, by2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return 0.0
    return min(_pt_seg(ax1, ay1, bx1, by1, bx2, by2), _pt_seg(ax2, ay2, bx1, by1, bx2, by2),
               _pt_seg(bx1, by1, ax1, ay1, ax2, ay2), _pt_seg(bx2, by2, ax1, ay1, ax2, ay2))


def _pt_rect(px, py, r) -> float:
    dx = max(r[0] - px, 0, px - r[2])
    dy = max(r[1] - py, 0, py - r[3])
    return math.hypot(dx, dy)


def _seg_rect(s, r) -> float:
    x1, y1, x2, y2 = s
    if r[0] <= x1 <= r[2] and r[1] <= y1 <= r[3]:
        return 0.0
    edges = [(r[0], r[1], r[2], r[1]), (r[2], r[1], r[2], r[3]), (r[2], r[3], r[0], r[3]), (r[0], r[3], r[0], r[1])]
    return min(_seg_seg(s, e) for e in edges)


@dataclass
class Shape:
    """A copper object for the grid and for DRC: rect (pad), seg (wire, with width) or circle (via)."""
    net: str
    layers: Tuple[int, ...]
    kind: str
    g: Tuple[float, ...]       # rect: x0,y0,x1,y1 | seg: x1,y1,x2,y2,w | circ: x,y,r
    label: str = ""

    def dist(self, other: "Shape") -> float:
        """Edge-to-edge distance (negative/zero = touching)."""
        def as_core(s):
            if s.kind == "seg":
                return ("seg", s.g[:4], s.g[4] / 2)
            if s.kind == "circ":
                return ("pt", s.g[:2], s.g[2])
            return ("rect", s.g, 0.0)
        ka, ga, ra = as_core(self)
        kb, gb, rb = as_core(other)
        if ka == "rect" and kb == "rect":
            dx = max(gb[0] - ga[2], ga[0] - gb[2], 0)
            dy = max(gb[1] - ga[3], ga[1] - gb[3], 0)
            d = math.hypot(dx, dy)
        elif ka == "rect" or kb == "rect":
            rect, (k, g) = (ga, (kb, gb)) if ka == "rect" else (gb, (ka, ga))
            d = _pt_rect(*g, rect) if k == "pt" else _seg_rect(g, rect)
        elif ka == "pt" and kb == "pt":
            d = math.hypot(ga[0] - gb[0], ga[1] - gb[1])
        elif ka == "pt" or kb == "pt":
            p, s = (ga, gb) if ka == "pt" else (gb, ga)
            d = _pt_seg(p[0], p[1], *s)
        else:
            d = _seg_seg(ga, gb)
        return d - ra - rb


def board_shapes(b: Board, rules: DesignRules) -> List[Shape]:
    out: List[Shape] = []
    pad_net = {(e, p): s for s, refs in b.signals.items() for e, p in refs}
    for el in b.elements.values():
        for p in el.pads:
            cx, cy = (round(v, 4) for v in transform(p.x, p.y, el.x, el.y, el.angle, el.mirror))
            w, h = (p.h, p.w) if abs((el.angle % 180) - 90) < 1 else (p.w, p.h)
            layers = LAYERS if p.tht else ((16,) if el.mirror else (1,))
            out.append(Shape(pad_net.get((el.name, p.name), f"~{el.name}.{p.name}"), layers, "rect",
                             (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), f"{el.name}.{p.name}"))
    # package copper (e.g. a PCB coil): belongs to the net of the pad its chain touches
    for elem, layer, x1, y1, x2, y2, w in b.pkg_copper:
        out.append(Shape(f"~{elem}.cu", (layer,), "seg", (x1, y1, x2, y2, w), f"{elem} copper"))
    _assign_pkg_copper(out)
    for net, layer, x1, y1, x2, y2, w in b.wires:
        out.append(Shape(net, (layer,), "seg", (x1, y1, x2, y2, w), f"{net} trace"))
    for net, x, y, d, _dr in b.vias:
        out.append(Shape(net, LAYERS, "circ", (x, y, d / 2), f"{net} via"))
    return out


def _assign_pkg_copper(shapes: List[Shape]):
    cu = [s for s in shapes if s.label.endswith(" copper")]
    if not cu:
        return
    parent = list(range(len(cu)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    ends = {}
    for i, s in enumerate(cu):
        for pt in ((round(s.g[0], 3), round(s.g[1], 3)), (round(s.g[2], 3), round(s.g[3], 3))):
            if pt in ends:
                parent[find(i)] = find(ends[pt])
            else:
                ends[pt] = i
    pads = [s for s in shapes if s.kind == "rect"]
    group_net = {}
    for i, s in enumerate(cu):
        for p in pads:
            if set(p.layers) & set(s.layers) and s.dist(p) <= 0 and not p.net.startswith("~"):
                group_net.setdefault(find(i), p.net)
    for i, s in enumerate(cu):
        s.net = group_net.get(find(i), s.net)


# ------------------------------------------------------------------ router
class Router:
    def __init__(self, board: Board, rules: DesignRules, widths: Optional[Dict[str, float]] = None,
                 clearances: Optional[Dict[str, float]] = None, grid: Optional[float] = None):
        self.b, self.r = board, rules
        self.widths = {k: max(v, rules.min_trace) for k, v in (widths or {}).items()}
        self.clr = {k: max(v, rules.min_clearance) for k, v in (clearances or {}).items()}
        self.wmax = max([rules.trace] + list(self.widths.values()))
        self.g = grid or max(0.05, round(min(rules.trace + rules.clearance, 0.5) / 2 / 0.05) * 0.05)
        x0, y0, x1, y1 = board.outline
        self.x0, self.y0 = x0, y0
        self.nx = int(math.ceil((x1 - x0) / self.g)) + 1
        self.ny = int(math.ceil((y1 - y0) / self.g)) + 1
        n = self.nx * self.ny
        self.core = {L: array("i", [0]) * n for L in LAYERS}
        self.halo = {L: array("i", [0]) * n for L in LAYERS}
        self.net_id: Dict[str, int] = {}
        self.names: Dict[int, str] = {}
        self.shapes = board_shapes(board, rules)
        self.obj: Dict[str, List[Shape]] = {}
        for s in self.shapes:
            self._mark(s, +1)
        self._edge()
        rad = max(rules.via_dia / 2 - self.wmax / 2, 0)
        self._via_r = int(math.ceil(rad / self.g))
        self._via_offs = [dj * self.nx + di for dj in range(-self._via_r, self._via_r + 1)
                          for di in range(-self._via_r, self._via_r + 1) if di * di + dj * dj <= (self._via_r + 0.5) ** 2]
        # no vias in or next to SMD pads (solder would wick into the hole)
        self.no_via = {L: bytearray(n) for L in LAYERS}
        vr = rules.via_dia / 2 + rules.clearance
        for s in self.shapes:
            if s.kind == "rect" and len(s.layers) == 1:
                for k, _in in self._cells_near(s, vr):
                    self.no_via[s.layers[0]][k] = 1

    # -- grid helpers
    def nid(self, net: str) -> int:
        if net not in self.net_id:
            i = len(self.net_id) + 1
            self.net_id[net], self.names[i] = i, net
        return self.net_id[net]

    def xy(self, i, j):
        return self.x0 + i * self.g, self.y0 + j * self.g

    def ij(self, x, y):
        return int(round((x - self.x0) / self.g)), int(round((y - self.y0) / self.g))

    def width(self, net):
        return self.widths.get(net, self.r.trace)

    def clearance(self, net):
        return self.clr.get(net, self.r.clearance)

    def _edge(self):
        m = self.r.edge_clearance + self.wmax / 2
        k = int(math.ceil(m / self.g))
        for L in LAYERS:
            h = self.halo[L]
            for j in range(self.ny):
                for i in list(range(min(k + 1, self.nx))) + list(range(max(0, self.nx - k - 1), self.nx)):
                    h[j * self.nx + i] = HARD
            for i in range(self.nx):
                for j in list(range(min(k + 1, self.ny))) + list(range(max(0, self.ny - k - 1), self.ny)):
                    h[j * self.nx + i] = HARD

    def _cells_near(self, s: Shape, reach: float):
        """Cells whose centre is within `reach` of the shape's copper, with 'inside' flag (distance <= 0)."""
        if s.kind == "rect":
            bx = s.g
        elif s.kind == "seg":
            x1, y1, x2, y2, w = s.g
            bx = (min(x1, x2) - w / 2, min(y1, y2) - w / 2, max(x1, x2) + w / 2, max(y1, y2) + w / 2)
        else:
            x, y, r = s.g
            bx = (x - r, y - r, x + r, y + r)
        i0, j0 = self.ij(bx[0] - reach, bx[1] - reach)
        i1, j1 = self.ij(bx[2] + reach, bx[3] + reach)
        for j in range(max(0, j0 - 1), min(self.ny, j1 + 2)):
            for i in range(max(0, i0 - 1), min(self.nx, i1 + 2)):
                x, y = self.xy(i, j)
                if s.kind == "rect":
                    d = _pt_rect(x, y, s.g)
                elif s.kind == "seg":
                    d = _pt_seg(x, y, *s.g[:4]) - s.g[4] / 2
                else:
                    d = math.hypot(x - s.g[0], y - s.g[1]) - s.g[2]
                if d <= reach:
                    yield j * self.nx + i, d <= 1e-9

    def _mark(self, s: Shape, sign: int):
        n = self.nid(s.net)
        reach = self.clearance(s.net) + self.wmax / 2 + self.g * 0.5
        for L in s.layers:
            core, halo = self.core[L], self.halo[L]
            for k, inside in self._cells_near(s, reach):
                if inside and core[k] in (FREE, n):
                    core[k] = n
                if halo[k] == FREE:
                    halo[k] = n
                elif halo[k] not in (n, HARD):
                    halo[k] = HARD
        self.obj.setdefault(s.net, []).append(s)

    def usable(self, L, k, n) -> bool:
        if self.core[L][k] == n:
            return True
        h = self.halo[L][k]
        return h == FREE or h == n

    def via_ok(self, i, j, n) -> bool:
        k0 = j * self.nx + i
        if self.no_via[1][k0] or self.no_via[16][k0]:
            return False
        kk = self._via_r
        if i - kk < 0 or j - kk < 0 or i + kk >= self.nx or j + kk >= self.ny:
            return False
        c1, c2, h1, h2 = self.core[1], self.core[16], self.halo[1], self.halo[16]
        for off in self._via_offs:
            k = k0 + off
            a, b = h1[k], h2[k]
            if not ((a == 0 or a == n or c1[k] == n) and (b == 0 or b == n or c2[k] == n)):
                return False
        return True

    # -- search
    def route_net(self, net: str, deadline: float) -> Optional[str]:
        refs = self.b.signals.get(net, [])
        n = self.nid(net)
        pads = [s for s in self.obj.get(net, []) if s.kind == "rect"]
        if len(pads) < 2:
            return None
        # already connected by existing copper? (simple check: skip if net already has wires touching all pads)
        terms = []
        for p in pads:
            cells = [(L, k) for L in p.layers for k, inside in self._cells_near(p, 0) if inside and self.core[L][k] == n]
            if not cells:      # tiny pad between grid points: nearest cell
                cx, cy = (p.g[0] + p.g[2]) / 2, (p.g[1] + p.g[3]) / 2
                i, j = self.ij(cx, cy)
                cells = [(L, j * self.nx + i) for L in p.layers]
            terms.append((p, cells))
        # Prim-like: start from the first pad, connect the nearest unconnected pad each time
        tree_cells = set(terms[0][1])
        tree_pts = [((terms[0][0].g[0] + terms[0][0].g[2]) / 2, (terms[0][0].g[1] + terms[0][0].g[3]) / 2)]
        todo = terms[1:]
        new_shapes: List[Shape] = []
        while todo:
            todo.sort(key=lambda t: min(math.hypot((t[0].g[0] + t[0].g[2]) / 2 - x, (t[0].g[1] + t[0].g[3]) / 2 - y)
                                        for x, y in tree_pts))
            pad, cells = todo.pop(0)
            # first look in a window around the two ends (fast), then the whole board
            cx, cy = (pad.g[0] + pad.g[2]) / 2, (pad.g[1] + pad.g[3]) / 2
            near = min(tree_pts, key=lambda q: math.hypot(q[0] - cx, q[1] - cy))
            m = 6.0 + 0.5 * math.hypot(near[0] - cx, near[1] - cy)
            i0, j0 = self.ij(min(cx, near[0]) - m, min(cy, near[1]) - m)
            i1, j1 = self.ij(max(cx, near[0]) + m, max(cy, near[1]) + m)
            win = (max(0, i0), max(0, j0), min(self.nx - 1, i1), min(self.ny - 1, j1))
            path = self._astar(tree_cells, set(cells), n, pad, deadline, win)
            if path is None and time.time() < deadline:
                path = self._astar(tree_cells, set(cells), n, pad, deadline)
            if path is None:
                for s in new_shapes:                  # undo this net
                    self._unmark(s)
                return f"no path to {pad.label}" if time.time() < deadline else "time limit"
            shapes = self._commit(path, net, pad, tree_pts)
            new_shapes += shapes
            tree_cells |= {(L, k) for L, k in path}
            tree_pts.append(((pad.g[0] + pad.g[2]) / 2, (pad.g[1] + pad.g[3]) / 2))
            for L, k in path:
                tree_pts.append(self.xy(k % self.nx, k // self.nx))
        self.routed_shapes.setdefault(net, []).extend(new_shapes)
        return None

    def _astar(self, starts, goals, n, goal_pad, deadline, box=None):
        """A* over (layer, cell). Weighted heuristic (x1.2) for speed; via legality checked lazily on pop."""
        nx, ny = self.nx, self.ny
        N = nx * self.ny
        gx, gy = (goal_pad.g[0] + goal_pad.g[2]) / 2, (goal_pad.g[1] + goal_pad.g[3]) / 2
        gi, gj = self.ij(gx, gy)
        via_cost = max(15.0, 3.0 / self.g)          # a via "costs" ~3 mm of trace
        W = 1.0
        core = (self.core[1], self.core[16])
        halo = (self.halo[1], self.halo[16])
        goal_s = {(0 if L == 1 else N) + k for L, k in goals}
        best: Dict[int, float] = {}
        came: Dict[int, int] = {}
        heap = []
        hyp = math.hypot
        via_cache: Dict[int, bool] = {}          # the grid doesn't change during one search
        bi0, bj0, bi1, bj1 = box if box else (0, 0, nx - 1, ny - 1)
        for L, k in starts:
            st = (0 if L == 1 else N) + k
            best[st] = 0.0
            heapq.heappush(heap, (W * hyp(k % nx - gi, k // nx - gj), 0.0, st, -1, 0))
        dirs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414))
        pops = 0
        while heap:
            f, cost, st, d, via_pending = heapq.heappop(heap)
            if cost > best.get(st, 1e18):
                continue
            li = 0 if st < N else 1
            k = st - li * N
            i, j = k % nx, k // nx
            if st in goal_s:
                path = [st]
                while path[-1] in came:
                    path.append(came[path[-1]])
                return [((1 if p < N else 16), p % N) for p in reversed(path)]
            pops += 1
            if pops % 4000 == 0 and time.time() > deadline:
                return None
            cl, hl = core[li], halo[li]
            base = li * N
            for di, (ddx, ddy, c) in enumerate(dirs):
                ii, jj = i + ddx, j + ddy
                if ii < bi0 or jj < bj0 or ii > bi1 or jj > bj1:
                    continue
                kk = jj * nx + ii
                h = hl[kk]
                if not (h == 0 or h == n or cl[kk] == n):
                    continue
                if ddx and ddy:       # no corner cutting on diagonals
                    k1, k2 = j * nx + ii, jj * nx + i
                    h1, h2 = hl[k1], hl[k2]
                    if not ((h1 == 0 or h1 == n or cl[k1] == n) and (h2 == 0 or h2 == n or cl[k2] == n)):
                        continue
                pref = 1.0 if (li == 0) == (ddy == 0) else 1.25      # Top prefers x runs, Bottom y runs
                nc = cost + c * pref + (0.3 if (d != -1 and d != di) else 0.0)
                ns = base + kk
                if nc < best.get(ns, 1e18):
                    best[ns] = nc
                    came[ns] = st
                    heapq.heappush(heap, (nc + W * hyp(ii - gi, jj - gj), nc, ns, di, 0))
            oi = 1 - li
            h = halo[oi][k]
            if (h == 0 or h == n or core[oi][k] == n) and (via_cache[k] if k in via_cache else
                                                          via_cache.setdefault(k, self.via_ok(i, j, n))):
                ns = oi * N + k
                nc = cost + via_cost
                if nc < best.get(ns, 1e18):
                    best[ns] = nc
                    came[ns] = st
                    heapq.heappush(heap, (nc + W * hyp(i - gi, j - gj), nc, ns, -1, 1))
        return None

    def _commit(self, path, net, pad, tree_pts) -> List[Shape]:
        w = self.width(net)
        shapes: List[Shape] = []
        # split into same-layer runs; layer changes are vias
        runs, cur = [], [path[0]]
        for a, b in zip(path, path[1:]):
            if a[0] != b[0]:
                runs.append(cur)
                x, y = self.xy(a[1] % self.nx, a[1] // self.nx)
                shapes.append(Shape(net, LAYERS, "circ", (x, y, self.r.via_dia / 2), f"{net} via"))
                cur = [b]
            else:
                cur.append(b)
        runs.append(cur)
        for run in runs:
            L = run[0][0]
            ij = [(k % self.nx, k // self.nx) for _L, k in run]
            # collapse straight runs (on integer grid indices - exact)
            keep = [ij[0]]
            for p, q, r_ in zip(ij, ij[1:], ij[2:]):
                if (q[0] - p[0]) * (r_[1] - q[1]) - (q[1] - p[1]) * (r_[0] - q[0]) != 0 or \
                        (q[0] - p[0]) * (r_[0] - q[0]) + (q[1] - p[1]) * (r_[1] - q[1]) < 0:
                    keep.append(q)
            if len(ij) > 1:
                keep.append(ij[-1])
            keep = [self.xy(*c) for c in keep]
            for p, q in zip(keep, keep[1:]):
                if p != q:
                    shapes.append(Shape(net, (L,), "seg", (p[0], p[1], q[0], q[1], w), f"{net} trace"))
        # stubs from grid to the real pad centres at both ends
        for end, (L, k) in ((pad, path[-1]),):
            cx, cy = round((end.g[0] + end.g[2]) / 2, 4), round((end.g[1] + end.g[3]) / 2, 4)
            x, y = self.xy(k % self.nx, k // self.nx)
            if math.hypot(cx - x, cy - y) > 1e-6:
                shapes.append(Shape(net, (L,), "seg", (x, y, cx, cy, w), f"{net} trace"))
        L0, k0 = path[0]
        x, y = self.xy(k0 % self.nx, k0 // self.nx)
        start_pad = next((s for s in self.obj.get(net, []) if s.kind == "rect" and L0 in s.layers
                          and _pt_rect(x, y, s.g) <= 1e-9), None)
        if start_pad is not None:
            cx, cy = round((start_pad.g[0] + start_pad.g[2]) / 2, 4), round((start_pad.g[1] + start_pad.g[3]) / 2, 4)
            if math.hypot(cx - x, cy - y) > 1e-6:
                shapes.append(Shape(net, (L0,), "seg", (x, y, cx, cy, w), f"{net} trace"))
        for s in shapes:
            self._mark(s, +1)
        return shapes

    def _bbox(self, s: Shape, reach: float):
        if s.kind == "rect":
            b = s.g
        elif s.kind == "seg":
            x1, y1, x2, y2, w = s.g
            b = (min(x1, x2) - w / 2, min(y1, y2) - w / 2, max(x1, x2) + w / 2, max(y1, y2) + w / 2)
        else:
            b = (s.g[0] - s.g[2], s.g[1] - s.g[2], s.g[0] + s.g[2], s.g[1] + s.g[2])
        return b[0] - reach, b[1] - reach, b[2] + reach, b[3] + reach

    def _unmark(self, s: Shape):
        """Remove a shape: clear the cells it reached, then re-mark whatever else reaches them."""
        self.obj[s.net] = [o for o in self.obj.get(s.net, []) if o is not s]
        reach = self.clearance(s.net) + self.wmax / 2 + self.g * 0.5
        touched = {(L, k) for L in s.layers for k, _in in self._cells_near(s, reach)}
        for L, k in touched:
            self.core[L][k] = FREE
            self.halo[L][k] = FREE
        tb = self._bbox(s, 2 * reach + self.g)
        for objs in self.obj.values():
            for o in objs:
                ob = self._bbox(o, self.clearance(o.net) + self.wmax / 2 + self.g)
                if ob[0] <= tb[2] and tb[0] <= ob[2] and ob[1] <= tb[3] and tb[1] <= ob[3]:
                    self._remark_cells(o, touched)
        self._edge_cells(touched)

    def _edge_cells(self, touched):
        m = self.r.edge_clearance + self.wmax / 2
        k = int(math.ceil(m / self.g))
        for L, c in touched:
            i, j = c % self.nx, c // self.nx
            if i <= k or j <= k or i >= self.nx - k - 1 or j >= self.ny - k - 1:
                self.halo[L][c] = HARD

    def _remark_cells(self, s, touched):
        n = self.nid(s.net)
        reach = self.clearance(s.net) + self.wmax / 2 + self.g * 0.5
        for L in s.layers:
            for k, inside in self._cells_near(s, reach):
                if (L, k) not in touched:
                    continue
                if inside and self.core[L][k] in (FREE, n):
                    self.core[L][k] = n
                h = self.halo[L][k]
                if h == FREE:
                    self.halo[L][k] = n
                elif h not in (n, HARD):
                    self.halo[L][k] = HARD

    def run(self, order: Optional[List[str]] = None, skip: Optional[List[str]] = None,
            time_limit: float = 60.0) -> RouteResult:
        t0 = time.time()
        res = RouteResult(grid=self.g)
        self.routed_shapes: Dict[str, List[Shape]] = {}
        skip = set(skip or [])
        nets = [s for s, refs in self.b.signals.items() if len(refs) >= 2]
        already = self._complete_nets(nets)
        res.skipped = sorted(already | (skip & set(nets)))
        todo = [s for s in nets if s not in already and s not in skip]

        def span(net):
            pts = [((p.g[0] + p.g[2]) / 2, (p.g[1] + p.g[3]) / 2) for p in self.obj.get(net, []) if p.kind == "rect"]
            if not pts:
                return 0
            return (max(p[0] for p in pts) - min(p[0] for p in pts)) + (max(p[1] for p in pts) - min(p[1] for p in pts))
        first = [n for n in (order or []) if n in todo]
        todo = first + sorted([n for n in todo if n not in first], key=span)
        deadline = t0 + time_limit
        failed = {}
        for net in todo:
            why = self.route_net(net, deadline)
            if why:
                failed[net] = why
        # rip-up and retry: clear the nets crossing a failed net's area, route the failed one first, re-route them
        rounds = 0
        while failed and time.time() < deadline and rounds < 5:
            rounds += 1
            for net in list(failed):
                if net not in failed:
                    continue
                pads = [p for p in self.obj.get(net, []) if p.kind == "rect"]
                xs = [v for p in pads for v in (p.g[0], p.g[2])]
                ys = [v for p in pads for v in (p.g[1], p.g[3])]
                grow = 2.0 * rounds
                box = (min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow)
                victims = [m for m, shs in self.routed_shapes.items()
                           if any(_seg_rect(s.g[:4], box) <= 0 if s.kind == "seg" else
                                  _pt_rect(s.g[0], s.g[1], box) <= 0 for s in shs)][:4 + 2 * rounds]
                for v in victims:
                    for s in self.routed_shapes.pop(v, []):
                        self._unmark(s)
                if self.route_net(net, deadline) is None:
                    failed.pop(net)
                for v in sorted(victims, key=span):
                    why = self.route_net(v, deadline)
                    if why:
                        failed[v] = why
                    else:
                        failed.pop(v, None)
        res.failed = failed
        res.routed = sorted(self.routed_shapes)
        for net, shs in self.routed_shapes.items():
            for s in shs:
                if s.kind == "seg":
                    res.segs.append(Seg(net, s.layers[0], *s.g))
                else:
                    res.vias.append(Via(net, s.g[0], s.g[1], self.r.via_dia, self.r.via_drill))
        res.segs = split_tees(res.segs)
        res.drc = check_drc(self.shapes + [s for shs in self.routed_shapes.values() for s in shs],
                            self.r, self.b.outline, self.clr)
        res.drc += check_terminations(self.b, res)
        res.seconds = time.time() - t0
        return res

    def _complete_nets(self, nets) -> set:
        """Nets whose pads are already all joined by existing copper (left alone)."""
        done = set()
        for net in nets:
            objs = self.obj.get(net, [])
            pads = [o for o in objs if o.kind == "rect"]
            if len(pads) < 2 or len(objs) == len(pads):
                continue
            parent = list(range(len(objs)))

            def find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i
            for i in range(len(objs)):
                for j in range(i + 1, len(objs)):
                    if set(objs[i].layers) & set(objs[j].layers) and objs[i].dist(objs[j]) <= 1e-6:
                        parent[find(i)] = find(j)
            if len({find(objs.index(p)) for p in pads}) == 1:
                done.add(net)
        return done


def _r4(v: float) -> float:
    return round(v, 4)          # Eagle stores 0.1 um; 4 decimals in mm is exact for it


def split_tees(segs: List[Seg]) -> List[Seg]:
    """Eagle only joins traces at their END points. A branch that starts in the middle of another trace
    gets that trace split at the junction, so every joint is an exact endpoint-to-endpoint connection."""
    out = [Seg(s.net, s.layer, _r4(s.x1), _r4(s.y1), _r4(s.x2), _r4(s.y2), s.width) for s in segs]
    changed = True
    while changed:
        changed = False
        ends = {}
        for s in out:
            ends.setdefault((s.net, s.layer), set()).update({(s.x1, s.y1), (s.x2, s.y2)})
        for idx, s in enumerate(out):
            for (px, py) in ends[(s.net, s.layer)]:
                if (px, py) in ((s.x1, s.y1), (s.x2, s.y2)):
                    continue
                if _pt_seg(px, py, s.x1, s.y1, s.x2, s.y2) < 1e-6:
                    out[idx] = Seg(s.net, s.layer, s.x1, s.y1, px, py, s.width)
                    out.append(Seg(s.net, s.layer, px, py, s.x2, s.y2, s.width))
                    changed = True
                    break
            if changed:
                break
    seen, uniq = set(), []
    for s in out:
        key = (s.net, s.layer, s.width) + tuple(sorted(((s.x1, s.y1), (s.x2, s.y2))))
        if (s.x1, s.y1) != (s.x2, s.y2) and key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq


def check_terminations(b: Board, res: "RouteResult") -> List[str]:
    """Every pad of a routed net must have a trace ending exactly at its centre, and every trace end must meet a
    pad centre, a via centre or another trace end (no dangling ends, no ends on the middle of a trace)."""
    out = []
    ends: Dict[Tuple[str, int, float, float], int] = {}
    for s in res.segs:
        for pt in ((s.x1, s.y1), (s.x2, s.y2)):
            k = (s.net, s.layer, _r4(pt[0]), _r4(pt[1]))
            ends[k] = ends.get(k, 0) + 1
    centres = set()
    for net in res.routed:
        for e, p in b.signals.get(net, []):
            el = b.elements.get(e)
            pad = next((q for q in el.pads if q.name == p), None) if el else None
            if pad is None:
                continue
            cx, cy = (_r4(v) for v in transform(pad.x, pad.y, el.x, el.y, el.angle, el.mirror))
            layers = LAYERS if pad.tht else ((16,) if el.mirror else (1,))
            centres |= {(net, L, cx, cy) for L in layers}
            if not any((net, L, cx, cy) in ends for L in layers):
                out.append(f"{e}.{p} ({net}): no trace ends at the pad centre ({cx}, {cy})")
    vias = {(v.net, L, _r4(v.x), _r4(v.y)) for v in res.vias for L in LAYERS}
    for k, n in ends.items():
        if n == 1 and k not in centres and k not in vias:
            out.append(f"{k[0]}: trace end at ({k[2]}, {k[3]}) on layer {k[1]} connects to nothing")
    return out


def check_drc(shapes: List[Shape], rules: DesignRules, outline, clr: Optional[Dict[str, float]] = None) -> List[str]:
    """Exact-geometry clearance check between different nets, plus board edge."""
    clr = clr or {}
    out = []
    cell = 2.0
    buckets: Dict[Tuple[int, int, int], List[int]] = {}

    def bbox(s):
        if s.kind == "rect":
            return s.g
        if s.kind == "seg":
            x1, y1, x2, y2, w = s.g
            return (min(x1, x2) - w / 2, min(y1, y2) - w / 2, max(x1, x2) + w / 2, max(y1, y2) + w / 2)
        x, y, r = s.g
        return (x - r, y - r, x + r, y + r)
    boxes = [bbox(s) for s in shapes]
    for idx, (s, b) in enumerate(zip(shapes, boxes)):
        for L in s.layers:
            for i in range(int(b[0] // cell) - 1, int(b[2] // cell) + 2):
                for j in range(int(b[1] // cell) - 1, int(b[3] // cell) + 2):
                    buckets.setdefault((L, i, j), []).append(idx)
    seen = set()
    for key, idxs in buckets.items():
        for a in range(len(idxs)):
            for c in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[c]
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                s, t = shapes[i], shapes[j]
                if s.net == t.net:
                    continue
                if s.net.startswith("~") and t.net.startswith("~") and s.label.split(".")[0] == t.label.split(".")[0]:
                    continue          # unconnected pads of the same part: the footprint's business
                if "trace" not in s.label and "via" not in s.label and "trace" not in t.label and "via" not in t.label:
                    continue          # pad-to-pad comes from the footprints/placement, not routing
                need = max(clr.get(s.net, rules.clearance), clr.get(t.net, rules.clearance))
                d = s.dist(t)
                if d < need - 1e-3:
                    out.append(f"{s.label} - {t.label}: {max(d, 0):.3f} mm (needs {need:.3f})")
                    if len(out) > 40:
                        return out
    x0, y0, x1, y1 = outline
    for s, b in zip(shapes, boxes):
        if "trace" in s.label or "via" in s.label:
            d = min(b[0] - x0, b[1] - y0, x1 - b[2], y1 - b[3])
            if d < rules.edge_clearance - 1e-3:
                out.append(f"{s.label}: {max(d, 0):.3f} mm from the board edge (needs {rules.edge_clearance})")
    return out


def route(board: Board, rules: DesignRules, widths=None, clearances=None, order=None, skip=None,
          grid=None, time_limit: float = 60.0) -> RouteResult:
    return Router(board, rules, widths, clearances, grid).run(order, skip, time_limit)


# ------------------------------------------------------------------ output
def _q(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def eagle_script(res: RouteResult, rules: DesignRules) -> str:
    out = ["GRID MM;", "SET WIRE_BEND 2;", f"CHANGE DRILL {rules.via_drill:g};"]
    for L in LAYERS:
        segs = [s for s in res.segs if s.layer == L]
        if not segs:
            continue
        out.append(f"LAYER {L};")
        for s in segs:
            out.append(f"WIRE {_q(s.net)} {s.width:g} ({s.x1:.4f} {s.y1:.4f}) ({s.x2:.4f} {s.y2:.4f});")
    for v in res.vias:
        out.append(f"VIA {_q(v.net)} {v.dia:g} round ({v.x:.4f} {v.y:.4f});")
    if res.pour:
        p = res.pour
        out.append(f"CHANGE ISOLATE {p['isolate']:g};")
        for L in p["layers"]:
            x0, y0, x1, y1 = p["box"]
            out.append(f"LAYER {L};")
            out.append(f"POLYGON {_q(p['net'])} 0.3 ({x0:.4f} {y0:.4f}) ({x1:.4f} {y0:.4f}) ({x1:.4f} {y1:.4f}) "
                       f"({x0:.4f} {y1:.4f}) ({x0:.4f} {y0:.4f});")
    out += ["RATSNEST;", "GRID LAST;"]
    return "\n".join(out) + "\n"


def write_brd(path, res: RouteResult, rules: DesignRules, classes: Optional[Dict[str, Tuple[float, float]]] = None):
    """Add the traces/vias (and pour) to a saved .brd; also store the design rules in it. Returns backup path."""
    import shutil
    import xml.etree.ElementTree as ET
    from pathlib import Path
    from .rules import apply_to_brd_xml
    path = Path(path)
    backup = path.with_name(f"{path.stem}.before_routing_{time.strftime('%Y%m%d_%H%M%S')}{path.suffix}")
    shutil.copy2(path, backup)
    raw = path.read_bytes()
    root = ET.fromstring(raw)
    apply_to_brd_xml(root, rules)
    b = root.find("drawing/board")
    sigs = {s.get("name"): s for s in b.findall("signals/signal")}
    for s in res.segs:
        if s.net in sigs:
            ET.SubElement(sigs[s.net], "wire", x1=f"{s.x1:.4f}", y1=f"{s.y1:.4f}", x2=f"{s.x2:.4f}", y2=f"{s.y2:.4f}",
                          width=f"{s.width:g}", layer=str(s.layer))
    for v in res.vias:
        if v.net in sigs:
            ET.SubElement(sigs[v.net], "via", x=f"{v.x:.4f}", y=f"{v.y:.4f}", extent="1-16",
                          drill=f"{v.drill:g}", diameter=f"{v.dia:g}")
    if res.pour and res.pour["net"] in sigs:
        x0, y0, x1, y1 = res.pour["box"]
        for L in res.pour["layers"]:
            pg = ET.SubElement(sigs[res.pour["net"]], "polygon", width="0.3", layer=str(L),
                               isolate=f"{res.pour['isolate']:g}")
            for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
                ET.SubElement(pg, "vertex", x=f"{x:.4f}", y=f"{y:.4f}")
    head = raw[:raw.find(b"<eagle")] if b"<eagle" in raw else b'<?xml version="1.0" encoding="utf-8"?>\n'
    with open(path, "wb") as f:
        f.write(head)
        ET.ElementTree(root).write(f, encoding="utf-8", xml_declaration=False)
    return backup


def preview_svg(board: Board, res: RouteResult) -> bytes:
    from .placement import PlacementResult, preview_svg as base
    svg = base(board, PlacementResult()).decode()
    extra = []
    for s in sorted(res.segs, key=lambda s: -s.layer):        # bottom first
        col, op = ("#4f86e0", "0.75") if s.layer == 16 else ("#e05252", "0.9")
        extra.append(f"<line x1='{s.x1:.3f}' y1='{-s.y1:.3f}' x2='{s.x2:.3f}' y2='{-s.y2:.3f}' stroke='{col}' "
                     f"stroke-opacity='{op}' stroke-width='{s.width:g}' stroke-linecap='round'/>")
    for v in res.vias:
        extra.append(f"<circle cx='{v.x:.3f}' cy='{-v.y:.3f}' r='{v.dia / 2:g}' fill='#c9a227'/>"
                     f"<circle cx='{v.x:.3f}' cy='{-v.y:.3f}' r='{v.drill / 2:g}' fill='#15171c'/>")
    return svg.replace("</svg>", "".join(extra) + "</svg>").encode()
