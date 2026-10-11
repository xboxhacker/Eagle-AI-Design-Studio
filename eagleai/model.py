"""Design model (JSON on disk) + validation against the real library pins."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .library import Device, LibraryIndex

POWER_DIRS = ("pwr", "sup")


@dataclass
class PartSpec:
    ref: str
    library: str
    deviceset: str
    device: str = ""
    value: str = ""
    mpn: str = ""
    lcsc: str = ""            # JLC/LCSC part number, e.g. C25804
    note: str = ""
    technology: str = ""      # Eagle technology variant ('' = library default)
    dnp: bool = False         # do not populate (kept in the schematic, left out of assembly files)
    stress: Dict = field(default_factory=dict)   # worst case it sees: {"V": across, "W": dissipated, "A": through, "why"}


@dataclass
class Problem:
    level: str    # "error" | "warn" | "info"
    where: str
    msg: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}: {self.msg}"


@dataclass
class Design:
    name: str = "untitled"
    parts: Dict[str, PartSpec] = field(default_factory=dict)
    nets: Dict[str, List[str]] = field(default_factory=dict)   # net -> ["U1.VCC", ...]
    notes: str = ""
    # prose for the Markdown document: title, subtitle, summary [..], citations {ref: text}, sections {ref: name}
    doc: Dict = field(default_factory=dict)
    net_volts: Dict[str, float] = field(default_factory=dict)  # worst-case max voltage of a net (for part ratings)

    # ------------------------------------------------------------------ io
    def to_json(self) -> str:
        parts = []
        for p in self.parts.values():
            d = asdict(p)
            if not d["stress"]:
                del d["stress"]
            parts.append(d)
        return json.dumps({
            "name": self.name, "notes": self.notes,
            "parts": parts,
            "nets": self.nets,
            **({"net_volts": self.net_volts} if self.net_volts else {}),
            **({"doc": self.doc} if self.doc else {}),
        }, indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Design":
        d = json.loads(text)
        out = cls(name=d.get("name", "untitled"), notes=d.get("notes", ""))
        for p in d.get("parts", []):
            ps = PartSpec(**{k: v for k, v in p.items() if k in PartSpec.__dataclass_fields__})
            out.parts[ps.ref] = ps
        out.nets = {k: list(v) for k, v in d.get("nets", {}).items()}
        out.doc = dict(d.get("doc") or {})
        out.net_volts = {k: float(v) for k, v in (d.get("net_volts") or {}).items()}
        return out

    def save(self, path: Path):
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Design":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    # --------------------------------------------------------------- edits
    def next_ref(self, prefix: str) -> str:
        n = 1
        while f"{prefix}{n}" in self.parts:
            n += 1
        return f"{prefix}{n}"

    def add_part(self, index: LibraryIndex, library: str, deviceset: str,
                 device: Optional[str] = None, ref: Optional[str] = None,
                 value: str = "", mpn: str = "") -> Tuple[Optional[PartSpec], Optional[str]]:
        dev, err = index.find_device(library, deviceset, device)
        if dev is None:
            return None, err
        prefix = re.sub(r"[^A-Z]", "", (dev.prefix or "U").upper()) or "U"
        ref = (ref or self.next_ref(prefix)).upper()
        if ref in self.parts:
            return None, f"reference {ref} already exists"
        ps = PartSpec(ref, library, deviceset, dev.device, value, mpn)
        self.parts[ref] = ps
        return ps, None

    def remove_part(self, ref: str) -> Optional[str]:
        ref = ref.upper()
        if ref not in self.parts:
            return f"no part {ref}"
        del self.parts[ref]
        for net in list(self.nets):
            self.nets[net] = [p for p in self.nets[net] if p.split(".", 1)[0].upper() != ref]
            if not self.nets[net]:
                del self.nets[net]
        return None

    def device_for(self, index: LibraryIndex, ref: str) -> Tuple[Optional[Device], Optional[str]]:
        p = self.parts.get(ref.upper())
        if p is None:
            return None, f"no part {ref}"
        return index.find_device(p.library, p.deviceset, p.device)

    def connect(self, index: LibraryIndex, net: str, pins: List[str]) -> Tuple[List[str], List[str]]:
        """Add pins to a net. Each pin is validated against the real symbol.

        Returns (added, errors). Nothing is added for a pin that errors.
        """
        net = re.sub(r"\s+", "_", net.strip())
        added, errors = [], []
        for tok in pins:
            if "." not in tok:
                errors.append(f"'{tok}': use REF.PIN")
                continue
            ref, pin = tok.split(".", 1)
            ref = ref.upper()
            dev, err = self.device_for(index, ref)
            if dev is None:
                errors.append(f"{tok}: {err}")
                continue
            found, perr = dev.resolve(pin)
            if perr:
                errors.append(f"{tok}: {perr}")
                continue
            owner = self._net_of(dev, ref, found[0])
            if owner and owner != net:
                errors.append(f"{tok}: already on net '{owner}' (disconnect it first)")
                continue
            canonical = f"{ref}.{found[0].name}" if len(found) == 1 else f"{ref}.{pin}"
            lst = self.nets.setdefault(net, [])
            if canonical not in lst:
                lst.append(canonical)
            added.append(canonical)
        if net in self.nets and not self.nets[net]:
            del self.nets[net]
        return added, errors

    def disconnect(self, token: str) -> int:
        n = 0
        for net in list(self.nets):
            keep = [p for p in self.nets[net] if p.upper() != token.upper()]
            n += len(self.nets[net]) - len(keep)
            self.nets[net] = keep
            if not keep:
                del self.nets[net]
        return n

    def _net_of(self, dev: Device, ref: str, pin) -> Optional[str]:
        for net, members in self.nets.items():
            for m in members:
                r, p = m.split(".", 1)
                if r.upper() != ref:
                    continue
                f, _ = dev.resolve(p)
                if f and any(x.key() == pin.key() for x in f):
                    return net
        return None

    # ---------------------------------------------------------- validation
    def validate(self, index: LibraryIndex) -> List[Problem]:
        probs: List[Problem] = []
        devs: Dict[str, Device] = {}
        for ref, p in self.parts.items():
            dev, err = index.find_device(p.library, p.deviceset, p.device)
            if dev is None:
                probs.append(Problem("error", ref, f"library part missing: {err}"))
                continue
            devs[ref] = dev
            if p.library in index.unverified:
                probs.append(Problem("warn", ref, f"{p.library} was generated from a datasheet — verify the footprint before ordering boards"))
            if dev.prefix.upper() in ("R", "C", "L") and not p.value:
                probs.append(Problem("warn", ref, "passive has no value"))

        used: Dict[Tuple[str, Tuple[str, str]], str] = {}
        for net, members in self.nets.items():
            if len(members) < 2:
                probs.append(Problem("warn", net, f"net has only one pin ({members[0] if members else 'none'})"))
            for m in members:
                ref, pin = m.split(".", 1)
                dev = devs.get(ref.upper())
                if dev is None:
                    probs.append(Problem("error", net, f"{m}: part {ref} is not in the design/library"))
                    continue
                found, perr = dev.resolve(pin)
                if perr:
                    probs.append(Problem("error", net, f"{m}: {perr}"))
                    continue
                for f in found:
                    k = (ref.upper(), f.key())
                    if k in used and used[k] != net:
                        probs.append(Problem("error", m, f"pin is on both '{used[k]}' and '{net}' — that is a short"))
                    used[k] = net

        for ref, dev in devs.items():
            for pin in dev.pins:
                if pin.direction in POWER_DIRS and (ref, pin.key()) not in used:
                    probs.append(Problem("warn", f"{ref}.{pin.name}", "power pin not connected to any net"))
            if dev.prefix.upper() in ("R", "C", "L") and len(dev.pins) == 2:
                a, b = (used.get((ref, p.key())) for p in dev.pins)
                if a and a == b and not re.match(r"^0\s*(R|ohm|Ω)?$", self.parts[ref].value, re.I):
                    probs.append(Problem("error", ref, f"both pins are on net '{a}' — the part is shorted"))
        return probs

    def bom(self) -> List[Dict]:
        """Group identical parts for the BOM table (always computed from the live design)."""
        groups: Dict[Tuple, List[str]] = {}
        for ref, p in sorted(self.parts.items(), key=lambda kv: (re.sub(r"\d", "", kv[0]), int(re.sub(r"\D", "", kv[0]) or 0))):
            groups.setdefault((p.library, p.deviceset, p.device, p.value, p.mpn, p.lcsc, p.note, p.dnp), []).append(ref)
        return [{"refs": ", ".join(refs), "qty": len(refs), "library": k[0], "deviceset": k[1],
                 "device": k[2], "value": k[3], "mpn": k[4], "lcsc": k[5], "note": k[6], "dnp": k[7]}
                for k, refs in groups.items()]
