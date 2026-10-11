"""Recommended ratings for the BOM and the MD: how many watts a resistor must handle, the minimum voltage of a
capacitor, the current an inductor must carry.

Three inputs, best first:
- stress the AI or the user set on a part (PartSpec.stress: V across it, W dissipated, A through it) - needed where
  the circuit decides it (an LED resistor's current, a resonant tank's peak voltage, an inductor's current);
- worst-case net voltages (Design.net_volts, set by the AI or in the Nets tab), e.g. +12V = 14.4 in a car;
- a guess from the net name (3V3, +12V, VCC9, GND) when nothing was set.

The recommended minimum rating = stress x derating, rounded up to the next standard rating:
ceramic X5R/X7R capacitors 2x (they lose capacitance under DC bias), C0G/NP0 and film 1.5x, electrolytic 1.25x,
tantalum 2x; resistor power 2x; other parts 1.25x volts and 1.5x amps.
The rating actually chosen comes from the value/note ("100n 50V", "0.25W") or, for resistors, the package size.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

CAP_VOLTS = [4, 6.3, 10, 16, 25, 35, 50, 63, 100, 160, 200, 250, 400, 450, 630, 1000, 2000, 3000]
RES_WATTS = [1 / 20, 1 / 16, 1 / 10, 1 / 8, 1 / 4, 1 / 2, 3 / 4, 1, 2, 3, 5, 10]
# typical thick-film chip resistor: (power W, max working voltage V)
RES_SIZE = {"0201": (1 / 20, 25), "0402": (1 / 16, 50), "0603": (1 / 10, 75), "0805": (1 / 8, 150),
            "1206": (1 / 4, 200), "1210": (1 / 2, 200), "1812": (3 / 4, 200), "2010": (3 / 4, 200),
            "2512": (1, 200)}
FRACTIONS = {1 / 20: "1/20", 1 / 16: "1/16", 1 / 10: "1/10", 1 / 8: "1/8", 1 / 4: "1/4", 1 / 3: "1/3", 1 / 2: "1/2",
             3 / 4: "3/4"}
GROUND = re.compile(r"^(A|D|P|S|C)?(GND|VSS|0V|COM)\w*$|^(GND|VSS)_?\w*$", re.I)


def _prefix(ref: str) -> str:
    return re.sub(r"\d.*", "", ref.upper())


def _num(s) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ net voltages
def net_voltage_from_name(net: str) -> Optional[float]:
    n = net.strip().upper().lstrip("+")
    if GROUND.match(net.strip()):
        return 0.0
    m = re.fullmatch(r"(?:V|VCC|VDD|VIN|VBAT|VBUS|P|VS|VDDA?|AVDD|DVDD)?_?(\d+)V(\d+)?(?:_?\w*)?", n)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}") if m.group(2) else float(m.group(1))
    m = re.fullmatch(r"(?:VCC|VDD|V)_?(\d+(?:\.\d+)?)", n)
    if m:
        return float(m.group(1))
    m = re.fullmatch(r"(?:VCC|VDD|V)_?(\d+)V(\d+)", n)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    return None


def net_voltage(design, net: str) -> Tuple[Optional[float], str]:
    """(worst-case volts, 'set' | 'name' | '')."""
    nv = getattr(design, "net_volts", None) or {}
    if net in nv and _num(nv[net]) is not None:
        return float(nv[net]), "set"
    v = net_voltage_from_name(net)
    return (v, "name") if v is not None else (None, "")


def part_nets(design, ref: str) -> List[str]:
    ref = ref.upper()
    return [n for n, ms in design.nets.items() if any(m.split(".", 1)[0].upper() == ref for m in ms)]


# ------------------------------------------------------------------ parsing what was chosen
def chosen_volts(text: str) -> Optional[float]:
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(k)?V(?:DC)?\b", text or "", re.I)
    if not m:
        return None
    return float(m.group(1)) * (1000 if m.group(2) else 1)


def chosen_watts(text: str) -> Optional[float]:
    t = text or ""
    m = re.search(r"(?<![\d./])(\d+)\s*/\s*(\d+)\s*W\b", t, re.I)
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2))
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(m)?W\b", t)
    if m:
        return float(m.group(1)) / (1000 if m.group(2) else 1)
    return None


def size_of(package: str) -> Optional[str]:
    m = re.search(r"(?<!\d)(0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)", (package or "").upper())
    return m.group(1) if m else None


def cap_kind(text: str) -> str:
    t = (text or "").upper()
    if "TANT" in t:
        return "tantalum"
    if any(k in t for k in ("CPOL", "ELKO", "ELECTROLYT", "POLAR", "E-CAP", "ALU")):
        return "electrolytic"
    if any(k in t for k in ("C0G", "NP0", "COG", "NPO")):
        return "C0G"
    if any(k in t for k in ("FILM", "WIMA", "MKS", "MKT", "MKP")):
        return "film"
    return "ceramic"


CAP_DERATE = {"ceramic": 2.0, "C0G": 1.5, "film": 1.5, "electrolytic": 1.25, "tantalum": 2.0}


def _up(x: float, steps) -> float:
    for s in steps:
        if s >= x - 1e-12:
            return s
    return x


def fmt_w(w: float) -> str:
    for k, s in FRACTIONS.items():
        if abs(w - k) < 1e-9:
            return f"{s} W"
    if w < 1:
        return f"{w * 1000:.3g} mW"
    return f"{w:.3g} W"


def fmt_v(v: float) -> str:
    return f"{v:.3g} V"


def fmt_a(a: float) -> str:
    return f"{a * 1000:.3g} mA" if a < 1 else f"{a:.3g} A"


# ------------------------------------------------------------------ the analysis
@dataclass
class Rating:
    ref: str
    sees: Dict[str, float] = field(default_factory=dict)      # V / W / A the part sees (worst case)
    need: Dict[str, float] = field(default_factory=dict)      # recommended minimum rating
    have: Dict[str, float] = field(default_factory=dict)      # rating of the part chosen (value/note/package)
    why: List[str] = field(default_factory=list)
    missing: str = ""                                          # what is needed to work it out
    problems: List[str] = field(default_factory=list)          # chosen rating below the recommended one

    def text(self, ascii_only: bool = False) -> str:
        """Short form for the BOM: '>= 25 V (sees 14.4 V)'."""
        ge = ">=" if ascii_only else "≥"
        out = []
        for k, f in (("W", fmt_w), ("V", fmt_v), ("A", fmt_a)):
            if k in self.need:
                s = f"{ge} {f(self.need[k])}"
                if k in self.sees:
                    s += f" ({f(self.sees[k])})"
                out.append(s)
        if not out and self.missing:
            return "?"
        txt = ", ".join(out)
        if self.problems:
            txt = ("UNDER-RATED: " if ascii_only else "⚠ UNDER-RATED: ") + txt
        return txt

    def detail(self) -> str:
        parts = [self.text(True)] if self.need else []
        parts += self.why
        if self.missing:
            parts.append("unknown: " + self.missing)
        parts += self.problems
        return "; ".join(p for p in parts if p)


def _package(design, index, ref) -> str:
    if index is None:
        return ""
    try:
        dev, _ = design.device_for(index, ref)
        return dev.package if dev else ""
    except Exception:
        return ""


def _value_ohms(v: str) -> Optional[float]:
    from .calc import parse
    try:
        x = parse(re.split(r"\s", (v or "").strip())[0] if v else "")
        return x if x and x > 0 else None
    except Exception:
        return None


def analyze_part(design, index, ref: str) -> Rating:
    ps = design.parts[ref]
    pre = _prefix(ref)
    r = Rating(ref)
    st = {k.upper(): _num(v) for k, v in (getattr(ps, "stress", None) or {}).items() if k.upper() in ("V", "W", "A")}
    st = {k: v for k, v in st.items() if v is not None}
    why_set = (getattr(ps, "stress", None) or {}).get("why", "")
    nets = part_nets(design, ref)
    volts = {n: net_voltage(design, n) for n in nets}
    text = f"{ps.value} {ps.note} {ps.mpn}"
    package = _package(design, index, ref)

    across, how = None, ""
    if "V" in st:
        across, how = st["V"], "set"
    elif len(nets) == 2 and all(v is not None for v, _ in volts.values()):
        (a, (va, sa)), (b, (vb, sb)) = volts.items()
        across = abs(va - vb)
        how = f"{a} {fmt_v(va)} to {b} {fmt_v(vb)}" + (" (from the net names)" if "name" in (sa, sb) else "")
    elif len(nets) == 2:
        known = [(n, v) for n, (v, _s) in volts.items() if v is not None]
        unknown = [n for n, (v, _s) in volts.items() if v is None]
        if known and known[0][1] and pre == "R":
            across = abs(known[0][1])                            # bound: the other end anywhere down to 0 V
            how = f"at most {fmt_v(across)}: {known[0][0]} to {unknown[0]} (set_stress for the real value)"
        else:
            r.missing = "max voltage of net " + ", ".join(unknown)
    elif len(nets) < 2 and pre in ("R", "C"):
        r.missing = "not connected on both ends"

    if pre == "C":
        kind = cap_kind(f"{ps.library} {ps.deviceset} {ps.device} {package or ''} {text}")
        if across:
            k = CAP_DERATE[kind]
            r.sees["V"] = across
            r.need["V"] = _up(max(across * k, 6.3), CAP_VOLTS)
            r.why.append(f"sees {fmt_v(across)} ({how}); {k:g}x for {kind}")
        elif across is not None:
            r.why.append(f"sees 0 V ({how})")
        have = chosen_volts(text)
        if have:
            r.have["V"] = have
    elif pre == "R":
        ohms = _value_ohms(ps.value)
        if "W" in st:
            r.sees["W"] = st["W"]
            r.why.append(f"dissipates {fmt_w(st['W'])} (set{': ' + why_set if why_set else ''})")
        elif "A" in st and ohms:
            r.sees["W"] = st["A"] ** 2 * ohms
            r.why.append(f"{fmt_a(st['A'])} through {ps.value} = {fmt_w(r.sees['W'])}")
        elif across is not None and ohms:
            r.sees["W"] = across ** 2 / ohms
            r.why.append(f"{fmt_v(across)}^2 / {ps.value} = {fmt_w(r.sees['W'])} ({how})")
        elif across is not None and not ohms:
            r.missing = "resistor value"
        if "W" in r.sees:
            r.need["W"] = _up(r.sees["W"] * 2, RES_WATTS)
            r.why.append("2x for resistor power")
            r.missing = ""
        if across is not None:
            r.sees["V"] = across
        size = size_of(package) or size_of(ps.device)
        have_w = chosen_watts(text) or (RES_SIZE[size][0] if size else None)
        if have_w:
            r.have["W"] = have_w
        if size and across is not None and across > RES_SIZE[size][1]:
            r.problems.append(f"{fmt_v(across)} is above the {RES_SIZE[size][1]} V working voltage of a {size} resistor")
    else:
        if across is not None and ("V" in st or pre in ("L", "D", "Q", "F")) and across:
            r.sees["V"] = across
            r.need["V"] = across * 1.25
            r.why.append(f"sees {fmt_v(across)} ({how}); 1.25x")
        have = chosen_volts(text)
        if have and "V" in r.need:
            r.have["V"] = have
        if pre not in ("L", "D", "Q", "F") and "V" not in st:
            r.missing = ""                    # ICs, connectors, LEDs: ratings come from their datasheets
    if "A" in st and pre != "R":
        r.sees["A"] = st["A"]
        r.need["A"] = st["A"] * (1.3 if pre == "L" else 1.5)
        r.why.append(f"carries {fmt_a(st['A'])}{' (' + why_set + ')' if why_set else ''}; "
                     f"{'1.3x (saturation current)' if pre == 'L' else '1.5x'}")
    elif pre == "L" and not r.need:
        r.missing = "current through it (set_stress A)"
    for k in ("V", "W"):
        if k in r.need and k in r.have and r.have[k] + 1e-12 < r.need[k]:
            f = fmt_v if k == "V" else fmt_w
            r.problems.append(f"chosen {f(r.have[k])} is below the recommended {f(r.need[k])}")
    return r


def analyze(design, index=None) -> Dict[str, Rating]:
    out = {}
    for ref in design.parts:
        try:
            out[ref] = analyze_part(design, index, ref)
        except Exception as exc:              # a rating must never break the BOM / MD
            out[ref] = Rating(ref, missing=f"could not work it out: {exc}")
    return out


def group_text(ratings: Dict[str, Rating], refs: List[str], ascii_only: bool = False) -> str:
    """Worst case of a BOM line with several refs."""
    rs = [ratings[r] for r in refs if r in ratings]
    if not rs:
        return ""
    worst = Rating(refs[0])
    for r in rs:
        for k, v in r.need.items():
            if v > worst.need.get(k, -1):
                worst.need[k] = v
                if k in r.sees:
                    worst.sees[k] = r.sees[k]
        worst.problems += r.problems
        if r.missing and not r.need:
            worst.missing = r.missing
    return worst.text(ascii_only)


def unknown_nets(design, ratings: Dict[str, Rating]) -> List[str]:
    out = []
    for r in ratings.values():
        m = re.match(r"max voltage of net (.+)$", r.missing)
        if m:
            out += [x.strip() for x in m.group(1).split(",")]
    return sorted(set(out))


def problems(design, index=None) -> List[Tuple[str, str, str]]:
    """(level, where, message) for Check / run_checks."""
    rs = analyze(design, index)
    out = [("warn", ref, r.detail()) for ref, r in rs.items() if r.problems]
    nets = unknown_nets(design, rs)
    if nets:
        out.append(("info", "ratings", "set the worst-case voltage of net(s) " + ", ".join(nets[:12])
                    + (" …" if len(nets) > 12 else "") + " to get capacitor/resistor ratings"))
    return out
