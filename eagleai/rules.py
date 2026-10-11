"""PCB design rules for placement, routing and checks.

Presets follow JLCPCB's published capabilities (jlcpcb.com/capabilities/pcb-capabilities, checked 2026-10-08):
  1-2 layer, 1 oz: min trace/space 0.10/0.10 mm;  2 oz: 0.16/0.16 mm
  via: min hole 0.15 / dia 0.25 mm; via annular ring recommended 0.25 mm (min 0.18) on 1-2 layer
  pad-to-track 0.10 mm, SMD pad-to-pad 0.15 mm, via hole-to-hole 0.2 mm
  copper to routed board edge >= 0.2 mm (0.4 mm for V-cut)
'JLC standard' adds margin on purpose (0.2 mm trace/space, 0.3/0.6 mm vias, 0.5 mm edge) - easier to make,
nothing exotic. 'JLC minimum' is the published limit; smaller features can raise the price, check the quote.

Custom rules: load an Eagle .dru (e.g. the one you use for DRC), or edit the numbers. Rules can be exported as an
Eagle 7 .dru (DRC -> Load) and are written into the .brd when the studio writes the board.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Dict, Optional

MIL = 0.0254


@dataclass
class DesignRules:
    name: str = "JLC standard (2 layer, 1 oz)"
    layers: int = 2
    copper_oz: float = 1.0
    trace: float = 0.20            # default trace width, mm
    clearance: float = 0.20        # copper to copper (wire-wire, wire-pad, pad-pad different nets)
    min_trace: float = 0.10        # nothing below this, ever
    min_clearance: float = 0.10
    smd_clearance: float = 0.15    # SMD pad to SMD pad (different nets)
    via_drill: float = 0.30
    via_dia: float = 0.60
    min_drill: float = 0.15
    min_annular: float = 0.15      # absolute min on vias (recommended 0.25 for 1-2 layer)
    hole_to_hole: float = 0.20
    edge_clearance: float = 0.50   # copper to board outline (routed edge)
    silk_width: float = 0.15
    silk_text: float = 1.0
    source: str = "preset"

    def check(self) -> list:
        """Rules that break the fab's limits."""
        out = []
        if self.trace < self.min_trace:
            out.append(f"trace {self.trace} mm < minimum {self.min_trace} mm")
        if self.clearance < self.min_clearance:
            out.append(f"clearance {self.clearance} mm < minimum {self.min_clearance} mm")
        if self.via_drill < self.min_drill:
            out.append(f"via drill {self.via_drill} mm < minimum {self.min_drill} mm")
        if (self.via_dia - self.via_drill) / 2 < self.min_annular - 1e-9:
            out.append(f"via annular ring {(self.via_dia - self.via_drill) / 2:.3f} mm < {self.min_annular} mm")
        return out

    def brief(self) -> Dict:
        return {k: v for k, v in asdict(self).items()}


PRESETS: Dict[str, DesignRules] = {
    "jlc_standard": DesignRules(),
    "jlc_minimum_1oz": DesignRules(name="JLC minimum (2 layer, 1 oz)", trace=0.10, clearance=0.10, via_drill=0.20,
                                   via_dia=0.45, min_annular=0.125, edge_clearance=0.20),
    "jlc_standard_2oz": DesignRules(name="JLC standard (2 layer, 2 oz)", copper_oz=2.0, trace=0.25, clearance=0.20,
                                    min_trace=0.16, min_clearance=0.16, via_drill=0.30, via_dia=0.82,
                                    min_annular=0.254, edge_clearance=0.50),
    "jlc_4layer": DesignRules(name="JLC standard (4 layer, 1 oz outer)", layers=4, trace=0.15, clearance=0.15,
                              min_trace=0.09, min_clearance=0.09, via_drill=0.30, via_dia=0.60, edge_clearance=0.50),
}


# ------------------------------------------------------------------ Eagle .dru
def _mm(v: str) -> Optional[float]:
    m = re.match(r"\s*(-?[\d.]+)\s*(mm|mil|in|mic)?", v or "")
    if not m:
        return None
    n = float(m.group(1))
    u = (m.group(2) or "mil").lower()
    return {"mm": n, "mil": n * MIL, "in": n * 25.4, "mic": n / 1000}[u]


def _first(v: str) -> str:
    return (v or "").split()[0] if (v or "").split() else ""


def from_eagle_params(p: Dict[str, str], name: str = "custom") -> DesignRules:
    """Eagle design-rule parameters (from a .dru or a .brd <designrules>) -> DesignRules."""
    r = DesignRules(name=name, source="eagle")
    g = lambda k: _mm(_first(p.get(k, ""))) if p.get(k) else None
    ww = [x for x in (g("mdWireWire"), g("mdWirePad"), g("mdWireVia"), g("mdPadPad"), g("mdPadVia")) if x]
    if ww:
        r.clearance = r.min_clearance = min(ww)
    if g("mdSmdSmd"):
        r.smd_clearance = g("mdSmdSmd")
    if g("msWidth"):
        r.min_trace = g("msWidth")
    if g("msDrill"):
        r.via_drill = r.min_drill = g("msDrill")
    if g("mdCopperDimension"):
        r.edge_clearance = g("mdCopperDimension")
    if g("mdDrill"):
        r.hole_to_hole = g("mdDrill")
    ann = g("rlMinViaOuter")
    if ann:
        r.min_annular = ann
        r.via_dia = round(r.via_drill + 2 * ann, 3)
    m = re.findall(r"\d+", p.get("layerSetup", ""))
    if m:
        r.layers = len(set(m))
    cu = re.findall(r"[\d.]+mm", p.get("mtCopper", ""))
    if cu:
        r.copper_oz = round(float(cu[0][:-2]) / 0.035, 1)
    r.trace = max(0.2, r.min_trace)          # default width: comfortable, never below the file's minimum
    r.clearance = max(r.clearance, r.min_clearance)
    return r


def read_dru(path: Path) -> DesignRules:
    p = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            p[k.strip()] = v.strip()
    if not any(k.startswith(("md", "ms")) for k in p):
        raise ValueError(f"{Path(path).name} doesn't look like an Eagle design rules file")
    return from_eagle_params(p, name=Path(path).stem)


def eagle_params(r: DesignRules) -> Dict[str, str]:
    mm = lambda v: f"{v:.4g}mm"
    c = mm(r.clearance)
    ann = mm(max(r.min_annular, (r.via_dia - r.via_drill) / 2))
    layer_setup = "(1*16)" if r.layers == 2 else "(1*2*15*16)" if r.layers == 4 else "(1*16)"
    cu = " ".join([f"{0.035 * r.copper_oz:.4f}mm"] * 16)
    return {
        "description[en]": f"<b>{r.name}</b> - written by Eagle AI Design Studio",
        "layerSetup": layer_setup, "mtCopper": cu,
        "mdWireWire": c, "mdWirePad": c, "mdWireVia": c, "mdPadPad": c, "mdPadVia": c, "mdViaVia": c,
        "mdSmdPad": mm(r.smd_clearance), "mdSmdVia": c, "mdSmdSmd": mm(r.smd_clearance),
        "mdViaViaSameLayer": "8mil", "mnLayersViaInSmd": "2", "mdCopperDimension": mm(r.edge_clearance),
        "mdDrill": mm(r.hole_to_hole), "mdSmdStop": "0mil",
        "msWidth": mm(r.min_trace), "msDrill": mm(r.min_drill), "msMicroVia": "9.99mm", "msBlindViaRatio": "0.5",
        "rvPadTop": "0.25", "rvPadInner": "0.25", "rvPadBottom": "0.25",
        "rvViaOuter": "0.25", "rvViaInner": "0.25", "rvMicroViaOuter": "0.25", "rvMicroViaInner": "0.25",
        "rlMinPadTop": "10mil", "rlMaxPadTop": "20mil", "rlMinPadInner": "10mil", "rlMaxPadInner": "20mil",
        "rlMinPadBottom": "10mil", "rlMaxPadBottom": "20mil",
        "rlMinViaOuter": ann, "rlMaxViaOuter": "20mil", "rlMinViaInner": ann, "rlMaxViaInner": "20mil",
        "mlMinStopFrame": "2mil", "mlMaxStopFrame": "2mil", "mlMinCreamFrame": "0mil", "mlMaxCreamFrame": "0mil",
        "mlViaStopLimit": "0mil", "srRoundness": "0", "srMinRoundness": "0mil", "srMaxRoundness": "0mil",
        "slThermalIsolate": "10mil", "slThermalsForVias": "0", "dpMaxLengthDifference": "10mm",
        "dpGapFactor": "2.5", "checkGrid": "0", "checkAngle": "0", "checkFont": "1", "checkRestrict": "1",
        "useDiameter": "13", "maxErrors": "50",
    }


def dru_text(r: DesignRules) -> str:
    return "\n".join(f"{k} = {v}" for k, v in eagle_params(r).items()) + "\n"


def write_dru(r: DesignRules, path: Path) -> Path:
    path = Path(path)
    path.write_text(dru_text(r), encoding="utf-8")
    return path


def apply_to_brd_xml(root: ET.Element, r: DesignRules):
    """Put the rules into a .brd's <designrules> (Eagle uses them for DRC and its autorouter)."""
    b = root.find("drawing/board")
    dr = b.find("designrules")
    if dr is None:
        dr = ET.Element("designrules")
        kids = list(b)
        idx = next((i for i, k in enumerate(kids) if k.tag in ("autorouter", "elements", "signals")), len(kids))
        b.insert(idx, dr)
    dr.set("name", re.sub(r"[^A-Za-z0-9_]+", "_", r.name)[:40])
    for k, v in eagle_params(r).items():
        if k.startswith("description"):
            continue
        el = next((e for e in dr.findall("param") if e.get("name") == k), None)
        if el is None:
            el = ET.SubElement(dr, "param", name=k)
        el.set("value", v)


# ------------------------------------------------------------------ trace width from current
def trace_width_for_current(amps: float, copper_oz: float = 1.0, temp_rise_c: float = 10.0, outer: bool = True) -> float:
    """IPC-2221 (k = 0.048 outer, 0.024 inner). Returns mm, rounded up to 0.05 mm."""
    k = 0.048 if outer else 0.024
    area_mil2 = (amps / (k * temp_rise_c ** 0.44)) ** (1 / 0.725)
    thick_mil = 1.378 * copper_oz
    w_mm = area_mil2 / thick_mil * MIL
    return max(0.1, round(-(-w_mm // 0.05) * 0.05, 2))


# ------------------------------------------------------------------ per-project choice
def load_project_rules(state: Dict, default_key: str = "jlc_standard") -> DesignRules:
    d = state.get("design_rules")
    if isinstance(d, dict):
        try:
            return DesignRules(**{k: v for k, v in d.items() if k in {f.name for f in fields(DesignRules)}})
        except TypeError:
            pass
    return PRESETS.get(default_key, PRESETS["jlc_standard"])
