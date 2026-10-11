"""Electronics calculators (pure math, no GUI): series/parallel R and C, LED resistors, capacitor units and codes,
Ohm's law, voltage dividers / regulator feedback, RC filters, SMD resistor codes, standard (E-series) values."""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple


class CalcError(ValueError):
    """A message for the user (bad or missing input)."""


# ------------------------------------------------------------------ standard values
E6 = [1.0, 1.5, 2.2, 3.3, 4.7, 6.8]
E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]
E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8,
       7.5, 8.2, 9.1]
E96 = [round(10 ** (i / 96), 2) for i in range(96)]          # E48/E96 follow the formula exactly
SERIES = {"E6": E6, "E12": E12, "E24": E24, "E96": E96}


def standard_values(series: str = "E24", lo: float = 1.0, hi: float = 10e6) -> List[float]:
    base = SERIES[series]
    out = []
    for d in range(math.floor(math.log10(lo)) - 1, math.ceil(math.log10(hi)) + 1):
        for m in base:
            v = round(m * 10 ** d, 12)
            if lo * (1 - 1e-9) <= v <= hi * (1 + 1e-9):
                out.append(v)
    return out


def nearest(value: float, series: str = "E24", mode: str = "nearest") -> float:
    """Closest standard value ('nearest'), or the next one up / down ('up' / 'down')."""
    if value <= 0:
        raise CalcError("value must be above 0")
    d = math.floor(math.log10(value))
    cands = [round(m * 10 ** e, 12) for e in (d - 1, d, d + 1) for m in SERIES[series]]
    if mode == "up":
        return min(c for c in cands if c >= value * (1 - 1e-9))
    if mode == "down":
        return max(c for c in cands if c <= value * (1 + 1e-9))
    return min(cands, key=lambda c: abs(math.log(c / value)))


# ------------------------------------------------------------------ parsing / formatting
_SI = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0, "r": 1.0,
       "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


def parse(text: str) -> float:
    """'10k' '4k7' '4R7' '100nF' '0.1uF' '2.2 MΩ' '1e-6' '330R' -> number. 'm' = milli, 'M' = mega."""
    s = (text or "").strip().replace(" ", "")
    s = re.sub(r"(?i)(ohms?|Ω|F|H|V|A|W|Hz)$", "", s)
    if not s:
        raise CalcError("enter a value")
    m = re.fullmatch(r"(\d+)([pnuµμmkKMGRr])(\d+)", s)                 # 4k7, 4R7, 2n2
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _SI[m.group(2) if m.group(2) not in "Rr" else "r"]
    m = re.fullmatch(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([fpnuµμmkKMGRr]?)", s)
    if not m:
        raise CalcError(f"can't read '{text}' - use e.g. 10k, 4k7, 100n, 0.1u, 2.2M")
    return float(m.group(1)) * _SI[m.group(2) if m.group(2) not in "Rr" else "r"]


def parse_list(text: str) -> List[float]:
    parts = [p for p in re.split(r"[,;\s]+", (text or "").strip()) if p]
    vals = [parse(p) for p in parts]
    if not vals:
        raise CalcError("enter one or more values, e.g. 10k, 4k7, 2.2k")
    return vals


def fmt(value: float, unit: str = "", digits: int = 4) -> str:
    """Engineering notation: 4700 -> '4.7 kΩ', 1e-7 -> '100 nF'."""
    if value == 0:
        return f"0 {unit}".strip()
    if math.isinf(value):
        return "∞ " + unit
    prefixes = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"),
                (1e-15, "f")]
    a = abs(value)
    for scale, p in prefixes:
        if a >= scale * 0.9995:
            break
    n = value / scale
    txt = f"{n:.{digits}g}"
    return f"{txt} {p}{unit}".strip()


# ------------------------------------------------------------------ series / parallel
def series_total(vals: List[float]) -> float:
    return sum(vals)


def parallel_total(vals: List[float]) -> float:
    if any(v <= 0 for v in vals):
        raise CalcError("values must be above 0")
    return 1.0 / sum(1.0 / v for v in vals)


def best_pairs(target: float, series: str = "E24", how: str = "series", n: int = 5) -> List[Tuple[float, float, float]]:
    """Two standard parts that combine to `target` (in series or parallel): [(a, b, result)], best first."""
    if target <= 0:
        raise CalcError("target must be above 0")
    out = []
    if how == "series":
        for a in standard_values(series, target / 100, target):
            rest = target - a
            if rest <= 0:
                continue
            b = nearest(rest, series)
            out.append((a, b, a + b))
    else:
        for a in standard_values(series, target, target * 100):
            if a <= target * (1 + 1e-9):
                continue
            b = nearest(1 / (1 / target - 1 / a), series)
            out.append((a, b, parallel_total([a, b])))
    seen, best = set(), []
    for a, b, r in sorted(out, key=lambda t: (abs(t[2] - target) / target, max(t[0], t[1]) / min(t[0], t[1]))):
        key = tuple(sorted((a, b)))
        if key not in seen:
            seen.add(key); best.append((a, b, r))
        if len(best) >= n:
            break
    return best


# ------------------------------------------------------------------ LED resistor
POWER_RATINGS = [(0.0625, "0402"), (0.1, "0603"), (0.125, "0805"), (0.25, "1206"), (0.5, "2010"), (1.0, "2512"),
                 (2.0, "through-hole 2 W"), (5.0, "through-hole 5 W")]


def resistor_rating(p_watts: float, derate: float = 2.0) -> Tuple[float, str]:
    """Smallest common rating >= derate x dissipation, with a typical SMD size for it."""
    need = p_watts * derate
    for w, pkg in POWER_RATINGS:
        if w >= need:
            return w, pkg
    return need, "power resistor"


def led_resistor(vs: float, vf: float, i_ma: float, n_series: int = 1, n_strings: int = 1,
                 series: str = "E24") -> Dict:
    """One resistor per string of n_series LEDs; n_strings identical strings in parallel."""
    if i_ma <= 0 or vs <= 0 or vf <= 0:
        raise CalcError("supply, LED voltage and current must be above 0")
    if n_series < 1 or n_strings < 1:
        raise CalcError("at least 1 LED per string and 1 string")
    v_r = vs - n_series * vf
    if v_r <= 0:
        raise CalcError(f"{n_series} LEDs x {vf:g} V = {n_series * vf:g} V - not below the {vs:g} V supply. "
                        f"Use fewer LEDs per string (max {max(int((vs - 0.01) // vf), 0)}).")
    i = i_ma / 1000
    r = v_r / i
    r_std = nearest(r, series, "up")                              # next value up: current never above target
    i_act = v_r / r_std
    p_r = i_act ** 2 * r_std
    rating, pkg = resistor_rating(p_r)
    p_led = n_series * vf * i_act
    return {"v_r": v_r, "r": r, "r_std": r_std, "i_act": i_act, "p_r": p_r, "rating": rating, "package": pkg,
            "i_total": i_act * n_strings, "p_total": vs * i_act * n_strings, "p_leds": p_led * n_strings,
            "efficiency": p_led / (vs * i_act), "headroom_warn": v_r < 0.1 * vs}


# ------------------------------------------------------------------ capacitors
def cap_units(f: float) -> Dict[str, float]:
    return {"pF": f / 1e-12, "nF": f / 1e-9, "µF": f / 1e-6, "mF": f / 1e-3, "F": f}


def cap_code(f: float) -> str:
    """EIA 3-digit code in pF (104 = 100 nF, 4R7 = 4.7 pF). '~' when the value needs more than 2 digits."""
    pf = f / 1e-12
    if pf <= 0:
        raise CalcError("value must be above 0")
    if pf < 10:
        r = round(pf, 1)
        code = f"{int(r)}R{int(round((r - int(r)) * 10))}"
        return code if abs(r - pf) < 1e-6 * max(pf, 1) else "~" + code
    exp = int(math.floor(math.log10(pf))) - 1
    sig = round(pf / 10 ** exp)
    if sig == 100:
        sig, exp = 10, exp + 1
    if exp > 9:
        raise CalcError("too large for a capacitor code")
    exact = abs(sig * 10 ** exp - pf) < 1e-6 * pf
    return ("" if exact else "~") + f"{sig:02d}{exp}"


def decode_cap_code(code: str) -> float:
    c = (code or "").strip().upper()
    m = re.fullmatch(r"(\d)R(\d)", c)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}") * 1e-12
    m = re.fullmatch(r"(\d\d)(\d)[A-Z]?", c)                     # trailing tolerance letter (J/K/M) ignored
    if not m:
        raise CalcError("a cap code looks like 104, 473K, 222 or 4R7")
    mult = int(m.group(2))
    if mult in (8, 9):                                          # EIA: 8 = x0.01, 9 = x0.1
        return int(m.group(1)) * (0.01 if mult == 8 else 0.1) * 1e-12
    return int(m.group(1)) * 10 ** mult * 1e-12


# ------------------------------------------------------------------ SMD resistor codes
EIA96_MULT = {"Z": 0.001, "Y": 0.01, "R": 0.01, "X": 0.1, "S": 0.1, "A": 1, "B": 10, "H": 10, "C": 100,
              "D": 1e3, "E": 1e4, "F": 1e5}


def decode_smd_resistor(code: str) -> Tuple[float, str]:
    """'103' (E24, 3 digits), '1002' (1 %, 4 digits), '4R7' / 'R10', '01C' (EIA-96) -> (ohms, kind)."""
    c = (code or "").strip().upper()
    if re.fullmatch(r"0+", c):
        return 0.0, "zero-ohm jumper"
    if "R" in c and re.fullmatch(r"\d*R\d*", c):
        return float(c.replace("R", ".")), "R marks the decimal point"
    m = re.fullmatch(r"(\d\d)([A-Z])", c)
    if m and m.group(2) in EIA96_MULT and 1 <= int(m.group(1)) <= 96:
        return E96[int(m.group(1)) - 1] * 100 * EIA96_MULT[m.group(2)], "EIA-96 (1 %)"
    m = re.fullmatch(r"(\d{2,3})(\d)", c)
    if m:
        return int(m.group(1)) * 10 ** int(m.group(2)), ("3-digit (5 %)" if len(c) == 3 else "4-digit (1 %)")
    raise CalcError("an SMD resistor code looks like 103, 4R7, 1002 or 01C")


def encode_smd_resistor(ohms: float) -> Dict[str, str]:
    if ohms < 0:
        raise CalcError("value must be 0 or more")
    if ohms == 0:
        return {"3-digit": "000", "4-digit": "0000", "EIA-96": "-"}

    def digits(n_sig):
        if ohms < 10 ** (n_sig - 1):                               # below 10 / 100 ohm: R notation
            s = f"{ohms:#.{n_sig}g}".rstrip(".")                    # keeps trailing zeros: 4.70 -> 4R70
            return ("" if float(s) == ohms else "~") + (s.replace(".", "R") if "." in s else s + "R")
        exp = int(math.floor(math.log10(ohms))) - (n_sig - 1)
        sig = round(ohms / 10 ** exp)
        if sig == 10 ** n_sig:
            sig, exp = 10 ** (n_sig - 1), exp + 1
        return ("" if abs(sig * 10 ** exp - ohms) < 1e-9 * ohms else "~") + f"{sig}{exp}"
    eia = "-"
    for letter, mult in (("Z", .001), ("Y", .01), ("X", .1), ("A", 1), ("B", 10), ("C", 100), ("D", 1e3),
                         ("E", 1e4), ("F", 1e5)):
        base = ohms / (100 * mult)
        for i, v in enumerate(E96):
            if abs(v - base) < 1e-6:
                eia = f"{i + 1:02d}{letter}"
    return {"3-digit": digits(2), "4-digit": digits(3), "EIA-96": eia}


# ------------------------------------------------------------------ Ohm's law
def ohms_law(v: Optional[float] = None, i: Optional[float] = None, r: Optional[float] = None,
             p: Optional[float] = None) -> Dict[str, float]:
    given = {k: x for k, x in (("v", v), ("i", i), ("r", r), ("p", p)) if x is not None}
    if len(given) != 2:
        raise CalcError("fill in exactly two of V, I, R, P")
    if "v" in given and "i" in given:
        r, p = v / i if i else math.inf, v * i
    elif "v" in given and "r" in given:
        i = v / r; p = v * i
    elif "v" in given and "p" in given:
        i = p / v; r = v / i if i else math.inf
    elif "i" in given and "r" in given:
        v = i * r; p = v * i
    elif "i" in given and "p" in given:
        v = p / i; r = v / i
    else:                                                       # r and p
        if r <= 0 or p < 0:
            raise CalcError("R must be above 0 and P not negative")
        v = math.sqrt(p * r); i = v / r
    return {"v": v, "i": i, "r": r, "p": p}


# ------------------------------------------------------------------ dividers / feedback
def divider(vin: float, r1: float, r2: float) -> Dict[str, float]:
    """R1 from Vin to the output, R2 from the output to ground."""
    if r1 < 0 or r2 < 0 or r1 + r2 == 0:
        raise CalcError("resistors must be positive")
    i = vin / (r1 + r2)
    return {"vout": vin * r2 / (r1 + r2), "i": i, "p1": i * i * r1, "p2": i * i * r2}


def best_dividers(vin: float, vout: float, series: str = "E24", r2_lo: float = 1e3, r2_hi: float = 100e3,
                  n: int = 6) -> List[Dict[str, float]]:
    """Standard R1/R2 pairs for Vout = Vin x R2 / (R1 + R2). For a regulator feedback pin use Vin = Vout
    and Vout = Vref (Vout = Vref x (1 + R1 / R2))."""
    if not 0 < vout < vin:
        raise CalcError("the output must be between 0 and the input voltage")
    out = []
    for r2 in standard_values(series, r2_lo, r2_hi):
        r1 = nearest(r2 * (vin / vout - 1), series)
        got = vin * r2 / (r1 + r2)
        out.append({"r1": r1, "r2": r2, "vout": got, "err": (got - vout) / vout, "i": vin / (r1 + r2)})
    out.sort(key=lambda d: (round(abs(d["err"]), 5), abs(math.log10(d["r2"] / 10e3))))
    return out[:n]


# ------------------------------------------------------------------ RC / LC
def rc_filter(r: float, c: float) -> Dict[str, float]:
    if r <= 0 or c <= 0:
        raise CalcError("R and C must be above 0")
    tau = r * c
    return {"tau": tau, "fc": 1 / (2 * math.pi * tau), "t99": 5 * tau}


def r_for_cutoff(fc: float, c: float) -> float:
    if fc <= 0 or c <= 0:
        raise CalcError("frequency and C must be above 0")
    return 1 / (2 * math.pi * fc * c)


def lc_resonance(l: float, c: float) -> float:
    if l <= 0 or c <= 0:
        raise CalcError("L and C must be above 0")
    return 1 / (2 * math.pi * math.sqrt(l * c))
