"""Bring an existing design into the app.

* import_sch  - an Eagle 7.x .sch. Fully deterministic, no AI cost. The libraries
                embedded in the .sch are registered with the index, so the pins
                are exactly what Eagle has in that file even if your installed
                .lbr changed since.
* parse_md    - a Schematic MD (markdown) file. Connections are parsed in code;
                only the BOM-row -> library-part choice is left to the AI
                (see tools.tool_import_apply_nets).
"""
from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .library import LibraryIndex, pin_norm
from .model import Design, PartSpec

MPN_KEYS = ("MPN", "MFR_PART", "MFRPART", "MANUFACTURER_PART_NUMBER", "PARTNO", "PART_NUMBER", "MPN#", "MF_PN")
LCSC_KEYS = ("LCSC", "LCSC_PART", "LCSC#", "JLC", "JLCPCB", "JLC_PART", "LCSC_PART_NUMBER")


def save_embedded(lib: str, libel: ET.Element, layers: Optional[ET.Element], folder: Path) -> Path:
    """Keep a copy of a .sch-embedded library so it survives re-opening the project."""
    folder.mkdir(parents=True, exist_ok=True)
    root = ET.Element("eagle", version="7.4.0")
    drawing = ET.SubElement(root, "drawing")
    if layers is not None:
        drawing.append(copy.deepcopy(layers))
    drawing.append(copy.deepcopy(libel))
    path = folder / f"{re.sub(r'[^A-Za-z0-9_.+-]', '_', lib)}.lbr.xml"
    path.write_text(f"<!-- {lib} -->\n" + ET.tostring(root, encoding="unicode"), encoding="utf-8")
    return path


def load_embedded(index: LibraryIndex, folder: Path) -> int:
    n = 0
    for f in sorted(folder.glob("*.lbr.xml")) if folder.is_dir() else []:
        try:
            text = f.read_text(encoding="utf-8")
            m = re.match(r"<!-- (.+?) -->", text)
            root = ET.fromstring(text[text.index("<eagle"):])
            libel = root.find("drawing/library")
            if libel is not None:
                index.add_embedded(m.group(1) if m else f.name[:-8], libel, root.find("drawing/layers"), str(f))
                n += 1
        except Exception:
            continue
    return n


def import_sch(path: Path, index: LibraryIndex, keep_libs_in: Optional[Path] = None) -> Tuple[Design, List[str]]:
    path = Path(path)
    report: List[str] = []
    root = ET.parse(path).getroot()
    ver = root.get("version", "?")
    if not ver.startswith(("6.", "7.")):
        report.append(f"file is Eagle {ver}; this tool targets 7.x - check the result carefully")
    sch = root.find("drawing/schematic")
    if sch is None:
        raise ValueError(f"{path.name} is not an Eagle schematic (no <schematic>)")
    layers = root.find("drawing/layers")
    for lib in sch.findall("libraries/library"):
        name = lib.get("name", "")
        index.add_embedded(name, lib, layers, str(path))
        if keep_libs_in:
            save_embedded(name, lib, layers, keep_libs_in)

    d = Design(path.stem)
    devs = {}
    renamed: Dict[str, str] = {}
    skipped = []
    for part in sch.findall("parts/part"):
        name = part.get("name", "")
        lib, ds, dv = part.get("library", ""), part.get("deviceset", ""), part.get("device", "")
        dev, err = index.find_device(lib, ds, dv)
        if dev is None:
            report.append(f"{name}: {err} - skipped")
            continue
        if not dev.package or not dev.pins:       # supply symbols, frames, logos
            skipped.append(name)
            continue
        ref = name.upper()
        if ref != name:
            renamed[name] = ref
        attrs = {a.get("name", "").upper(): a.get("value", "") for a in part.findall("attribute")}
        spec = PartSpec(ref, lib, ds, dv, part.get("value", "") or "",
                        next((attrs[k] for k in MPN_KEYS if attrs.get(k)), ""),
                        next((attrs[k] for k in LCSC_KEYS if attrs.get(k)), ""))
        tech = part.get("technology")
        if tech:
            spec.technology = tech
        d.parts[ref] = spec
        devs[name] = dev
    if skipped:
        report.append(f"left out {len(skipped)} supply/frame symbols ({', '.join(skipped[:8])}{'…' if len(skipped) > 8 else ''}); "
                      f"their net names are kept as labels")
    if renamed:
        report.append(f"upper-cased refs: {', '.join(f'{a}->{b}' for a, b in renamed.items())}")

    sheets = sch.findall("sheets/sheet")
    if len(sheets) > 1:
        report.append(f"{len(sheets)} sheets merged onto one (nets with the same name join, as in Eagle)")
    used = set()
    for sheet in sheets:
        for net in sheet.findall("nets/net"):
            nname = net.get("name", "")
            for pr in net.iter("pinref"):
                pname, gate, pin = pr.get("part", ""), pr.get("gate", ""), pr.get("pin", "")
                dev = devs.get(pname)
                if dev is None:
                    continue
                pobj = next((p for p in dev.pins if p.gate == gate and p.name == pin), None)
                if pobj is None:
                    report.append(f"{pname}.{pin} (gate {gate}) on {nname}: pin not in library - skipped")
                    continue
                found, err = dev.resolve(pin)
                tok = pin if (not err and any(f.key() == pobj.key() for f in found) and len(found) == 1) else pobj.pads[0]
                token = f"{pname.upper()}.{tok}"
                lst = d.nets.setdefault(nname, [])
                if token not in lst:
                    lst.append(token)
                used.add((pname.upper(), pobj.key()))

    # Eagle wires power pins of un-placed gates (hidden VCC/GND gates of 74xx etc.) to the net
    # named like the pin. To stay conservative we only do the same for (a) pins of gates with no
    # instance, or (b) placed but un-wired pwr pins whose net name already exists in the design.
    placed = {(i.get("part", ""), i.get("gate", "")) for sh in sheets for i in sh.findall("instances/instance")}
    implicit = []
    for name, dev in devs.items():
        for p in dev.pins:
            if p.direction != "pwr" or (name.upper(), p.key()) in used:
                continue
            net = re.sub(r"@\d+$", "", p.name)
            if (name, p.gate) in placed and net not in d.nets:
                continue
            found, err = dev.resolve(p.name)
            tok = p.name if not err and len(found) == 1 else p.pads[0]
            d.nets.setdefault(net, []).append(f"{name.upper()}.{tok}")
            implicit.append(f"{name.upper()}.{p.name}->{net}")
    if implicit:
        report.append(f"{len(implicit)} implicit power connections made explicit: {', '.join(implicit[:10])}"
                      f"{'…' if len(implicit) > 10 else ''}")
    d.notes = f"Imported from {path.name}"
    report.insert(0, f"imported {len(d.parts)} parts and {len(d.nets)} nets from {path.name}")
    return d, report


# ------------------------------------------------------------------ markdown
TOKEN = re.compile(r"\b([A-Z]{1,4}\d{1,4}[A-Z]?)\.(?:Pin\s?(\d+)|([A-Za-z0-9_+\-~@]+))")
NET = re.compile(r"\bNet\s+([A-Za-z0-9_+\-./]+?)(?=[\s:,)`]|$)")
SRC_PIN = re.compile(r"^\s*[-*]?\s*`?Pin\s+(\d+)\s*(?:\(([^)]*)\))?")
SRC_NAME = re.compile(r"^\s*[-*]?\s*`?([A-Za-z0-9_+~@]+)\s*:")


class _UF:
    def __init__(self):
        self.p: Dict[str, str] = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def looks_like_md_schematic(path: Path) -> bool:
    try:
        return "COMPLETE BILL OF MATERIALS" in Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def parse_md(path: Path) -> Dict:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), Path(path).stem)

    # BOM table
    bom: List[Dict] = []
    in_bom, header = False, None
    for l in lines:
        if l.startswith("## "):
            in_bom = "BILL OF MATERIALS" in l.upper()
            header = None
            continue
        if not in_bom or not l.strip().startswith("|"):
            continue
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        row = dict(zip(header, cells))
        refs = [r.strip().upper() for r in row.get("ref", "").split(",") if r.strip()]
        if refs:
            bom.append({"refs": refs, "part_number": row.get("part number", ""), "value": row.get("value", ""),
                        "description": row.get("description", ""), "package": row.get("package", "")})

    uf = _UF()
    names: Dict[str, str] = {}           # node -> pin name hint
    named: Dict[str, set] = {}           # node -> net names given
    cur = None

    def node(ref, num, nm):
        n = f"{ref}.{num}" if num else f"{ref}.{nm}"
        uf.find(n)
        return n

    for l in lines:
        h = re.match(r"^###\s+([A-Z]{1,4}\d{1,4}[A-Z]?)\b", l)
        if h:
            cur = h.group(1)
            continue
        if l.startswith("## ") or l.startswith("# "):
            cur = None
        stripped = l.strip().strip("`")
        summary = re.match(r"^[-*]?\s*`?Net\s+([A-Za-z0-9_+\-./]+)\s*:\s*(.*)$", stripped)
        targets = [node(m.group(1), m.group(2), m.group(3)) for m in TOKEN.finditer(l)]
        if summary:
            net = summary.group(1)
            for t in targets:
                named.setdefault(t, set()).add(net)
            for a, b in zip(targets, targets[1:]):
                uf.union(a, b)
            continue
        src = None
        if cur:
            m = SRC_PIN.match(l)
            if m:
                src = node(cur, m.group(1), None)
                if m.group(2):
                    names[src] = m.group(2).strip()
            else:
                m = SRC_NAME.match(l)
                if m and m.group(1).upper() not in ("NET", "PIN", "NOTE") and (TOKEN.search(l) or NET.search(l)):
                    src = node(cur, None, m.group(1))
        nets_here = [n for n in NET.findall(l)]
        group = ([src] if src else []) + targets
        if src is None and len(targets) < 2:
            continue                      # prose mention, not a connection
        for n in nets_here:
            for g in group:
                named.setdefault(g, set()).add(n)
        for a, b in zip(group, group[1:]):
            uf.union(a, b)

    groups: Dict[str, List[str]] = {}
    for n in list(uf.p):
        groups.setdefault(uf.find(n), []).append(n)
    nets: Dict[str, List[str]] = {}
    conflicts: List[str] = []
    auto = 0
    for members in groups.values():
        given = sorted({x for m in members for x in named.get(m, ())})
        if len(members) < 2 and not given:
            continue
        if len(given) > 1:
            conflicts.append(f"nets {given} are joined through {', '.join(sorted(members)[:6])} - one of them is wrong")
        if given:
            name = given[0]
        else:
            auto += 1
            name = f"N{auto}"
        nets.setdefault(name, [])
        nets[name] += sorted(members)
    bom_refs = {r for row in bom for r in row["refs"]}
    used_refs = {m.split(".")[0] for ms in nets.values() for m in ms}
    return {"title": title, "bom": bom, "nets": nets, "pin_names": names, "conflicts": conflicts,
            "refs_not_in_bom": sorted(used_refs - bom_refs), "source": str(path)}
