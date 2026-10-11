"""Decisions made in code instead of by Claude (zero tokens).

* PartMemory  - remembers which library device you/Claude chose for an MPN or a
                passive size, across all projects. Next time it is reused locally.
* pick_passive - R/C/L by package size: reuse what the design already uses, then
                part memory, then a scored library search.
* match_bom_row - for imports: memory hit, passive rule, or an UNAMBIGUOUS
                part-number match. Anything unclear is left for Claude, so quality
                does not drop - Claude just stops doing the obvious ones.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .jlc import footprint_size
from .library import LibraryIndex, norm

PASSIVE = ("R", "C", "L")


def clean_value(v: str) -> str:
    """'10kΩ' -> '10k', '4.7µF' -> '4.7uF' (Eagle-friendly, still readable)."""
    v = (v or "").strip().replace("Ω", "").replace("µ", "u").replace("μ", "u")
    return re.sub(r"\s*ohms?$", "", v, flags=re.I).strip()


def ref_prefix(ref: str) -> str:
    return re.sub(r"[^A-Z]", "", ref.upper())


class PartMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data: Dict[str, List[str]] = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}

    @staticmethod
    def key_mpn(mpn: str) -> str:
        return "MPN:" + norm(mpn)

    @staticmethod
    def key_passive(prefix: str, size: str, polarized: bool = False) -> str:
        return f"PASSIVE:{prefix}:{size}:{'pol' if polarized else 'np'}"

    def get(self, key: str) -> Optional[Tuple[str, str, str]]:
        v = self.data.get(key)
        return tuple(v) if v else None

    def put(self, key: str, lib: str, ds: str, dev: str):
        if not key or key.endswith(":"):
            return
        self.data[key] = [lib, ds, dev]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        except OSError:
            pass

    def learn(self, index: LibraryIndex, lib: str, ds: str, dev: str, mpn: str = "", value: str = ""):
        """Called whenever a part is placed - by Claude or by code."""
        d, _ = index.find_device(lib, ds, dev)
        if d is None:
            return
        if mpn and norm(mpn) not in ("GENERIC", ""):
            self.put(self.key_mpn(mpn), lib, ds, d.device)
        pre = ref_prefix(d.prefix)
        size = footprint_size(d.package)
        if pre in PASSIVE and size:
            self.put(self.key_passive(pre, size, _is_polarized(ds, d.package)), lib, ds, d.device)


def _is_polarized(ds: str, pkg: str) -> bool:
    return bool(re.search(r"POL|ELKO|ELEC|TANT", f"{ds} {pkg}", re.I))


def pick_passive(index: LibraryIndex, prefix: str, package: str, polarized: bool = False,
                 memory: Optional[PartMemory] = None, design=None, style: str = "US") -> Optional[Tuple[str, str, str]]:
    prefix = prefix.upper()
    size = footprint_size(package) or norm(package)
    if not size:
        return None
    # 1) what this design already uses for the same kind+size (keeps the schematic consistent)
    if design is not None:
        for ps in design.parts.values():
            if ref_prefix(ps.ref) == prefix:
                d, _ = index.find_device(ps.library, ps.deviceset, ps.device)
                if d and size in norm(d.package) and _is_polarized(d.deviceset, d.package) == polarized:
                    return ps.library, ps.deviceset, ps.device
    # 2) remembered choice
    if memory:
        hit = memory.get(PartMemory.key_passive(prefix, size, polarized))
        if hit and index.find_device(*hit)[0]:
            return hit
    # 3) scored library search
    best, best_s = None, -1e9
    for e in index.all_entries():
        if ref_prefix(e["prefix"]) != prefix or size not in norm(e["package"]):
            continue
        ds = e["deviceset"].upper()
        if _is_polarized(ds, e["package"]) != polarized:
            continue
        s = 0.0
        if e["library"].lower() == "rcl":
            s += 20
        if ds.startswith(f"{prefix}-{style}"):
            s += 15
        elif ds.startswith(f"{prefix}-"):
            s += 8
        if norm(e["package"]) in (f"{prefix}{size}", size, f"{prefix}{size}K"):
            s += 10
        s -= len(e["package"]) * 0.2 + len(ds) * 0.1
        if s > best_s:
            best, best_s = (e["library"], e["deviceset"], e["device"]), s
    return best


def match_bom_row(row: Dict, index: LibraryIndex, memory: Optional[PartMemory], design=None,
                  style: str = "US") -> Tuple[Optional[Tuple[str, str, str]], str]:
    """Returns ((lib, deviceset, device) or None, how)."""
    refs = row["refs"]
    prefix = ref_prefix(refs[0])
    mpn = row.get("part_number", "")
    if memory and mpn and norm(mpn) != "GENERIC":
        hit = memory.get(PartMemory.key_mpn(mpn))
        if hit and index.find_device(*hit)[0]:
            return hit, "remembered"
    if prefix in PASSIVE:
        pol = bool(re.search(r"electrolytic|polar|tantal", row.get("description", ""), re.I))
        hit = pick_passive(index, prefix, row.get("package", ""), pol, memory, design, style)
        if hit:
            return hit, "passive rule"
        return None, "no passive of that size found"
    if not mpn or norm(mpn) == "GENERIC":
        return None, "no part number"
    m = norm(mpn)
    cands = [e for e in index.all_entries()
             if len(norm(e["deviceset"])) >= 4 and m.startswith(norm(e["deviceset"]).replace("*", ""))]
    if not cands:
        return None, "no library deviceset matches the part number"
    longest = max(len(norm(e["deviceset"])) for e in cands)
    cands = [e for e in cands if len(norm(e["deviceset"])) == longest]
    pkg = norm(row.get("package", ""))
    if pkg:
        by_pkg = [e for e in cands if pkg and (pkg in norm(e["package"]) or norm(e["package"]) in pkg)]
        if by_pkg:
            cands = by_pkg
    # also accept when device suffix completes the MPN exactly (LM317 + T = LM317T)
    exact = [e for e in cands if norm(e["deviceset"] + e["device"]) == m]
    if len(exact) == 1:
        cands = exact
    if len({(e["library"], e["deviceset"], e["device"]) for e in cands}) == 1:
        e = cands[0]
        return (e["library"], e["deviceset"], e["device"]), "part-number match"
    return None, f"{len(cands)} possible library parts - needs judgement"
