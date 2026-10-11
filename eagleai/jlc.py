"""JLCPCB / LCSC part cross-reference: cheapest in-stock part.

Two backends:
  * JlcSearchClient - the public jlcsearch.tscircuit.com JSON API (no key).
    Verified 2026-10-07: components/list.json returns lcsc, mfr, package,
    description, stock, price (tier string "1-499:0.0179,500-:0.0144"),
    is_basic, is_preferred; resistors/list.json filters on resistance+package.
    Verified 2026-10-08: `search` finds an exact MPN (CL10A105KB8NNNC) or an
    exact LCSC number (C97906); partial strings ("1uF 0603", a part number without its suffix)
    return unrelated rows, so results are always filtered client-side.
    capacitors/list.json filters on capacitance (in farads) + package.
    For anything these can't find, the AI does a web search (jlc_ai.py).
  * LocalParts - a CSV you export from JLCPCB's parts library (any file with
    LCSC part / MFR part / package / stock / price / basic-or-extended columns).
"""
from __future__ import annotations

import csv
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE = "https://jlcsearch.tscircuit.com"
# Assumption - JLC charges a one-off setup fee per distinct Extended part on
# assembly orders. Check their current fee and edit here.
DEFAULT_EXTENDED_FEE = 3.00


@dataclass
class Candidate:
    lcsc: str                 # "C25804"
    mfr: str
    package: str
    description: str
    stock: int
    tiers: List[Tuple[int, float]]   # (min_qty, unit_price) ascending
    basic: bool = False
    preferred: bool = False
    source: str = ""

    def unit_price(self, qty: int) -> Optional[float]:
        price = None
        for q, p in self.tiers:
            if qty >= q:
                price = p
        if price is None and self.tiers:
            price = self.tiers[0][1]
        return price

    def line_cost(self, qty: int, extended_fee: float = DEFAULT_EXTENDED_FEE) -> Optional[float]:
        up = self.unit_price(qty)
        if up is None:
            return None
        return up * qty + (0.0 if (self.basic or self.preferred) else extended_fee)

    def brief(self, qty: int = 1) -> Dict:
        return {"lcsc": self.lcsc, "mfr": self.mfr, "package": self.package,
                "stock": self.stock, "unit_price": self.unit_price(qty),
                "basic": self.basic, "preferred": self.preferred,
                "description": self.description[:90]}


def parse_tiers(price) -> List[Tuple[int, float]]:
    """'1-499:0.0179,500-2999:0.0144,6000-:0.01' or a bare number."""
    if price is None or price == "":
        return []
    if isinstance(price, (int, float)):
        return [(1, float(price))]
    s = str(price).strip()
    if re.fullmatch(r"[\d.]+", s):
        return [(1, float(s))]
    tiers = []
    for chunk in s.split(","):
        m = re.match(r"\s*(\d+)\s*-\s*\d*\s*:\s*([\d.]+)", chunk)
        if m:
            tiers.append((int(m.group(1)), float(m.group(2))))
    return sorted(tiers)


def _cand_from_api(row: Dict) -> Candidate:
    price = row.get("price") if row.get("price") is not None else row.get("price1")
    return Candidate(
        lcsc=f"C{row.get('lcsc')}", mfr=str(row.get("mfr", "")), package=str(row.get("package", "")),
        description=str(row.get("description", "")), stock=int(row.get("stock") or 0),
        tiers=parse_tiers(price), basic=bool(row.get("is_basic")),
        preferred=bool(row.get("is_preferred")), source="jlcsearch")


def rank(cands: List[Candidate], qty: int = 1, require_stock: bool = True,
         extended_fee: float = DEFAULT_EXTENDED_FEE, min_stock_factor: float = 1.0) -> List[Candidate]:
    """Cheapest first, among parts with enough stock for `qty` (x factor)."""
    need = int(qty * min_stock_factor)
    ok = [c for c in cands if c.tiers and (not require_stock or c.stock >= max(need, 1))]
    ok.sort(key=lambda c: (c.line_cost(qty, extended_fee) or 1e9, -c.stock))
    return ok


class JlcSearchClient:
    def __init__(self, base: str = BASE, timeout: float = 20.0):
        self.base, self.timeout = base.rstrip("/"), timeout

    def _get(self, path: str, **params) -> Dict:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(f"{self.base}/{path}?{q}", headers={"User-Agent": "eagleai-ide"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError(f"JLC lookup failed: {exc}") from exc

    def find(self, query: str, limit: int = 100) -> List[Candidate]:
        """Free-text. Fetches a page and filters client-side on every token."""
        data = self._get("components/list.json", search=query, limit=limit)
        rows = data.get("components", [])
        toks = [t.upper() for t in re.split(r"[\s,]+", query) if t]
        out = []
        for r in rows:
            hay = f"{r.get('mfr', '')} {r.get('package', '')} {r.get('description', '')} C{r.get('lcsc', '')}".upper()
            if all(t in hay for t in toks):
                out.append(_cand_from_api(r))
        return out

    def lookup(self, lcsc: str) -> Optional[Candidate]:
        """One part by LCSC number (C12345) - None if JLC doesn't list it."""
        num = re.sub(r"\D", "", lcsc or "")
        if not num:
            return None
        data = self._get("components/list.json", search=f"C{num}", limit=5)
        for r in data.get("components", []):
            if str(r.get("lcsc")) == num:
                return _cand_from_api(r)
        return None

    def by_mpn(self, mpn: str, limit: int = 50) -> List[Candidate]:
        """Exact manufacturer part number (the API only matches whole MPNs)."""
        want = _norm(mpn)
        if len(want) < 3:
            return []
        data = self._get("components/list.json", search=mpn, limit=limit)
        return [_cand_from_api(r) for r in data.get("components", []) if want in _norm(r.get("mfr", ""))]

    def capacitors(self, farads: float, package: str, limit: int = 100) -> List[Candidate]:
        data = self._get("capacitors/list.json", capacitance=f"{farads:.4g}", package=package, limit=limit)
        return [_cand_from_api(r) for r in data.get("capacitors", [])]

    def resistors(self, ohms: float, package: str, limit: int = 100) -> List[Candidate]:
        data = self._get("resistors/list.json", resistance=ohms, package=package, limit=limit)
        return [_cand_from_api(r) for r in data.get("resistors", [])]

    def category(self, category: str, limit: int = 100, **params) -> List[Candidate]:
        """Other category endpoints (capacitors, leds, ...) - same pattern as
        resistors but parameter names are unverified; see module docstring."""
        data = self._get(f"{category}/list.json", limit=limit, **params)
        rows = data.get(category) or next((v for v in data.values() if isinstance(v, list)), [])
        return [_cand_from_api(r) for r in rows]


class LocalParts:
    """CSV exported from JLCPCB's parts library; header names matched loosely."""

    ALIASES = {
        "lcsc": ("lcsc part", "lcsc part #", "lcsc", "jlcpcb part #", "part #"),
        "mfr": ("mfr.part", "mfr part", "mfr", "manufacturer part"),
        "package": ("package",),
        "description": ("description",),
        "stock": ("stock",),
        "price": ("price",),
        "type": ("library type", "type"),
    }

    def __init__(self, csv_path: Path):
        self.rows: List[Candidate] = []
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = {h.lower().strip(): h for h in (reader.fieldnames or [])}
            pick = {k: next((cols[a] for a in al if a in cols), None) for k, al in self.ALIASES.items()}
            for r in reader:
                lc = (r.get(pick["lcsc"]) or "").strip() if pick["lcsc"] else ""
                if not lc:
                    continue
                typ = (r.get(pick["type"]) or "").lower() if pick["type"] else ""
                try:
                    stock = int(float((r.get(pick["stock"]) or "0").replace(",", "")))
                except ValueError:
                    stock = 0
                self.rows.append(Candidate(
                    lcsc=lc if lc.upper().startswith("C") else f"C{lc}",
                    mfr=(r.get(pick["mfr"]) or "") if pick["mfr"] else "",
                    package=(r.get(pick["package"]) or "") if pick["package"] else "",
                    description=(r.get(pick["description"]) or "") if pick["description"] else "",
                    stock=stock, tiers=parse_tiers(r.get(pick["price"]) if pick["price"] else None),
                    basic="basic" in typ, preferred="prefer" in typ, source="csv"))

    def lookup(self, lcsc: str) -> Optional[Candidate]:
        num = re.sub(r"\D", "", lcsc or "")
        return next((c for c in self.rows if re.sub(r"\D", "", c.lcsc) == num), None)

    def by_mpn(self, mpn: str, limit: int = 50) -> List[Candidate]:
        want = _norm(mpn)
        return [c for c in self.rows if want and want in _norm(c.mfr)][:limit]

    def find(self, query: str, limit: int = 100) -> List[Candidate]:
        toks = [t.upper() for t in re.split(r"[\s,]+", query) if t]
        out = [c for c in self.rows
               if all(t in f"{c.mfr} {c.package} {c.description} {c.lcsc}".upper() for t in toks)]
        return out[:limit]


# ---------------------------------------------------------------- helpers
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9}


def parse_value(text: str) -> Optional[float]:
    """'10k' -> 10000, '4k7' -> 4700, '100nF' -> 1e-7, '2.2M' -> 2.2e6."""
    s = (text or "").strip().replace("Ω", "").replace("ohm", "").replace("F", "").replace("H", "")
    m = re.fullmatch(r"(\d+)([pnuµmkMG])(\d+)", s)
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _SI[m.group(2)]
    m = re.fullmatch(r"([\d.]+)\s*([pnuµmkMG]?)", s)
    if not m:
        return None
    return float(m.group(1)) * (_SI[m.group(2)] if m.group(2) else 1.0)


def footprint_size(package_name: str) -> Optional[str]:
    m = re.search(r"(0201|0402|0603|0805|1206|1210|2010|2512)", package_name or "")
    return m.group(1) if m else None


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def rated_volts(text: str) -> Optional[float]:
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*V(?:DC)?\b", text or "", re.I)
    return float(m.group(1)) if m else None


def mpn_variants(name: str) -> List[str]:
    """'L7805ABD2T-TR' -> ['L7805ABD2T-TR', 'L7805ABD2T'];  'IRLML2502TRPBF' -> [.., 'IRLML2502']."""
    n = (name or "").strip()
    out = [n]
    for pat in (r"[-_/]?(TR|T&R|REEL|TAPE)$", r"(TR)?PBF$", r"\(.*\)$", r"[-_/]?TR\d*$"):
        v = re.sub(pat, "", out[-1], flags=re.I)
        if v and v != out[-1]:
            out.append(v)
    return list(dict.fromkeys(o for o in out if len(_norm(o)) >= 4))


def _looks_like_mpn(s: str) -> bool:
    s = (s or "").strip()
    return bool(re.search(r"[A-Za-z]", s) and re.search(r"\d", s) and len(_norm(s)) >= 5
                and not re.fullmatch(r"[\d.,]+\s*[pnuµmkMG]?\s*[FHΩRVA]?.*", s))


def best_for_part(client, prefix: str, value: str, mpn: str, package: str,
                  qty: int = 1, extended_fee: float = DEFAULT_EXTENDED_FEE,
                  names: Tuple[str, ...] = (), note: str = "",
                  min_volts: Optional[float] = None) -> Tuple[List[Candidate], str]:
    """Pick candidates for one BOM line without AI. Returns (ranked, how).
    names = library deviceset/device names, tried as part numbers too. min_volts = the recommended capacitor rating
    worked out from the circuit (ratings.py); the voltage written in the value/note wins if it is higher."""
    size = footprint_size(package)
    v = (value or "").replace("µ", "u").replace("μ", "u")
    if prefix == "R" and hasattr(client, "resistors"):
        ohms = parse_value(v)
        if ohms is not None and size:
            return rank(client.resistors(ohms, size), qty, extended_fee=extended_fee), f"resistor {value} {size}"
    if prefix == "C" and hasattr(client, "capacitors") and size:
        far = parse_value(re.split(r"\s", v)[0] if v else "")
        if far:
            need = max(rated_volts(f"{value} {note}") or 0, min_volts or 0) or 16.0   # unknown: at least 16 V
            cands = [c for c in client.capacitors(far, size) if (rated_volts(c.description) or 0) >= need]
            return rank(cands, qty, extended_fee=extended_fee), f"capacitor {value} {size} >= {need:g}V"
    tried = []
    for name in [mpn, value] + list(names):
        if not _looks_like_mpn(name):
            continue
        for q in mpn_variants(name):
            if q.upper() in tried:
                continue
            tried.append(q.upper())
            hits = client.by_mpn(q) if hasattr(client, "by_mpn") else client.find(q)
            ranked = rank(hits, qty, extended_fee=extended_fee)
            if ranked:
                return ranked, f"part number '{q}'"
    if tried:
        return [], "part numbers tried: " + ", ".join(tried) + " (not listed - try 'Find missing with AI')"
    return [], "no part number to search - try 'Find missing with AI'"
