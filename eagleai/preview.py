"""SVG preview of the schematic the .sch writer will produce.

Uses the same Layout as sch_writer, so what you see is what Eagle will show.
Eagle's y axis points up; SVG's points down, so every y is negated.
"""
from __future__ import annotations

import html
import math
from typing import Dict, List, Optional

from .layout import Layout, build_layout, pin_stub_dir, snap
from .library import Device, LibraryIndex
from .model import Design
from .sch_writer import STUB, net_nodes, resolve_devices, safe_net_name

INK = "#8fd18f"       # symbol lines
NET = "#4aa3ff"
TEXT = "#e8e8e8"
PIN = "#d9a441"
BG = "#1b1d21"
FONT = "font-family='Consolas,monospace'"


def _f(el, name, default=0.0) -> float:
    try:
        return float(el.get(name, default))
    except (TypeError, ValueError):
        return default


def _arc_path(el, ox, oy) -> Optional[str]:
    """Eagle wires with a curve attribute are arcs; approximate with an SVG arc."""
    curve = _f(el, "curve")
    if not curve:
        return None
    x1, y1, x2, y2 = _f(el, "x1"), _f(el, "y1"), _f(el, "x2"), _f(el, "y2")
    chord = math.hypot(x2 - x1, y2 - y1)
    if chord == 0:
        return None
    r = chord / (2 * math.sin(math.radians(abs(curve)) / 2))
    large = 1 if abs(curve) > 180 else 0
    sweep = 0 if curve > 0 else 1      # y is flipped, so the sense inverts
    return (f"M{ox + x1:.3f},{-(oy + y1):.3f} A{abs(r):.3f},{abs(r):.3f} 0 {large} {sweep} "
            f"{ox + x2:.3f},{-(oy + y2):.3f}")


def _symbol_svg(sym, ox: float, oy: float, ref: str, value: str, show_pin_names: bool) -> List[str]:
    out: List[str] = []
    for el in sym:
        t = el.tag
        if t == "wire":
            arc = _arc_path(el, ox, oy)
            w = max(_f(el, "width", 0.254), 0.2)
            if arc:
                out.append(f"<path d='{arc}' fill='none' stroke='{INK}' stroke-width='{w}'/>")
            else:
                out.append(f"<line x1='{ox + _f(el, 'x1'):.3f}' y1='{-(oy + _f(el, 'y1')):.3f}' "
                           f"x2='{ox + _f(el, 'x2'):.3f}' y2='{-(oy + _f(el, 'y2')):.3f}' "
                           f"stroke='{INK}' stroke-width='{w}' stroke-linecap='round'/>")
        elif t == "rectangle":
            x1, x2 = sorted((_f(el, "x1"), _f(el, "x2")))
            y1, y2 = sorted((_f(el, "y1"), _f(el, "y2")))
            out.append(f"<rect x='{ox + x1:.3f}' y='{-(oy + y2):.3f}' width='{x2 - x1:.3f}' "
                       f"height='{y2 - y1:.3f}' fill='{INK}' fill-opacity='0.25' stroke='none'/>")
        elif t == "circle":
            w = _f(el, "width", 0.254)
            out.append(f"<circle cx='{ox + _f(el, 'x'):.3f}' cy='{-(oy + _f(el, 'y')):.3f}' "
                       f"r='{_f(el, 'radius'):.3f}' fill='none' stroke='{INK}' stroke-width='{max(w, 0.2)}'/>")
        elif t == "polygon":
            pts = " ".join(f"{ox + _f(v, 'x'):.3f},{-(oy + _f(v, 'y')):.3f}" for v in el.findall("vertex"))
            if pts:
                out.append(f"<polygon points='{pts}' fill='{INK}' fill-opacity='0.4' stroke='{INK}' stroke-width='0.2'/>")
        elif t == "text":
            txt = (el.text or "").strip()
            if txt == ">NAME":
                txt = ref
            elif txt == ">VALUE":
                txt = value
            if txt:
                size = _f(el, "size", 1.778)
                out.append(f"<text x='{ox + _f(el, 'x'):.3f}' y='{-(oy + _f(el, 'y')):.3f}' "
                           f"font-size='{size:.3f}' fill='{TEXT}' {FONT}>{html.escape(txt)}</text>")
        elif t == "pin":
            px, py = ox + _f(el, "x"), oy + _f(el, "y")
            rot = el.get("rot", "R0") or "R0"
            ang = int("".join(c for c in rot if c.isdigit()) or 0) % 360
            dx, dy = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}.get(ang, (1, 0))
            ln = {"point": 0, "short": 2.54, "middle": 5.08, "long": 7.62}.get(el.get("length", "long"), 7.62)
            out.append(f"<line x1='{px:.3f}' y1='{-py:.3f}' x2='{px + dx * ln:.3f}' y2='{-(py + dy * ln):.3f}' "
                       f"stroke='{PIN}' stroke-width='0.254'/>")
            out.append(f"<circle cx='{px:.3f}' cy='{-py:.3f}' r='0.45' fill='{PIN}'/>")
            if show_pin_names and el.get("visible", "both") in ("both", "pin"):
                # name sits just inside the body end of the pin line
                tx, ty = px + dx * (ln + 0.8), py + dy * (ln + 0.8)
                anchor = "start" if dx > 0 else "end" if dx < 0 else "middle"
                out.append(f"<text x='{tx:.3f}' y='{-ty + 0.6:.3f}' font-size='1.5' fill='{TEXT}' "
                           f"text-anchor='{anchor}' {FONT}>{html.escape(el.get('name', ''))}</text>")
    return out


def render_svg(design: Design, index: LibraryIndex, show_pin_names: bool = True,
               highlight_net: Optional[str] = None) -> str:
    devs, errs = resolve_devices(design, index)
    layout: Layout = build_layout(devs)
    nodes = net_nodes(design, devs)
    x0, y0, x1, y1 = layout.extents
    w, h = max(x1 - x0, 10), max(y1 - y0, 10)
    vb = f"{x0:.2f} {-y1:.2f} {w:.2f} {h:.2f}"
    parts: List[str] = [
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='{vb}' width='100%' height='100%' "
        f"preserveAspectRatio='xMidYMid meet'>",
        f"<rect x='{x0:.2f}' y='{-y1:.2f}' width='{w:.2f}' height='{h:.2f}' fill='{BG}'/>",
    ]
    for b in layout.blocks:
        spec = design.parts[b.ref]
        sym = b.device.symbols.get(next(g.symbol for g in b.device.gates if g.name == b.gate))
        parts.append(f"<g data-ref='{b.ref}'>")
        parts += _symbol_svg(sym, b.ox, b.oy, b.ref, spec.value, show_pin_names)
        parts.append("</g>")

    for net, plist in nodes.items():
        col = "#ff6b6b" if net == highlight_net else NET
        for ref, pin in plist:
            px, py = layout.pin_point(ref, pin)
            d = pin_stub_dir(pin)
            ex, ey = snap(px + d[0] * STUB), snap(py + d[1] * STUB)
            parts.append(f"<line x1='{px}' y1='{-py}' x2='{ex}' y2='{-ey}' stroke='{col}' stroke-width='0.254'/>")
            anchor = "end" if d[0] < 0 else "start" if d[0] > 0 else "middle"
            ty = -ey + (-0.8 if d[1] > 0 else 2.2 if d[1] < 0 else 0.5)
            tx = ex + d[0] * 0.6
            parts.append(f"<text x='{tx:.3f}' y='{ty:.3f}' font-size='1.6' fill='{col}' text-anchor='{anchor}' "
                         f"{FONT}>{html.escape(safe_net_name(net))}</text>")
    for e in errs:
        parts.append(f"<text x='{x0 + 2:.2f}' y='{-y1 + 4 + 3 * errs.index(e):.2f}' font-size='2' fill='#ff6b6b' {FONT}>"
                     f"{html.escape(e[:120])}</text>")
    parts.append("</svg>")
    return "\n".join(parts)


def symbol_svg(dev: Device, gate_index: int = 0, show_pin_names: bool = True) -> str:
    """Stand-alone picture of one library symbol (used by the LBR builder review)."""
    gate = dev.gates[gate_index]
    sym = dev.symbols[gate.symbol]
    from .layout import symbol_bbox
    bx0, by0, bx1, by1 = symbol_bbox(sym)
    pad = 12.0
    w, h = bx1 - bx0 + 2 * pad, by1 - by0 + 2 * pad
    body = "\n".join(_symbol_svg(sym, 0, 0, "U?", dev.deviceset, show_pin_names))
    return (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='{bx0 - pad:.2f} {-by1 - pad:.2f} {w:.2f} {h:.2f}' "
            f"width='100%' height='100%'><rect x='{bx0 - pad:.2f}' y='{-by1 - pad:.2f}' width='{w:.2f}' "
            f"height='{h:.2f}' fill='{BG}'/>{body}</svg>")
