"""PCB-trace coils (racetrack / round spirals) as Eagle 7.4 library parts - exact geometry from code.

The AI (or the Coil dialog) only picks the numbers; this module draws the copper, so the spiral is always
continuous, evenly spaced and DRC-clean. Arcs are real Eagle arcs (wire curve=...), not hundreds of segments.

Geometry (mm, part origin = board centre, x along the length):
  pitch p = trace + gap.  Turn i has half-height R_i = R_0 - i*p and arc centres at x = +/-a.
  Each turn: right half-circle about (a, 0)  ->  bottom straight  ->  left half-circle about (-a, -p/2)
  with radius R_i - p/2, which lands exactly one pitch further in. Every pair of neighbouring traces is
  therefore exactly `gap` apart, all the way round - no jogs, no diagonals.
  A round coil is the same with a = 0.
  The spiral runs clockwise inward on Top and ends at the centre on a through-hole pad (Eagle 7 packages
  can't hold vias). With two layers the Bottom spiral is the Top spiral mirrored left/right, run outward,
  so the current turns the same way on both layers and the two layers add (series). The centre pad is the
  centre tap (CT).
  Terminals A (Top, outer) and B (Bottom, outer; or the centre pad on a one-layer coil) sit just outside the
  outer turn on through-hole pads.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict, List, Tuple

from .lbr_builder import SpecError

MU0 = 4e-7 * math.pi
RHO_CU = 1.72e-8          # ohm*m at 20 C
OZ_UM = 34.8              # 1 oz copper = 34.8 um


@dataclass
class CoilParams:
    name: str = "COIL"
    shape: str = "racetrack"          # racetrack | round
    length: float = 317.5             # board length (x), mm   (round: ignored)
    width: float = 38.1               # board width (y) or diameter, mm
    corner_radius: float = 2.0        # board outline corner radius (racetrack)
    clearance: float = 3.0            # board edge -> outer edge of the outer trace, mm (room for the terminal pads)
    trace: float = 1.0
    gap: float = 0.2
    turns: int = 5                    # turns per layer
    layers: str = "both"              # top | both
    center_tap: bool = True           # 'both': make the centre pad a pin (CT)
    start_angle: float = 30.0         # where the outer terminal sits on the right-hand arc, degrees from the top
    pad_dia: float = 2.0
    pad_drill: float = 1.0
    center_dia: float = 1.6
    center_drill: float = 0.8
    outline: bool = True              # draw the board outline on Dimension (20)
    copper_oz: float = 1.0


def _wire(x1, y1, x2, y2, w, layer, curve=0.0) -> Dict:
    g = {"type": "wire", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "width": w, "layer": layer}
    if curve:
        g["curve"] = curve
    return g


def _len(seg: Dict) -> float:
    d = math.hypot(seg["x2"] - seg["x1"], seg["y2"] - seg["y1"])
    c = abs(seg.get("curve", 0))
    if not c:
        return d
    th = math.radians(c)
    return th * (d / 2) / math.sin(th / 2)


def check(cp: CoilParams) -> List[str]:
    """Hard problems as messages (empty = OK)."""
    err = []
    if cp.shape not in ("racetrack", "round"):
        err.append("shape must be racetrack or round")
    if cp.layers not in ("top", "both"):
        err.append("layers must be top or both")
    if cp.trace <= 0 or cp.gap <= 0 or cp.turns < 1:
        err.append("trace, gap must be > 0 and turns >= 1")
    if cp.shape == "racetrack" and cp.length < cp.width:
        err.append("racetrack length must be >= width")
    if err:
        return err
    p = cp.trace + cp.gap
    R0 = cp.width / 2 - cp.clearance - cp.trace / 2
    Rn = R0 - cp.turns * p
    need_c = cp.gap + cp.pad_dia + 0.3
    if cp.clearance < need_c:
        err.append(f"clearance {cp.clearance} mm is too small for the {cp.pad_dia} mm terminal pads outside the "
                   f"coil - use at least {need_c:.2f} mm (or a smaller pad)")
    if cp.pad_drill >= cp.pad_dia or cp.center_drill >= cp.center_dia:
        err.append("pad drill must be smaller than the pad")
    min_rn = cp.center_dia / 2 + cp.trace / 2 + cp.gap
    if Rn < min_rn:
        max_turns = int((R0 - min_rn) / p)
        err.append(f"{cp.turns} turns don't fit: the centre would be {2 * max(Rn, 0):.2f} mm across "
                   f"(need {2 * min_rn:.2f} for the centre pad). Max {max(max_turns, 0)} turns with these sizes.")
    if not 0 <= cp.start_angle <= 150:
        err.append("start_angle must be 0..150 degrees")
    return err


def spiral(cp: CoilParams) -> Tuple[List[Dict], Tuple[float, float], Dict]:
    """Top-layer spiral as wire dicts (layer 1), outer start point, info."""
    p = cp.trace + cp.gap
    w = cp.trace
    R0 = cp.width / 2 - cp.clearance - w / 2
    a = 0.0 if cp.shape == "round" else (cp.length / 2 - cp.clearance - w / 2) - R0
    segs: List[Dict] = []
    phi = math.radians(cp.start_angle)
    start = (a + R0 * math.sin(phi), R0 * math.cos(phi))
    for i in range(cp.turns):
        R, Rnext = R0 - i * p, R0 - (i + 1) * p
        if i == 0:
            segs.append(_wire(start[0], start[1], a, -R, w, 1, -(180 - cp.start_angle)))
        else:
            segs.append(_wire(a, R, a, -R, w, 1, -180))                    # right half circle
        if a > 0:
            segs.append(_wire(a, -R, -a, -R, w, 1))                         # bottom straight
        segs.append(_wire(-a, -R, -a, Rnext, w, 1, -180))                   # left half circle, one pitch in
        x_end = a if i < cp.turns - 1 else 0.0
        if x_end > -a:
            segs.append(_wire(-a, Rnext, x_end, Rnext, w, 1))               # top straight
    Rn = R0 - cp.turns * p
    segs.append(_wire(0.0, Rn, 0.0, 0.0, w, 1))                             # lead to the centre pad
    return segs, start, {"R0": R0, "a": a, "Rn": Rn, "pitch": p}


def mirror(segs: List[Dict], layer: int) -> List[Dict]:
    """Mirror left/right onto another layer. Mirroring reverses the arc direction, so curve changes sign."""
    out = []
    for s in segs:
        m = dict(s, x1=-s["x1"], x2=-s["x2"], layer=layer)
        if s.get("curve"):
            m["curve"] = -s["curve"]
        out.append(m)
    return out


def inductance_round_uH(n: int, d_out_mm: float, d_in_mm: float) -> float:
    """Modified Wheeler / current-sheet estimate for a flat round spiral (Mohan 1999), one layer."""
    d_out, d_in = d_out_mm / 1000, d_in_mm / 1000
    d_avg = (d_out + d_in) / 2
    rho = (d_out - d_in) / (d_out + d_in)
    return MU0 * n * n * d_avg / 2 * (math.log(2.46 / rho) + 0.20 * rho * rho) * 1e6


def build_spec(cp: CoilParams) -> Tuple[Dict, Dict]:
    errs = check(cp)
    if errs:
        raise SpecError("; ".join(errs))
    top, start, info = spiral(cp)
    graphics = list(top)
    bottom = mirror(top, 16) if cp.layers == "both" else []
    graphics += bottom
    p, R0 = info["pitch"], info["R0"]
    phi = math.radians(cp.start_angle)
    # terminal pads just outside the outer turn, radially out from the start point
    r_pad = R0 + cp.trace / 2 + cp.gap + cp.pad_dia / 2
    ax, ay = info["a"] + r_pad * math.sin(phi), r_pad * math.cos(phi)
    graphics.append(_wire(start[0], start[1], ax, ay, cp.trace, 1))
    pads = [{"name": "A", "x": round(ax, 4), "y": round(ay, 4), "type": "tht", "drill": cp.pad_drill,
             "dia": cp.pad_dia, "shape": "round"},
            {"name": "CT" if cp.layers == "both" else "B", "x": 0.0, "y": 0.0, "type": "tht",
             "drill": cp.center_drill, "dia": cp.center_dia, "shape": "round"}]
    if cp.layers == "both":
        graphics.append(_wire(-start[0], start[1], -ax, ay, cp.trace, 16))
        pads.append({"name": "B", "x": round(-ax, 4), "y": round(ay, 4), "type": "tht", "drill": cp.pad_drill,
                     "dia": cp.pad_dia, "shape": "round"})
    # board outline on Dimension
    if cp.outline:
        if cp.shape == "round":
            graphics.append({"type": "circle", "x": 0, "y": 0, "radius": cp.width / 2, "width": 0.1, "layer": 20})
        else:
            hx, hy, r = cp.length / 2, cp.width / 2, max(0.0, min(cp.corner_radius, cp.width / 2))
            graphics += [_wire(-hx + r, -hy, hx - r, -hy, 0.1, 20), _wire(hx, -hy + r, hx, hy - r, 0.1, 20),
                         _wire(hx - r, hy, -hx + r, hy, 0.1, 20), _wire(-hx, hy - r, -hx, -hy + r, 0.1, 20)]
            if r > 0:
                graphics += [_wire(hx - r, -hy, hx, -hy + r, 0.1, 20, 90), _wire(hx, hy - r, hx - r, hy, 0.1, 20, 90),
                             _wire(-hx + r, hy, -hx, hy - r, 0.1, 20, 90), _wire(-hx, -hy + r, -hx + r, -hy, 0.1, 20, 90)]
    # numbers for the user
    length_mm = sum(_len(s) for s in top)
    t_m = cp.copper_oz * OZ_UM * 1e-6
    r_layer = RHO_CU * (length_mm / 1000) / (cp.trace / 1000 * t_m)
    stats = {"pitch_mm": round(p, 3), "trace_length_per_layer_mm": round(length_mm, 1),
             "dc_resistance_per_layer_ohm": round(r_layer, 3),
             "dc_resistance_total_ohm": round(r_layer * (2 if cp.layers == "both" else 1), 3),
             "outer_turn_mm": [round(2 * (info["a"] + R0) + cp.trace, 2), round(2 * R0 + cp.trace, 2)],
             "inner_opening_mm": round(2 * info["Rn"] - cp.trace, 2),
             "copper_oz": cp.copper_oz}
    if cp.shape == "round":
        d_out = 2 * R0 + cp.trace
        d_in = 2 * (R0 - (cp.turns - 1) * p) - cp.trace
        stats["inductance_per_layer_uH_estimate"] = round(inductance_round_uH(cp.turns, d_out, d_in), 3)
    stats["note"] = ("Estimates only - measure the real board (two-layer inductance also depends on board "
                     "thickness). Inductance is not estimated for racetrack coils.")
    desc = (f"{cp.shape} PCB coil, {cp.turns} turns/layer, {cp.layers} layer(s), {cp.trace} mm trace, "
            f"{cp.gap} mm gap; generated by Eagle AI Design Studio coil generator. Params: {asdict(cp)}")
    pins = [{"name": "A", "pads": ["A"], "direction": "pas", "side": "left"},
            {"name": "B", "pads": ["B"], "direction": "pas", "side": "right"}]
    if cp.layers == "both" and cp.center_tap:
        pins.append({"name": "CT", "pads": ["CT"], "direction": "pas", "side": "bottom"})
    spec = {"name": cp.name, "prefix": "L", "description": desc,
            "package": {"type": "custom", "name": cp.name, "pads": pads, "graphics": graphics,
                        "silk_box": False, "description": desc,
                        "body_w": cp.length if cp.shape == "racetrack" else cp.width, "body_l": cp.width},
            "pins": pins}
    return spec, stats


def params_from(d: Dict) -> CoilParams:
    fields = CoilParams.__dataclass_fields__
    kw = {k: v for k, v in (d or {}).items() if k in fields}
    for k, f in fields.items():
        if k in kw and f.type in ("float", float):
            kw[k] = float(kw[k])
        elif k in kw and f.type in ("int", int):
            kw[k] = int(kw[k])
        elif k in kw and f.type in ("bool", bool) and isinstance(kw[k], str):
            kw[k] = kw[k].lower() in ("1", "true", "yes")
    return CoilParams(**kw)
