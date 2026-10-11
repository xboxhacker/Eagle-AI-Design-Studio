"""Write an Eagle 7.x .sch from a Design.

Nets are drawn as short stubs with net labels at every pin (same name = same net).
That is what keeps the output ERC-clean without an autorouter.
"""
from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .layout import Layout, build_layout, pin_stub_dir, snap
from .library import Device, LibraryIndex, merged_layers
from .model import Design

STUB = 5.08
WIRE_W = "0.1524"


def safe_net_name(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_+\-.]", "_", name.strip())
    return n or "NET"


def _label_rot(d: Tuple[int, int]) -> Optional[str]:
    return {(-1, 0): "R180", (0, 1): "R90", (0, -1): "R270"}.get(d)


def resolve_devices(design: Design, index: LibraryIndex) -> Tuple[Dict[str, Device], List[str]]:
    devs: Dict[str, Device] = {}
    errs: List[str] = []
    for ref, p in design.parts.items():
        dev, err = index.find_device(p.library, p.deviceset, p.device)
        if dev is None:
            errs.append(f"{ref}: {err}")
        else:
            devs[ref] = dev
    return devs, errs


def net_nodes(design: Design, devs: Dict[str, Device]) -> Dict[str, List[Tuple[str, object]]]:
    """net -> [(ref, Pin)] with duplicates removed; unresolved pins are skipped."""
    out: Dict[str, List[Tuple[str, object]]] = {}
    for net, members in design.nets.items():
        seen = set()
        for m in members:
            ref, pin = m.split(".", 1)
            dev = devs.get(ref.upper())
            if dev is None:
                continue
            found, _ = dev.resolve(pin)
            for f in found:
                k = (ref.upper(), f.key())
                if k not in seen:
                    seen.add(k)
                    out.setdefault(net, []).append((ref.upper(), f))
    return out


def write_sch(design: Design, index: LibraryIndex, path: Path) -> Dict:
    devs, errs = resolve_devices(design, index)
    layout = build_layout(devs)
    nodes = net_nodes(design, devs)

    root = ET.Element("eagle", version="7.4.0")
    drawing = ET.SubElement(root, "drawing")
    settings = ET.SubElement(drawing, "settings")
    ET.SubElement(settings, "setting", alwaysvectorfont="no")
    ET.SubElement(settings, "setting", verticaltext="up")
    ET.SubElement(drawing, "grid", distance="0.1", unitdist="inch", unit="inch", style="lines",
                  multiple="1", display="yes", altdistance="0.05", altunitdist="inch", altunit="inch")

    libs = sorted({d.library for d in devs.values()})
    drawing.append(merged_layers(index, libs))

    sch = ET.SubElement(drawing, "schematic", xreflabel="%F%N/%S.%C%R", xrefpart="/%S.%C%R")
    libs_el = ET.SubElement(sch, "libraries")
    for lib in libs:
        el = copy.deepcopy(index.library_element(lib))
        if not index.lib_version(lib).startswith(("6", "7")):
            from .ul_import import sanitize_for_eagle7
            sanitize_for_eagle7(el)          # Fusion/Eagle 9 library: strip what Eagle 7.4 can't read
        el.set("name", lib)
        libs_el.append(el)
    ET.SubElement(sch, "attributes")
    ET.SubElement(sch, "variantdefs")
    classes = ET.SubElement(sch, "classes")
    ET.SubElement(classes, "class", number="0", name="default", width="0", drill="0")

    parts_el = ET.SubElement(sch, "parts")
    for ref, dev in devs.items():
        spec = design.parts[ref]
        attrs = dict(name=ref, library=dev.library, deviceset=dev.deviceset, device=dev.device)
        if spec.value:
            attrs["value"] = spec.value
        tech = spec.technology or dev.technology
        if tech:
            attrs["technology"] = tech
        ET.SubElement(parts_el, "part", **attrs)

    sheets = ET.SubElement(sch, "sheets")
    sheet = ET.SubElement(sheets, "sheet")
    ET.SubElement(sheet, "plain")
    inst = ET.SubElement(sheet, "instances")
    for b in layout.blocks:
        ET.SubElement(inst, "instance", part=b.ref, gate=b.gate, x=str(b.ox), y=str(b.oy))
    ET.SubElement(sheet, "busses")
    nets_el = ET.SubElement(sheet, "nets")

    used_names: Dict[str, str] = {}
    for net, plist in nodes.items():
        name = safe_net_name(net)
        if name in used_names and used_names[name] != net:
            name = f"{name}_{len(used_names)}"
        used_names[name] = net
        net_el = ET.SubElement(nets_el, "net", name=name, **{"class": "0"})
        for ref, pin in plist:
            px, py = layout.pin_point(ref, pin)
            d = pin_stub_dir(pin)
            ex, ey = snap(px + d[0] * STUB), snap(py + d[1] * STUB)
            seg = ET.SubElement(net_el, "segment")
            ET.SubElement(seg, "pinref", part=ref, gate=pin.gate, pin=pin.name)
            ET.SubElement(seg, "wire", x1=str(px), y1=str(py), x2=str(ex), y2=str(ey),
                          width=WIRE_W, layer="91")
            lab = dict(x=str(ex), y=str(ey), size="1.778", layer="95")
            rot = _label_rot(d)
            if rot:
                lab["rot"] = rot
            ET.SubElement(seg, "label", **lab)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    with open(path, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle SYSTEM "eagle.dtd">\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)
    return {"path": str(path), "parts": len(devs), "nets": len(nodes), "errors": errs,
            "gates": len(layout.blocks)}
