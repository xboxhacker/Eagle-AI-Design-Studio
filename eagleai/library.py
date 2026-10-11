"""Eagle .lbr index: fast scan for search, lazy deep parse for pin data.

Pins are always read from the real library XML, so the AI can never invent them.
"""
from __future__ import annotations

import copy
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

CACHE_VERSION = 4


def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def pin_norm(s: str) -> str:
    """Normalise a pin name: drop @n suffix, overbar ~, punctuation, case."""
    s = re.sub(r"@\d+$", "", (s or "").strip())
    return re.sub(r"[^A-Z0-9+\-]", "", s.upper().replace("~", ""))


@dataclass
class Pin:
    name: str
    pad: str          # may hold several space-separated pads
    gate: str
    x: float
    y: float
    rot: str = "R0"
    length: str = "long"
    direction: str = "io"

    @property
    def pads(self) -> List[str]:
        return self.pad.split()

    def key(self) -> Tuple[str, str]:
        return (self.gate, self.name)

    def label(self) -> str:
        return f"{self.name} (pad {self.pad}, gate {self.gate})"


@dataclass
class Gate:
    name: str
    symbol: str
    x: float
    y: float
    addlevel: str = "next"


@dataclass
class Device:
    library: str
    deviceset: str
    device: str
    package: str
    prefix: str
    description: str
    uservalue: bool
    gates: List[Gate]
    pins: List[Pin]
    symbols: Dict[str, ET.Element]
    technology: Optional[str] = None

    @property
    def id(self) -> str:
        return f"{self.library}/{self.deviceset}/{self.device}"

    def resolve(self, token: str) -> Tuple[List[Pin], Optional[str]]:
        """Resolve a pin token (name or pad number) to library pins.

        Returns (pins, error). More than one pin only when they are the same
        logical pin split by Eagle's @n suffix (e.g. GND@1, GND@2).
        """
        t = (token or "").strip()
        if not t:
            return [], "empty pin"
        tiers = [
            [p for p in self.pins if p.name == t],
            [p for p in self.pins if p.name.upper() == t.upper()],
            [p for p in self.pins if t in p.pads],
            [p for p in self.pins if pin_norm(p.name) == pin_norm(t)],
        ]
        for found in tiers:
            if not found:
                continue
            if len(found) == 1:
                return found, None
            if all("@" in p.name for p in found) and len({pin_norm(p.name) for p in found}) == 1:
                return found, None
            opts = ", ".join(p.label() for p in found)
            return [], f"pin '{t}' is ambiguous on {self.id}: {opts}"
        return [], f"no pin '{t}' on {self.id}. Valid pins: {self.pin_summary()}"

    def pin_summary(self, limit: int = 80) -> str:
        parts = [f"{p.name}[{p.pad}]" for p in self.pins[:limit]]
        more = f" …(+{len(self.pins) - limit})" if len(self.pins) > limit else ""
        return ", ".join(parts) + more


class LibraryIndex:
    def __init__(self, dirs: List[Path]):
        self.dirs = [Path(d) for d in dirs]
        self.entries: List[Dict] = []
        self._files: Dict[str, Path] = {}          # lib name -> path
        self._roots: Dict[str, ET.Element] = {}    # parsed lbr roots (lazy)
        self._dev_cache: Dict[Tuple[str, str, str], Device] = {}
        self._embedded: Dict[str, Tuple[ET.Element, Optional[ET.Element]]] = {}   # from an imported .sch
        self._embedded_entries: List[Dict] = []
        self._merged: Dict[str, ET.Element] = {}
        self.unverified: set = set()               # libs generated from datasheets
        self.duplicates: List[str] = []            # same library name found twice
        self.skipped: List[str] = []               # unreadable .lbr files

    # ------------------------------------------------------------------ scan
    def lbr_files(self) -> List[Path]:
        """Every .lbr under every library folder, including all sub-folders.
        Unreadable folders are skipped instead of aborting the walk."""
        out: List[Path] = []
        seen = set()
        for d in self.dirs:
            if not d.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(d, onerror=lambda e: None):
                dirnames[:] = sorted(n for n in dirnames if not n.startswith(".") and n.lower() not in ("history", "__pycache__", "fusion360", "eagle_script", "_unzip"))
                for fn in sorted(filenames):
                    if fn.lower().endswith(".lbr"):
                        f = Path(dirpath) / fn
                        key = os.path.normcase(str(f.resolve()))
                        if key not in seen:
                            seen.add(key)
                            out.append(f)
        return out

    def _meta(self, files: List[Path]) -> List[Dict]:
        m = []
        for f in files:
            try:
                st = f.stat()
                m.append({"path": str(f), "mtime": st.st_mtime, "size": st.st_size})
            except OSError:
                pass
        return m

    def scan(self, progress: Optional[Callable[[int, int, str], None]] = None) -> int:
        files = self.lbr_files()
        self.entries, self._files, self.duplicates, self.skipped = [], {}, [], []
        self._roots.clear()
        self._dev_cache.clear()
        self._merged.clear()
        for i, f in enumerate(files):
            if progress:
                progress(i + 1, len(files), f.name)
            if f.stem in self._files:          # first folder in the list wins
                self.duplicates.append(f"{f} (using {self._files[f.stem]})")
                continue
            try:
                self._scan_one(f)
            except Exception as exc:  # a broken lbr must not kill the scan
                self.skipped.append({"path": str(f), "name": f.stem, "kind": classify_bad_lbr(f), "error": str(exc)})
        for sk in self.skipped:          # an XML copy of the same library elsewhere covers it
            if sk["name"] in self._files:
                sk["covered_by"] = str(self._files[sk["name"]])
        return len(self.entries)

    def problem_libs(self) -> List[Dict]:
        return [s for s in self.skipped if isinstance(s, dict)]

    @staticmethod
    def _entries_from(libel: ET.Element, lib: str, source: str) -> List[Dict]:
        out = []
        for ds in libel.iter("deviceset"):
            desc_el = ds.find("description")
            desc = re.sub(r"<[^>]+>", " ", desc_el.text or "") if desc_el is not None else ""
            for dev in ds.findall("devices/device"):
                out.append({
                    "library": lib,
                    "deviceset": ds.get("name", ""),
                    "device": dev.get("name", ""),
                    "package": dev.get("package", ""),
                    "prefix": ds.get("prefix", ""),
                    "description": " ".join(desc.split())[:200],
                    "source": source,
                })
        return out

    def _scan_one(self, path: Path):
        lib = path.stem
        root = ET.parse(path).getroot()
        self._files[lib] = path
        self.entries.extend(self._entries_from(root, lib, str(path)))

    def add_file(self, path: Path, unverified: bool = False):
        """Index one new/changed lbr (used after the LBR builder writes one)."""
        lib = path.stem
        self.entries = [e for e in self.entries if e["library"] != lib]
        self._roots.pop(lib, None)
        self._merged.pop(lib, None)
        for k in [k for k in self._dev_cache if k[0] == lib]:
            self._dev_cache.pop(k)
        self._scan_one(path)
        if unverified:
            self.unverified.add(lib)

    def add_embedded(self, lib: str, libel: ET.Element, layers: Optional[ET.Element], source: str):
        """Register a library copy embedded in a .sch. Devices in it take priority
        over the installed .lbr of the same name, so an imported design keeps
        exactly the pins Eagle has in that file."""
        self._embedded[lib] = (copy.deepcopy(libel), copy.deepcopy(layers) if layers is not None else None)
        self._embedded_entries = [e for e in self._embedded_entries if e["library"] != lib]
        self._embedded_entries.extend(self._entries_from(libel, lib, source))
        self._merged.pop(lib, None)
        for k in [k for k in self._dev_cache if k[0] == lib]:
            self._dev_cache.pop(k)

    def all_entries(self) -> List[Dict]:
        seen = {(e["library"], e["deviceset"], e["device"]) for e in self._embedded_entries}
        return self._embedded_entries + [e for e in self.entries
                                         if (e["library"], e["deviceset"], e["device"]) not in seen]

    def stats(self) -> str:
        libs = len(self._files)
        msg = f"{libs} libraries, {len(self.entries)} devices"
        if self._embedded:
            msg += f", {len(self._embedded)} libraries embedded in the imported .sch"
        if self.duplicates:
            msg += f", {len(self.duplicates)} duplicate names ignored"
        bad = [s for s in self.problem_libs() if not s.get("covered_by")]
        if bad:
            msg += f", {len(bad)} libraries need converting (see Libraries tab)"
        return msg

    # ----------------------------------------------------------------- cache
    def load_cache(self, cache: Path) -> Tuple[bool, str]:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"no cache ({exc})"
        if data.get("version") != CACHE_VERSION:
            return False, "cache version changed"
        files = self.lbr_files()
        if self._meta(files) != data.get("files"):
            return False, "libraries changed since cache was built"
        self.entries = data["entries"]
        self._files = {}
        for f in files:
            self._files.setdefault(f.stem, f)
        self.duplicates = data.get("duplicates", [])
        self.skipped = data.get("skipped", [])
        return True, f"{len(self.entries)} devices from cache"

    def save_cache(self, cache: Path):
        files = self.lbr_files()
        cache.write_text(json.dumps({
            "version": CACHE_VERSION, "files": self._meta(files), "entries": self.entries,
            "duplicates": self.duplicates, "skipped": self.skipped,
        }), encoding="utf-8")

    # ---------------------------------------------------------------- search
    def search(self, query: str, prefix: Optional[str] = None, limit: int = 20) -> List[Dict]:
        toks = [norm(t) for t in re.split(r"[\s,;]+", query or "") if norm(t)]
        if not toks:
            return []
        scored = []
        for e in self.all_entries():
            if prefix and e["prefix"].upper() != prefix.upper():
                continue
            ds, dev, pkg = norm(e["deviceset"]), norm(e["device"]), norm(e["package"])
            hay = " ".join([ds, dev, pkg, norm(e["library"]), norm(e["description"])])
            if not all(t in hay for t in toks):
                continue
            s = 0
            for t in toks:
                if ds == t:
                    s += 100
                elif ds.startswith(t):
                    s += 40
                elif t in ds:
                    s += 25
                if t in pkg or t in dev:
                    s += 8
            s -= len(ds) * 0.1
            scored.append((s, e))
        scored.sort(key=lambda p: -p[0])
        return [e for _, e in scored[:limit]]

    # ------------------------------------------------------------- deep data
    def _root(self, lib: str) -> ET.Element:
        if lib not in self._roots:
            path = self._files.get(lib)
            if path is None:
                raise KeyError(f"library '{lib}' not found")
            self._roots[lib] = ET.parse(path).getroot()
        return self._roots[lib]

    def library_element(self, lib: str) -> ET.Element:
        """The library as Eagle will see it: the .sch-embedded copy first, plus
        anything only the installed .lbr has (added by name)."""
        if lib in self._merged:
            return self._merged[lib]
        installed = None
        if lib in self._files:
            installed = self._root(lib).find(".//library")
        emb = self._embedded.get(lib, (None, None))[0]
        if emb is None and installed is None:
            raise KeyError(f"library '{lib}' not found")
        if emb is None:
            out = installed
        else:
            out = copy.deepcopy(emb)
            if installed is not None:
                for section, child in (("packages", "package"), ("symbols", "symbol"), ("devicesets", "deviceset")):
                    dst = out.find(section)
                    if dst is None:
                        dst = ET.SubElement(out, section)
                    have = {c.get("name") for c in dst.findall(child)}
                    src = installed.find(section)
                    for c in (src.findall(child) if src is not None else []):
                        if c.get("name") not in have:
                            dst.append(copy.deepcopy(c))
        self._merged[lib] = out
        return out

    def lib_version(self, lib: str) -> str:
        if lib in self._files:
            return self._root(lib).get("version", "")
        return getattr(self, "_embedded_versions", {}).get(lib, "7")

    def layers_element(self, lib: str) -> Optional[ET.Element]:
        if lib in self._files:
            return self._root(lib).find(".//layers")
        return self._embedded.get(lib, (None, None))[1]

    def get_device(self, lib: str, deviceset: str, device: str = "") -> Device:
        key = (lib, deviceset, device)
        if key in self._dev_cache:
            return self._dev_cache[key]
        libel = self.library_element(lib)
        ds = next((d for d in libel.iter("deviceset") if d.get("name") == deviceset), None)
        if ds is None:
            raise KeyError(f"deviceset '{deviceset}' not in {lib}")
        dev = next((d for d in ds.findall("devices/device") if d.get("name", "") == device), None)
        if dev is None:
            names = [d.get("name", "") for d in ds.findall("devices/device")]
            raise KeyError(f"device '{device}' not in {lib}/{deviceset}. Variants: {names}")
        symbols = {s.get("name"): s for s in libel.iter("symbol")}
        gates = [Gate(g.get("name", "G$1"), g.get("symbol", ""), float(g.get("x", 0)),
                      float(g.get("y", 0)), g.get("addlevel", "next"))
                 for g in ds.findall("gates/gate")]
        gate_sym = {g.name: g.symbol for g in gates}
        pins: List[Pin] = []
        for c in dev.findall("connects/connect"):
            gate, pname, pad = c.get("gate", ""), c.get("pin", ""), c.get("pad", "")
            sym = symbols.get(gate_sym.get(gate, ""))
            if sym is None:
                continue
            sp = next((p for p in sym.findall("pin") if p.get("name") == pname), None)
            if sp is None:
                continue
            pins.append(Pin(pname, pad, gate, float(sp.get("x", 0)), float(sp.get("y", 0)),
                            sp.get("rot", "R0") or "R0", sp.get("length", "long") or "long",
                            sp.get("direction", "io") or "io"))
        techs = [t.get("name", "") for t in dev.findall("technologies/technology")]
        tech = None if (not techs or "" in techs) else techs[0]
        out = Device(lib, deviceset, device, dev.get("package", ""), ds.get("prefix", ""),
                     "", ds.get("uservalue", "no").lower() == "yes", gates, pins, symbols, tech)
        d_el = ds.find("description")
        if d_el is not None:
            out.description = re.sub(r"<[^>]+>", " ", d_el.text or "").strip()
        self._dev_cache[key] = out
        return out

    def find_device(self, lib: str, deviceset: str, device: Optional[str] = None) -> Tuple[Optional[Device], Optional[str]]:
        """Tolerant lookup: a missing device name picks the only/first variant."""
        try:
            if device is not None:
                return self.get_device(lib, deviceset, device), None
            names = [e["device"] for e in self.all_entries()
                     if e["library"] == lib and e["deviceset"] == deviceset]
            if not names:
                return None, f"{lib}/{deviceset} not found"
            return self.get_device(lib, deviceset, names[0]), None
        except KeyError as exc:
            return None, str(exc.args[0])


def classify_bad_lbr(path: Path) -> str:
    """Why a .lbr didn't parse: 'binary' (Eagle 5 or older, Eagle 6+/7.4 can convert it),
    'empty', or 'xml-damaged' (text that is nearly XML - repair_xml_lbr may fix it)."""
    try:
        head = Path(path).read_bytes()[:4096]
    except OSError:
        return "unreadable"
    if not head.strip():
        return "empty"
    if head.startswith((b"\xff\xfe", b"\xfe\xff")) or b"<eagle" in head or b"<?xml" in head:
        return "xml-damaged"
    if head.count(b"\x00") > len(head) // 20:
        return "binary"
    return "xml-damaged" if b"<" in head else "binary"


def repair_xml_lbr(path: Path, out_dir: Path, guard=None) -> Optional[Path]:
    """Code-only repair for nearly-XML libraries: UTF-16, BOM, junk before the
    first tag, trailing garbage. Never guesses content. Returns the fixed copy or None."""
    raw = Path(path).read_bytes()
    text = None
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            if "<eagle" in text:
                break
        except UnicodeDecodeError:
            continue
    if not text or "<eagle" not in text:
        return None
    start = text.find("<?xml") if 0 <= text.find("<?xml") < text.find("<eagle") else text.find("<eagle")
    end = text.rfind("</eagle>")
    if end < 0:
        return None                      # truncated: the missing part can't be invented
    text = text[start:end + len("</eagle>")]
    text = re.sub(r'encoding="[^"]+"', 'encoding="utf-8"', text, count=1)
    try:
        ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / Path(path).name
    if guard is not None and dst.exists() and dst.read_text(encoding="utf-8", errors="replace") != text:
        dst = guard.target(dst, f"repaired library {dst.name}")
        if dst is None:
            return None
    dst.write_text(text, encoding="utf-8")
    return dst


def merged_layers(index: LibraryIndex, libs: List[str]) -> ET.Element:
    """Union of the layer tables of the embedded libraries (+ schematic layers)."""
    out = ET.Element("layers")
    seen = set()
    for lib in libs:
        el = index.layers_element(lib)
        if el is None:
            continue
        for layer in el.findall("layer"):
            n = layer.get("number")
            if n not in seen:
                seen.add(n)
                out.append(copy.deepcopy(layer))
    for num, name, color in [(91, "Nets", 2), (92, "Busses", 1), (93, "Pins", 2),
                             (94, "Symbols", 4), (95, "Names", 7), (96, "Values", 7),
                             (97, "Info", 7), (98, "Guide", 6)]:
        if str(num) not in seen:
            ET.SubElement(out, "layer", number=str(num), name=name, color=str(color),
                          fill="1", visible="yes", active="yes")
    out[:] = sorted(out, key=lambda l: int(l.get("number")))
    return out
