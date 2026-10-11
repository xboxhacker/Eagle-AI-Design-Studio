"""Build an Eagle .lbr (package + symbol + deviceset) from a JSON part spec.

The spec is what Claude extracts from a datasheet (see extract_spec_from_datasheet);
all geometry/XML is generated here in code, so the model never writes raw XML.

EVERYTHING produced this way is "unverified": a wrong land pattern costs a
board run. The app flags these libraries in the Problems panel until a human
checks them against the datasheet drawing.

Spec (all lengths in mm):
{
  "name": "TPS62130",                # library + deviceset name
  "prefix": "U",
  "description": "3-17V 3A buck",
  "package": {
      "type": "dual_smd" | "quad_smd" | "dual_tht" | "sot23" | "custom",
      "name": "VQFN-16",
      "pins": 16, "pitch": 0.5, "span": 3.7,       # span = pad-centre to pad-centre across the part
      "pad_w": 0.3, "pad_l": 0.9,                  # pad_w across pitch direction, pad_l along the lead
      "drill": 0.8, "dia": 1.4,                    # dual_tht only
      "body_w": 3.0, "body_l": 3.0,                # silkscreen outline
      "exposed_pad": {"name": "EP", "w": 1.6, "h": 1.6},
      "pads": [{"name":"1","x":0,"y":0,"w":1,"h":1,"type":"smd"|"tht","drill":0.8,"shape":"round"}],  # custom
      "graphics": [{"type":"wire","x1":0,"y1":0,"x2":1,"y2":0,"width":0.5,"layer":1,"curve":-90},
                   {"type":"circle","x":0,"y":0,"radius":5,"width":0.1,"layer":20}],   # custom: copper, outline...
      "silk_box": true                             # custom: draw the default body box (false for coils etc.)
  },
  "pins": [{"name":"VIN","pads":["1","2"],"direction":"pwr","side":"left"}]
}
"""
from __future__ import annotations

import base64
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

GRID = 2.54
DIRECTIONS = ("nc", "in", "out", "io", "oc", "pwr", "pas", "hiz", "sup")
GND_RE = re.compile(r"^(A|P|D|S)?(GND|VSS|VEE|EP|PAD|THERMAL)", re.I)

LAYERS = [(1, "Top", 4), (16, "Bottom", 1), (17, "Pads", 2), (18, "Vias", 2), (19, "Unrouted", 6),
          (20, "Dimension", 15), (21, "tPlace", 7), (22, "bPlace", 7), (23, "tOrigins", 15),
          (24, "bOrigins", 15), (25, "tNames", 7), (26, "bNames", 7), (27, "tValues", 7),
          (28, "bValues", 7), (29, "tStop", 7), (30, "bStop", 7), (31, "tCream", 7),
          (32, "bCream", 7), (35, "tGlue", 7), (36, "bGlue", 7), (39, "tKeepout", 4),
          (40, "bKeepout", 1), (41, "tRestrict", 4), (42, "bRestrict", 1), (43, "vRestrict", 2),
          (44, "Drills", 7), (45, "Holes", 7), (51, "tDocu", 7), (52, "bDocu", 7),
          (91, "Nets", 2), (92, "Busses", 1), (93, "Pins", 2), (94, "Symbols", 4),
          (95, "Names", 7), (96, "Values", 7), (97, "Info", 7), (98, "Guide", 6)]


class SpecError(ValueError):
    pass


def _r(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def snap(v: float, step: float = GRID) -> float:
    return round(round(v / step) * step, 4)


def safe_name(s: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_.+\-]", "_", (s or "").strip())
    if not n:
        raise SpecError("name is empty")
    return n


# ------------------------------------------------------------------- packages
Pad = Dict  # {name,x,y,w,h,type,drill,dia,shape,rot}


def _smd(name, x, y, w, h) -> Pad:
    return {"name": str(name), "x": x, "y": y, "w": w, "h": h, "type": "smd"}


def dual_pads(p: Dict, smd: bool) -> List[Pad]:
    n = int(p["pins"])
    pitch, span = float(p["pitch"]), float(p["span"])
    if n < 2 or n % 2:
        raise SpecError("dual packages need an even pin count (use type 'sot23' or 'custom' otherwise)")
    half = n // 2
    pads: List[Pad] = []
    for i in range(half):                      # left column, pin 1 at top, going down
        y = (half - 1) * pitch / 2 - i * pitch
        pads.append(_pad(p, smd, i + 1, -span / 2, y, left=True))
    for i in range(half):                      # right column, going up
        y = -(half - 1) * pitch / 2 + i * pitch
        pads.append(_pad(p, smd, half + i + 1, span / 2, y, left=False))
    return pads


def _pad(p: Dict, smd: bool, name, x, y, left: bool, vertical_lead: bool = False) -> Pad:
    if smd:
        pw, pl = float(p.get("pad_w", float(p["pitch"]) * 0.55)), float(p.get("pad_l", 1.5))
        return _smd(name, x, y, pl, pw) if not vertical_lead else _smd(name, x, y, pw, pl)
    drill = float(p.get("drill", 0.8))
    return {"name": str(name), "x": x, "y": y, "type": "tht", "drill": drill,
            "dia": float(p.get("dia", drill * 1.8)), "shape": "long" if str(name) != "1" else "square"}


def quad_pads(p: Dict) -> List[Pad]:
    n = int(p["pins"])
    if n % 4:
        raise SpecError("quad packages need a pin count divisible by 4")
    k = n // 4
    pitch, span = float(p["pitch"]), float(p["span"])
    pads: List[Pad] = []
    num = 1
    for i in range(k):                          # left side, top to bottom
        pads.append(_pad(p, True, num, -span / 2, (k - 1) * pitch / 2 - i * pitch, True)); num += 1
    for i in range(k):                          # bottom, left to right
        pads.append(_pad(p, True, num, -(k - 1) * pitch / 2 + i * pitch, -span / 2, False, True)); num += 1
    for i in range(k):                          # right, bottom to top
        pads.append(_pad(p, True, num, span / 2, -(k - 1) * pitch / 2 + i * pitch, False)); num += 1
    for i in range(k):                          # top, right to left
        pads.append(_pad(p, True, num, (k - 1) * pitch / 2 - i * pitch, span / 2, False, True)); num += 1
    return pads


def sot23_pads(p: Dict) -> List[Pad]:
    n = int(p["pins"])
    pitch = float(p.get("pitch", 0.95))
    span = float(p.get("span", 2.6))
    pw, pl = float(p.get("pad_w", 0.6)), float(p.get("pad_l", 1.1))
    if n == 3:
        pos = [(-pitch, -span / 2), (pitch, -span / 2), (0, span / 2)]
    elif n == 5:
        pos = [(-pitch, -span / 2), (0, -span / 2), (pitch, -span / 2), (pitch, span / 2), (-pitch, span / 2)]
    elif n == 6:
        pos = [(-pitch, -span / 2), (0, -span / 2), (pitch, -span / 2),
               (pitch, span / 2), (0, span / 2), (-pitch, span / 2)]
    else:
        raise SpecError("sot23 supports 3, 5 or 6 pins")
    return [_smd(i + 1, x, y, pw, pl) for i, (x, y) in enumerate(pos)]


def make_pads(pkg: Dict) -> List[Pad]:
    t = pkg.get("type", "")
    if t == "dual_smd":
        pads = dual_pads(pkg, True)
    elif t == "dual_tht":
        pads = dual_pads(pkg, False)
    elif t == "quad_smd":
        pads = quad_pads(pkg)
    elif t == "sot23":
        pads = sot23_pads(pkg)
    elif t == "custom":
        pads = []
        for q in pkg.get("pads", []):
            rot = float(q.get("rot", 0) or 0) % 360
            if q.get("type", "smd") == "tht":
                shape = q.get("shape", "round")
                if shape not in ("round", "square", "octagon", "long", "offset"):
                    raise SpecError(f"pad {q.get('name')}: shape must be round, square, octagon, long or offset")
                pads.append({"name": str(q["name"]), "x": float(q["x"]), "y": float(q["y"]), "type": "tht",
                             "drill": float(q.get("drill", 0.8)), "dia": float(q.get("dia", 1.5)),
                             "shape": shape, "rot": rot})
            else:
                pd = _smd(q["name"], float(q["x"]), float(q["y"]), float(q["w"]), float(q["h"]))
                pd["rot"], pd["roundness"] = rot, int(q.get("roundness", 0) or 0)
                pads.append(pd)
    else:
        raise SpecError(f"unknown package type '{t}' (dual_smd, quad_smd, dual_tht, sot23, custom)")
    ep = pkg.get("exposed_pad")
    if ep:
        pads.append(_smd(ep.get("name", "EP"), float(ep.get("x", 0)), float(ep.get("y", 0)),
                         float(ep["w"]), float(ep["h"])))
    if not pads:
        raise SpecError("package has no pads")
    names = [p["name"] for p in pads]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise SpecError(f"duplicate pad names: {sorted(dup)}")
    return pads


def package_xml(pkg: Dict, pads: List[Pad]) -> ET.Element:
    el = ET.Element("package", name=safe_name(pkg.get("name") or "PKG"))
    xs = [p["x"] + (p.get("w", p.get("dia", 1)) / 2) for p in pads] + [p["x"] - (p.get("w", p.get("dia", 1)) / 2) for p in pads]
    ys = [p["y"] + (p.get("h", p.get("dia", 1)) / 2) for p in pads] + [p["y"] - (p.get("h", p.get("dia", 1)) / 2) for p in pads]
    bw = float(pkg.get("body_w") or (max(xs) - min(xs)))
    bl = float(pkg.get("body_l") or (max(ys) - min(ys)))
    hx, hy = bw / 2, bl / 2
    if pkg.get("description"):
        ET.SubElement(el, "description").text = pkg["description"]
    for g in pkg.get("graphics", []):          # custom geometry (copper traces, board outline, docs)
        if g.get("type", "wire") == "wire":
            w = ET.SubElement(el, "wire", x1=_r(g["x1"]), y1=_r(g["y1"]), x2=_r(g["x2"]), y2=_r(g["y2"]),
                              width=_r(g.get("width", 0.2)), layer=str(int(g.get("layer", 21))))
            if g.get("curve"):
                w.set("curve", _r(g["curve"]))
        elif g["type"] == "circle":
            ET.SubElement(el, "circle", x=_r(g["x"]), y=_r(g["y"]), radius=_r(g["radius"]),
                          width=_r(g.get("width", 0.2)), layer=str(int(g.get("layer", 21))))
        elif g["type"] == "rectangle":
            ET.SubElement(el, "rectangle", x1=_r(g["x1"]), y1=_r(g["y1"]), x2=_r(g["x2"]), y2=_r(g["y2"]),
                          layer=str(int(g.get("layer", 21))))
        elif g["type"] == "polygon":
            pg = ET.SubElement(el, "polygon", width=_r(g.get("width", 0.1)), layer=str(int(g.get("layer", 1))))
            for v in g["vertices"]:
                ve = ET.SubElement(pg, "vertex", x=_r(v["x"]), y=_r(v["y"]))
                if v.get("curve"):
                    ve.set("curve", _r(v["curve"]))
        elif g["type"] == "text":
            ET.SubElement(el, "text", x=_r(g["x"]), y=_r(g["y"]), size=_r(g.get("size", 1.0)),
                          layer=str(int(g.get("layer", 21)))).text = str(g.get("text", ""))
        elif g["type"] == "hole":
            ET.SubElement(el, "hole", x=_r(g["x"]), y=_r(g["y"]), drill=_r(g["drill"]))
        else:
            raise SpecError(f"unknown graphics type '{g['type']}' (wire, circle, rectangle, polygon, text, hole)")
    if pkg.get("silk_box", True):
        # silkscreen outline (tPlace=21), and the same box on tDocu (51)
        for layer, width in (("21", 0.2), ("51", 0.1)):
            for (a, b, c, d) in ((-hx, -hy, hx, -hy), (hx, -hy, hx, hy), (hx, hy, -hx, hy), (-hx, hy, -hx, -hy)):
                ET.SubElement(el, "wire", x1=_r(a), y1=_r(b), x2=_r(c), y2=_r(d), width=_r(width), layer=layer)
        # pin-1 marker next to pad 1 on silkscreen
        p1 = next((p for p in pads if p["name"] == "1"), pads[0])
        mx, my = (-hx - 0.8, p1["y"]) if p1["x"] < 0 else (p1["x"], hy + 0.8)
        ET.SubElement(el, "circle", x=_r(mx), y=_r(my), radius="0.25", width="0.2", layer="21")
    ET.SubElement(el, "text", x=_r(-hx), y=_r(hy + 0.6), size="1", layer="25").text = ">NAME"
    ET.SubElement(el, "text", x=_r(-hx), y=_r(-hy - 1.6), size="1", layer="27").text = ">VALUE"
    for p in pads:
        if p["type"] == "smd":
            e = ET.SubElement(el, "smd", name=p["name"], x=_r(p["x"]), y=_r(p["y"]),
                              dx=_r(p["w"]), dy=_r(p["h"]), layer="1")
            if p.get("roundness"):
                e.set("roundness", str(p["roundness"]))
        else:
            e = ET.SubElement(el, "pad", name=p["name"], x=_r(p["x"]), y=_r(p["y"]),
                              drill=_r(p["drill"]), diameter=_r(p["dia"]), shape=p.get("shape", "round"))
        if p.get("rot"):
            e.set("rot", f"R{_r(p['rot'])}")
    return el


# --------------------------------------------------------------------- symbol
def assign_sides(pins: List[Dict]) -> Dict[str, List[Dict]]:
    sides: Dict[str, List[Dict]] = {"left": [], "right": [], "top": [], "bottom": []}
    auto: List[Dict] = []
    for p in pins:
        s = (p.get("side") or "").lower()
        if s in sides:
            sides[s].append(p)
        else:
            auto.append(p)
    for p in auto:
        d = p.get("direction", "io")
        if d in ("pwr", "sup"):
            sides["bottom" if GND_RE.match(p["name"]) else "top"].append(p)
        elif d == "in":
            sides["left"].append(p)
        elif d in ("out", "oc", "hiz"):
            sides["right"].append(p)
        else:
            (sides["left"] if len(sides["left"]) <= len(sides["right"]) else sides["right"]).append(p)
    return sides


def symbol_xml(name: str, pins: List[Dict], title_value: bool = True) -> ET.Element:
    sides = assign_sides(pins)
    longest = max((len(p["name"]) for p in pins), default=4)
    nl, nr, nt, nb = (len(sides[s]) for s in ("left", "right", "top", "bottom"))
    # body size on grid, even multiples of 2.54 so the centre is on grid
    h_units = max(nl, nr, 2) + 1
    h_units += h_units % 2
    name_w = longest * 1.4 * 2  # left+right names inside the body
    w_units = max(math.ceil(name_w / GRID), nt + 1, nb + 1, 6)
    w_units += w_units % 2
    W, H = w_units * GRID, h_units * GRID
    L = 7.62  # "long" pin
    sym = ET.Element("symbol", name=safe_name(name))
    for a, b, c, d in ((-W / 2, -H / 2, W / 2, -H / 2), (W / 2, -H / 2, W / 2, H / 2),
                       (W / 2, H / 2, -W / 2, H / 2), (-W / 2, H / 2, -W / 2, -H / 2)):
        ET.SubElement(sym, "wire", x1=_r(a), y1=_r(b), x2=_r(c), y2=_r(d), width="0.254", layer="94")
    ET.SubElement(sym, "text", x=_r(-W / 2), y=_r(H / 2 + 1.27), size="1.778", layer="95").text = ">NAME"
    ET.SubElement(sym, "text", x=_r(-W / 2), y=_r(-H / 2 - 3.81), size="1.778", layer="96").text = ">VALUE"

    def add(p, x, y, rot):
        ET.SubElement(sym, "pin", name=p["name"], x=_r(x), y=_r(y), length="long",
                      direction=p.get("direction", "io"), **({"rot": rot} if rot != "R0" else {}))

    for i, p in enumerate(sides["left"]):
        add(p, -W / 2 - L, H / 2 - GRID * (i + 1), "R0")
    for i, p in enumerate(sides["right"]):
        add(p, W / 2 + L, H / 2 - GRID * (i + 1), "R180")
    for i, p in enumerate(sides["top"]):
        add(p, -W / 2 + GRID * (i + 1), H / 2 + L, "R270")
    for i, p in enumerate(sides["bottom"]):
        add(p, -W / 2 + GRID * (i + 1), -H / 2 - L, "R90")
    return sym


# ------------------------------------------------------------------------ lbr
def validate_spec(spec: Dict) -> Tuple[List[Pad], List[Dict], List[str]]:
    """Raises SpecError on hard problems; returns (pads, merged pins, warnings)."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    safe_name(spec.get("name", ""))
    pkg = spec.get("package")
    if not isinstance(pkg, dict):
        raise SpecError("missing 'package'")
    try:
        pads = make_pads(pkg)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SpecError):
            raise
        raise SpecError(f"bad package geometry: {exc!r} (missing/invalid field)")
    pad_names = {p["name"] for p in pads}

    merged: Dict[str, Dict] = {}
    warnings: List[str] = []
    claimed: Dict[str, str] = {}
    for raw in spec.get("pins", []):
        name = str(raw.get("name", "")).strip()
        if not name:
            raise SpecError("a pin has no name")
        pl = [str(x) for x in (raw.get("pads") or ([raw["pad"]] if "pad" in raw else []))]
        if not pl:
            raise SpecError(f"pin {name}: no pads")
        direction = raw.get("direction", "io")
        if direction not in DIRECTIONS:
            raise SpecError(f"pin {name}: direction '{direction}' not in {DIRECTIONS}")
        for pad in pl:
            if pad not in pad_names:
                raise SpecError(f"pin {name}: pad '{pad}' does not exist in the package "
                                f"(pads: {sorted(pad_names, key=lambda s: (len(s), s))})")
            if pad in claimed and claimed[pad] != name:
                raise SpecError(f"pad {pad} is claimed by both {claimed[pad]} and {name}")
            claimed[pad] = name
        if name in merged:                      # same-name pins share one symbol pin
            merged[name]["pads"] += [x for x in pl if x not in merged[name]["pads"]]
        else:
            merged[name] = {"name": name, "pads": pl, "direction": direction, "side": raw.get("side")}
    missing = sorted(pad_names - set(claimed), key=lambda s: (len(s), s))
    if missing:
        warnings.append(f"pads with no pin (will be unconnected in Eagle): {missing}")
    if not merged:
        raise SpecError("no pins")
    return pads, list(merged.values()), warnings


def _pad_box(p: Pad):
    if p["type"] == "smd":
        w, h = p["w"], p["h"]
    else:                                     # Eagle long/offset pads are 2x diameter along x
        w, h = p["dia"] * (2 if p.get("shape") in ("long", "offset") else 1), p["dia"]
    r = (p.get("rot") or 0) % 180
    if abs(r - 90) < 1e-6:
        w, h = h, w
    elif r:
        w = h = math.hypot(w, h)              # odd angle: circumscribed box (conservative)
    return p["x"] - w / 2, p["y"] - h / 2, p["x"] + w / 2, p["y"] + h / 2


def geometry_report(spec: Dict, pads: List[Pad], pins: List[Dict], min_gap: float = 0.15) -> Dict:
    """Numbers the AI (and you) can check without seeing the footprint."""
    pin_of = {pd: pn["name"] for pn in pins for pd in pn["pads"]}
    boxes = [(p, _pad_box(p)) for p in pads]
    problems, closest = [], (1e9, "")
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (a, A), (b, B) = boxes[i], boxes[j]
            dx = max(B[0] - A[2], A[0] - B[2], 0)
            dy = max(B[1] - A[3], A[1] - B[3], 0)
            overlap = (B[0] < A[2] and A[0] < B[2] and B[1] < A[3] and A[1] < B[3])
            gap = -1.0 if overlap else math.hypot(dx, dy)
            same = pin_of.get(a["name"]) and pin_of.get(a["name"]) == pin_of.get(b["name"])
            if gap < closest[0] and not same:
                closest = (gap, f"{a['name']}-{b['name']}")
            if not same and gap < min_gap:
                problems.append(f"pads {a['name']} and {b['name']} " + ("overlap" if gap < 0 else f"only {gap:.3f} mm apart"))
    for p in pads:
        if p["type"] == "tht":
            if p["drill"] < 0.3:
                problems.append(f"pad {p['name']}: drill {p['drill']} mm is below 0.3 mm (JLC standard minimum)")
            if (p["dia"] - p["drill"]) / 2 < 0.15:
                problems.append(f"pad {p['name']}: annular ring {(p['dia'] - p['drill']) / 2:.3f} mm is too thin (>= 0.15)")
    xs = [v for _p, b in boxes for v in (b[0], b[2])]
    ys = [v for _p, b in boxes for v in (b[1], b[3])]
    g = spec["package"].get("graphics", [])
    copper = [x for x in g if str(x.get("layer")) in ("1", "16")]
    return {"pads": len(pads), "smd": sum(p["type"] == "smd" for p in pads), "tht": sum(p["type"] == "tht" for p in pads),
            "pad_extent_mm": [round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3)],
            "closest_pads": f"{closest[1]} {closest[0]:.3f} mm" if closest[1] else "",
            "copper_graphics": len(copper), "has_outline": any(str(x.get("layer")) == "20" for x in g),
            "pad_list": "; ".join(f"{p['name']}({p['x']:g},{p['y']:g})" for p in pads[:60]),
            "problems": problems or "none"}


def _new_root() -> ET.Element:
    root = ET.Element("eagle", version="7.4.0")
    drawing = ET.SubElement(root, "drawing")
    st = ET.SubElement(drawing, "settings")
    ET.SubElement(st, "setting", alwaysvectorfont="no")
    ET.SubElement(st, "setting", verticaltext="up")
    ET.SubElement(drawing, "grid", distance="0.1", unitdist="inch", unit="inch", style="lines",
                  multiple="1", display="no", altdistance="0.01", altunitdist="inch", altunit="inch")
    layers = ET.SubElement(drawing, "layers")
    for num, lname, color in LAYERS:
        ET.SubElement(layers, "layer", number=str(num), name=lname, color=str(color),
                      fill="1", visible="yes", active="yes")
    lib = ET.SubElement(drawing, "library")
    ET.SubElement(lib, "description")
    for tag in ("packages", "symbols", "devicesets"):
        ET.SubElement(lib, tag)
    return root


def _put(parent: ET.Element, el: ET.Element):
    """Add el to parent, replacing an element of the same tag+name."""
    for old in parent.findall(el.tag):
        if old.get("name") == el.get("name"):
            parent.remove(old)
    parent.append(el)


def build_lbr(spec: Dict, out_dir: Path = None, out_path: Path = None,
              note: str = "GENERATED FROM DATASHEET BY EAGLEAI - VERIFY FOOTPRINT BEFORE USE") -> Tuple[Path, List[str]]:
    """Write the part to out_path (or out_dir/<name>.lbr). If that file is an existing Eagle library the part
    is ADDED to it (a package/symbol/deviceset with the same name is replaced); other parts are kept."""
    pads, pins, warnings = validate_spec(spec)
    name = safe_name(spec["name"])
    pkg_el = package_xml(spec["package"], pads)
    sym_el = symbol_xml(name, pins)
    path = Path(out_path) if out_path else Path(out_dir) / f"{name}.lbr"
    if path.suffix.lower() != ".lbr":
        path = path.with_suffix(".lbr")
    root = None
    if path.exists():
        try:
            root = ET.parse(path).getroot()
            if root.find("drawing/library") is None:
                raise SpecError(f"{path.name} exists but is not an Eagle library - choose another file")
            for tag in ("packages", "symbols", "devicesets"):
                if root.find(f"drawing/library/{tag}") is None:
                    ET.SubElement(root.find("drawing/library"), tag)
            warnings.append(f"added to the existing library {path.name} (other parts kept)")
        except ET.ParseError:
            raise SpecError(f"{path.name} exists but can't be read (binary/damaged) - choose another file")
    if root is None:
        root = _new_root()
        root.find("drawing/library/description").text = f"{spec.get('description', '')}\n{note}".strip()
    lib = root.find("drawing/library")
    _put(lib.find("packages"), pkg_el)
    _put(lib.find("symbols"), sym_el)
    ds = ET.Element("deviceset", name=name, prefix=spec.get("prefix", "U") or "U")
    ET.SubElement(ds, "description").text = (spec.get("description", "") + ("\n" + note if note else "")).strip()
    gates = ET.SubElement(ds, "gates")
    ET.SubElement(gates, "gate", name="G$1", symbol=name, x="0", y="0")
    devices = ET.SubElement(ds, "devices")
    dev = ET.SubElement(devices, "device", name="", package=pkg_el.get("name"))
    cons = ET.SubElement(dev, "connects")
    for p in pins:
        ET.SubElement(cons, "connect", gate="G$1", pin=p["name"], pad=" ".join(p["pads"]))
    techs = ET.SubElement(dev, "technologies")
    ET.SubElement(techs, "technology", name="")
    _put(lib.find("devicesets"), ds)

    path.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tmp = path.with_suffix(".lbr.tmp")
    with open(tmp, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle SYSTEM "eagle.dtd">\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)
    tmp.replace(path)
    (path.parent / f"{name}.spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    try:
        rep = geometry_report(spec, pads, pins)
        spec["_geometry"] = rep
        if rep["problems"] != "none":
            warnings += [f"geometry: {x}" for x in rep["problems"]]
    except Exception as exc:          # the report must never stop a build
        warnings.append(f"geometry report failed: {exc}")
    return path, warnings


# -------------------------------------------------------- datasheet -> spec
SPEC_TOOL = {
    "name": "submit_part_spec",
    "description": ("Submit the part definition read from the datasheet. Use ONLY values printed in the "
                    "datasheet (pin table, recommended land pattern / package drawing). Never guess; "
                    "if a dimension is not in the document, put it in 'uncertain'."),
    "input_schema": {
        "type": "object",
        "required": ["name", "prefix", "package", "pins"],
        "properties": {
            "name": {"type": "string", "description": "Orderable part number, e.g. TPS62130RGTR -> TPS62130"},
            "prefix": {"type": "string", "description": "U for ICs, Q transistors, D diodes, J connectors"},
            "description": {"type": "string"},
            "package": {
                "type": "object",
                "required": ["type", "name"],
                "properties": {
                    "type": {"enum": ["dual_smd", "quad_smd", "dual_tht", "sot23", "custom"]},
                    "name": {"type": "string", "description": "e.g. VQFN-16_3x3"},
                    "pins": {"type": "integer", "description": "lead count, NOT including the exposed pad"},
                    "pitch": {"type": "number"}, "span": {"type": "number", "description": "pad centre to centre across the part, from the RECOMMENDED LAND PATTERN"},
                    "pad_w": {"type": "number"}, "pad_l": {"type": "number"},
                    "drill": {"type": "number"}, "dia": {"type": "number"},
                    "body_w": {"type": "number"}, "body_l": {"type": "number"},
                    "exposed_pad": {"type": "object", "properties": {
                        "name": {"type": "string"}, "w": {"type": "number"}, "h": {"type": "number"}}},
                    "pads": {"type": "array", "items": {"type": "object", "properties": {
                        "name": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
                        "w": {"type": "number"}, "h": {"type": "number"},
                        "type": {"enum": ["smd", "tht"]}, "drill": {"type": "number"}, "dia": {"type": "number"},
                        "shape": {"enum": ["round", "square", "octagon", "long", "offset"]},
                        "rot": {"type": "number"}, "roundness": {"type": "integer"}}}},
                    "graphics": {"type": "array", "description": (
                        "custom only: extra drawing. Layers: 1 Top copper, 16 Bottom copper, 20 board outline, "
                        "21 silkscreen, 29/30 solder-mask openings, 39/40 keepout, 41/42 copper restrict, 51 docu. "
                        "wire {x1,y1,x2,y2,width,layer,curve(deg, + = CCW arc)}; circle {x,y,radius,width,layer}; "
                        "rectangle {x1,y1,x2,y2,layer}; polygon {vertices:[{x,y,curve}],width,layer}; "
                        "text {x,y,text,size,layer}; hole {x,y,drill} (non-plated). Eagle 7 packages cannot hold "
                        "vias - use a tht pad to change layers."),
                        "items": {"type": "object"}},
                    "silk_box": {"type": "boolean", "description": "custom: draw the default body box (default true)"},
                },
            },
            "pins": {"type": "array", "items": {
                "type": "object", "required": ["name", "pads", "direction"],
                "properties": {
                    "name": {"type": "string", "description": "Datasheet pin name; strip overbar marks; use e.g. NRESET not ~RESET"},
                    "pads": {"type": "array", "items": {"type": "string"}},
                    "direction": {"enum": list(DIRECTIONS)},
                    "side": {"enum": ["left", "right", "top", "bottom"]},
                }}},
            "uncertain": {"type": "array", "items": {"type": "string"},
                          "description": "Anything not clearly stated in the datasheet"},
            "datasheet_pages": {"type": "string", "description": "Pages used for pinout and land pattern"},
        },
    },
}

EXTRACT_PROMPT = (
    "Read this datasheet and call submit_part_spec. Rules: (1) the pin table and package drawing / "
    "recommended land pattern are the only sources of geometry; (2) pitch and span must come from the "
    "land pattern, not the package body; (3) pins with the same name on several pads (GND, VIN) are listed "
    "once with all their pads; (4) the exposed/thermal pad goes in package.exposed_pad AND as a pin "
    "(usually GND) with that pad name; (5) if the package is not a plain dual/quad/SOT-23, use type "
    "'custom' with explicit pad coordinates (mm, origin at the part centre, y up, pin 1 top-left); "
    "(6) list anything you could not read in 'uncertain'. {hint}")


PAGE_KEYWORDS = {
    "pin configuration": 5, "pin functions": 5, "pin description": 5, "pin assignment": 5, "pinout": 5,
    "terminal functions": 5, "pin definitions": 5, "land pattern": 6, "recommended footprint": 6,
    "recommended pcb": 5, "solder pad": 4, "footprint": 3, "package outline": 5, "mechanical data": 5,
    "package information": 4, "package dimensions": 5, "dimensions": 2, "ordering information": 3,
    "package option": 3, "device information": 2,
}


def select_datasheet_pages(pdf_path: Path, hint: str = "", max_pages: int = 10, min_total: int = 13,
                           keywords: Optional[Dict[str, int]] = None, always=(0, 1)):
    """Pick the pinout / package / land-pattern pages. Returns (pdf_bytes, [page numbers]) or
    (None, why) when the whole PDF should be sent (short, scanned, or pypdf missing)."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return None, "pypdf not installed"
    import io
    reader = PdfReader(str(pdf_path))
    n = len(reader.pages)
    if n < min_total:
        return None, f"only {n} pages"
    scores, chars = [], 0
    for i, page in enumerate(reader.pages):
        try:
            t = (page.extract_text() or "").lower()
        except Exception:
            t = ""
        chars += len(t)
        sc = sum(w * min(t.count(k), 3) for k, w in (keywords or PAGE_KEYWORDS).items())
        if hint and hint.lower() in t:
            sc += 1
        scores.append(sc)
    if chars < 200 * n:
        return None, "little extractable text (scanned?)"
    keep = {i for i in always if i < n}
    for i in sorted(range(n), key=lambda i: -scores[i]):
        if len(keep) >= max_pages or scores[i] < 4:
            break
        keep.add(i)
    pages = sorted(keep)
    w = PdfWriter()
    for i in pages:
        w.add_page(reader.pages[i])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue(), [i + 1 for i in pages]


def extract_spec_from_datasheet(client, model: str, pdf_path: Path, hint: str = "",
                                tracker=None, max_tokens: int = 8000, pages_only: bool = True) -> Dict:
    """Send a datasheet (or just its relevant pages) to Claude; get a part spec back via forced tool use."""
    note = ""
    data_bytes = None
    if pages_only:
        data_bytes, info = select_datasheet_pages(Path(pdf_path), hint)
        if data_bytes is not None:
            note = (f" You are seeing pages {info} of the datasheet (the pinout/package/ordering pages). "
                    f"If the land pattern or pin table is not among them, say so in 'uncertain'.")
    if data_bytes is None:
        data_bytes = Path(pdf_path).read_bytes()
    data = base64.standard_b64encode(data_bytes).decode("ascii")
    resp = client.messages.create(
        model=model, max_tokens=max_tokens,
        tools=[SPEC_TOOL], tool_choice={"type": "tool", "name": "submit_part_spec"},
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
            {"type": "text", "text": EXTRACT_PROMPT.format(hint=hint) + note},
        ]}])
    if tracker:
        tracker.record(model, resp.usage, label=f"datasheet {Path(pdf_path).name}{' (pages)' if note else ''}")
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_part_spec":
            spec = dict(block.input)
            spec["_pages_only"] = bool(note)
            return spec
    raise RuntimeError("Claude did not return a part spec")


# -------------------------------------------------------- datasheet -> layout rules
LAYOUT_KEYWORDS = {"layout guideline": 6, "layout example": 6, "pcb layout": 6, "layout recommendation": 6,
                   "recommended layout": 6, "board layout": 5, "layout": 2, "placement": 3, "place the": 2,
                   "as close as possible": 4, "close to the": 2, "short": 1, "trace": 1, "loop": 1, "via": 1,
                   "thermal": 1, "ground plane": 2}

LAYOUT_TOOL = {
    "name": "submit_layout_rules",
    "description": "Placement rules for the parts around this IC, taken ONLY from the datasheet's layout guidance.",
    "input_schema": {"type": "object", "required": ["rules"], "properties": {
        "rules": {"type": "array", "items": {"type": "object", "required": ["role", "near_pin"], "properties": {
            "role": {"type": "string", "description": "what the part is, as the datasheet names it: input capacitor, "
                                                       "bootstrap cap, inductor, feedback divider, ..."},
            "near_pin": {"type": "string", "description": "IC pin name it must be close to (e.g. VIN, SW, FB)"},
            "max_dist_mm": {"type": "number", "description": "only if the datasheet gives a number or clearly says 'as "
                                                            "close as possible' (then 2)"},
            "same_side": {"type": "boolean"},
            "quote": {"type": "string", "description": "the datasheet sentence this comes from"}}}},
        "other_guidance": {"type": "array", "items": {"type": "string"},
                           "description": "routing/copper advice that is not about placement (keep for the user)"},
        "pages": {"type": "string"}}}}


def extract_layout_rules(client, model: str, pdf_path: Path, hint: str = "", tracker=None) -> Dict:
    data_bytes, info = select_datasheet_pages(Path(pdf_path), hint, max_pages=6, min_total=7,
                                              keywords=LAYOUT_KEYWORDS, always=())
    note = f" You are seeing pages {info} (picked for layout guidance)." if data_bytes is not None else ""
    if data_bytes is None:
        data_bytes = Path(pdf_path).read_bytes()
    data = base64.standard_b64encode(data_bytes).decode("ascii")
    resp = client.messages.create(
        model=model, max_tokens=3000, tools=[LAYOUT_TOOL],
        tool_choice={"type": "tool", "name": "submit_layout_rules"},
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
            {"type": "text", "text": f"Part: {hint or 'the main IC'}. List the placement rules from this datasheet's "
                                     f"layout guidelines / layout example (which external part goes next to which pin, "
                                     f"and how close). Only what the datasheet says; quote it.{note}"}]}])
    if tracker:
        tracker.record(model, resp.usage, label=f"layout rules {Path(pdf_path).name}")
    for b in resp.content:
        if b.type == "tool_use":
            return dict(b.input)
    raise RuntimeError("Claude did not return layout rules")
