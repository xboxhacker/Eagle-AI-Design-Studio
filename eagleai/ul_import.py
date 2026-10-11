"""Import Ultra Librarian (and similar vendor) library zips.

What Ultra Librarian ships (their help pages, checked 2026-10-08):
  * Eagle download   -> an Eagle SCRIPT (.scr) that builds the part inside an open library
                        (File > New > Library, then File > Execute script). Sometimes also a .lbr.
  * Fusion 360 download -> a .lbr (Fusion / Eagle 9 format), often with STEP 3D models.

What this importer does:
  1. Lists the zip and classifies it (eagle-script / eagle-lbr / fusion-lbr, + 3D models, PDFs).
  2. Unzips into the folder you choose (default: <library folder>/UltraLibrarian/<part>/).
  3. Eagle .scr  -> runs it in Eagle 7.4 on a fresh empty .lbr (UNVERIFIED CLI behaviour - same
                    mechanism as the binary-library converter; fallback is the manual 2-step above).
     Fusion .lbr -> keeps the original (with its 3D links) for later Fusion 360 use AND writes a
                    down-converted Eagle 7.4 copy (<name>_e74.lbr): Fusion-only elements such as
                    3D package references are stripped; symbols/footprints/pins are unchanged.
  4. STEP/WRL files are kept next to it in 3d/ for rendering after you move to Fusion 360.
"""
from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

MODEL_EXT = (".step", ".stp", ".wrl", ".igs", ".iges", ".f3d", ".x_t")

# Elements that exist in the Eagle 7.x DTD. Anything else (Fusion/Eagle 9 additions like package3d,
# package3dinstances, spline, frames...) is removed in down-conversion.
EAGLE7_ELEMENTS = set("""eagle compatibility note drawing settings setting grid layers layer library libraries description
packages package symbols symbol devicesets deviceset gates gate devices device connects connect technologies technology
attribute attributes polygon vertex wire dimension text circle rectangle frame hole pad smd pin schematic board
sheets sheet plain instances instance busses bus nets net segment pinref portref label junction parts part variantdefs
variantdef variant classes class clearance designrules param autorouter pass approved elements element signals signal
contactref via errors errors2""".split())
FUSION_ATTRS = ("urn", "library_version", "library_locally_modified", "locally_modified", "package3d_urn",
                "override_package3d_urn", "override_package_urn", "override_locally_modified", "library_urn")


def eagle_version(path: Path) -> str:
    try:
        for _ev, el in ET.iterparse(str(path), events=("start",)):
            if el.tag == "eagle":
                return el.get("version", "")
    except Exception:
        pass
    return ""


def sanitize_for_eagle7(el: ET.Element) -> int:
    """In place: drop Fusion/Eagle-9-only elements and attributes. Returns how many were removed."""
    removed = 0
    for parent in list(el.iter()):
        for child in list(parent):
            if child.tag not in EAGLE7_ELEMENTS:
                parent.remove(child)
                removed += 1
    for node in el.iter():
        for a in FUSION_ATTRS:
            if a in node.attrib:
                del node.attrib[a]
                removed += 1
    return removed


def downconvert_lbr(src: Path, dst: Path) -> Dict:
    tree = ET.parse(src)
    root = tree.getroot()
    removed = sanitize_for_eagle7(root)
    root.set("version", "7.4.0")
    ET.indent(tree, space="  ")
    with open(dst, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle SYSTEM "eagle.dtd">\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)
    return {"path": str(dst), "removed": removed}


def inspect_zip(zpath: Path) -> Dict:
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n]
        lbrs = [n for n in names if n.lower().endswith(".lbr")]
        scrs = [n for n in names if n.lower().endswith(".scr")]
        models = [n for n in names if n.lower().endswith(MODEL_EXT)]
        pdfs = [n for n in names if n.lower().endswith(".pdf")]
        kind = "unknown"
        lbr_versions = {}
        for n in lbrs:
            head = z.read(n)[:2000].decode("utf-8", "ignore")
            m = re.search(r'<eagle[^>]*version="([^"]+)"', head)
            lbr_versions[n] = m.group(1) if m else ""
        kinds = []
        if any(not v.startswith(("6", "7")) for v in lbr_versions.values()):
            kinds.append("Fusion 360 library")
        if any(v.startswith(("6", "7")) for v in lbr_versions.values()):
            kinds.append("Eagle 6/7 library")
        if scrs:
            kinds.append("Eagle script")
        if models:
            kinds.append("3D model")
        kind = " + ".join(kinds) or "unknown"
    stem = Path(zpath).stem
    part = re.sub(r"^(ul_|ultralibrarian_)", "", stem, flags=re.I)
    part = re.sub(r"[^A-Za-z0-9_.+-]", "_", part)[:60] or "part"
    return {"zip": str(zpath), "kind": kind, "lbrs": lbrs, "lbr_versions": lbr_versions, "scripts": scrs,
            "models": models, "pdfs": pdfs, "all": names, "part": part}


def _safe_extract(z: zipfile.ZipFile, name: str, dest: Path) -> Path:
    target = (dest / name).resolve()
    if not str(target).startswith(str(dest.resolve())):
        raise ValueError(f"unsafe path in zip: {name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with z.open(name) as src, open(target, "wb") as out:
        shutil.copyfileobj(src, out)
    return target


def empty_lbr(path: Path):
    """A valid, empty Eagle 7.4 library (for running a vendor .scr into)."""
    from .lbr_builder import LAYERS
    root = ET.Element("eagle", version="7.4.0")
    d = ET.SubElement(root, "drawing")
    st = ET.SubElement(d, "settings")
    ET.SubElement(st, "setting", alwaysvectorfont="no")
    ET.SubElement(d, "grid", distance="0.1", unitdist="inch", unit="inch", style="lines", multiple="1",
                  display="no", altdistance="0.01", altunitdist="inch", altunit="inch")
    ly = ET.SubElement(d, "layers")
    for num, name, color in LAYERS:
        ET.SubElement(ly, "layer", number=str(num), name=name, color=str(color), fill="1", visible="yes", active="yes")
    lib = ET.SubElement(d, "library")
    for sec in ("description", "packages", "symbols", "devicesets"):
        ET.SubElement(lib, sec)
    tree = ET.ElementTree(root)
    with open(path, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle SYSTEM "eagle.dtd">\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)


def run_script_in_eagle(eagle_exe: str, scr: Path, lbr: Path, timeout: float = 120.0) -> Dict:
    import subprocess
    empty_lbr(lbr)
    cmd = [eagle_exe, "-C", f"SCRIPT '{scr}'; WRITE; QUIT;", str(lbr)]
    try:
        subprocess.run(cmd, timeout=timeout, capture_output=True)
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "Eagle did not finish (waiting on a dialog?)"}
    except OSError as exc:
        return {"ok": False, "msg": f"could not start Eagle: {exc}"}
    try:
        n = len(ET.parse(lbr).getroot().findall(".//deviceset"))
    except Exception:
        n = 0
    return {"ok": n > 0, "msg": f"{n} device(s) created" if n else "the script ran but the library is still empty"}


def _first_deviceset(lbr: Path) -> str:
    try:
        ds = ET.parse(lbr).getroot().find(".//deviceset")
        return ds.get("name", "") if ds is not None else ""
    except Exception:
        return ""


def _signature(lbr: Path):
    r = ET.parse(lbr).getroot()
    pads = sorted((p.get("name"), round(float(p.get("x", 0)), 2), round(float(p.get("y", 0)), 2))
                  for p in list(r.iter("pad")) + list(r.iter("smd")))
    cons = sorted((c.get("pin"), c.get("pad")) for c in r.iter("connect"))
    return pads, cons


def suggest_part_name(info: Dict) -> str:
    return info["part"]


def _copy_keep(src: Path, dst: Path, guard=None):
    """Vendor originals next to the library: an existing different file is backed up before it's replaced."""
    if dst.exists() and guard is not None:
        try:
            same = Path(src).read_bytes() == dst.read_bytes()
        except OSError:
            same = False
        if not same:
            guard.backup(dst)
    shutil.copy(src, dst)


def import_zip(zpath: Path, dest: Path, eagle_exe: str = "", part: str = "", guard=None) -> Dict:
    """Unzip into dest and leave ONE Eagle 7.4 library there named after the part:
         dest/<PART>.lbr                 <- use this in Eagle 7.4 (scanned by the app)
         dest/fusion360/<PART>.lbr + 3D  <- original Fusion 360 library and STEP model (not scanned)
         dest/eagle_script/*.scr         <- original vendor script
         dest/docs/                      <- readme / import guides / PDFs
    Source priority for the 7.4 library: Eagle 6/7 .lbr > Fusion .lbr (down-converted) > .scr (converted in code).
    When both a Fusion .lbr and a .scr exist, their pads and pin-to-pad connections are compared."""
    info = inspect_zip(zpath)
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / "_unzip"
    if tmp.exists():
        shutil.rmtree(tmp)
    with zipfile.ZipFile(zpath) as z:
        ex = {n: _safe_extract(z, n, tmp) for n in info["all"]}
    report: List[str] = []
    eagle_lbrs = [ex[n] for n in info["lbrs"] if info["lbr_versions"].get(n, "").startswith(("6", "7"))]
    fusion_lbrs = [ex[n] for n in info["lbrs"] if ex[n] not in eagle_lbrs]
    scrs = [ex[n] for n in info["scripts"]]
    if not part and not (eagle_lbrs or fusion_lbrs) and scrs:
        probe = tmp / "_probe.lbr"
        try:
            part = (scr_to_lbr(scrs[0], probe)["devices"] or [""])[0]
        except Exception:
            part = ""
        probe.unlink(missing_ok=True)
    part = part or next((d for d in (_first_deviceset(f) for f in eagle_lbrs + fusion_lbrs) if d), "") or info["part"]
    part = re.sub(r"[^A-Za-z0-9_.+-]", "_", part)
    out = dest / f"{part}.lbr"
    if guard is not None and out.exists():
        chosen = guard.target(out, f"library {out.name} (re-importing {Path(zpath).name})")
        if chosen is None:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ValueError(f"import cancelled - {out.name} was left as it is")
        if chosen != out:                  # keep the old one, import next to it under a new name
            part, out = chosen.stem, chosen
    source = ""
    if eagle_lbrs:
        shutil.copy(eagle_lbrs[0], out)
        source = f"Eagle {eagle_version(eagle_lbrs[0])} library (used as is)"
    elif fusion_lbrs:
        r = downconvert_lbr(fusion_lbrs[0], out)
        source = f"Fusion 360 library, down-converted ({r['removed']} Fusion-only items removed)"
    elif scrs:
        r = scr_to_lbr(scrs[0], out)
        source = f"Eagle script converted in code ({', '.join(r['devices'])})"
        if r["ignored_commands"]:
            report.append(f"script commands not converted: {r['ignored_commands']} - check the part in Eagle")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("the zip contains no .lbr and no .scr - nothing to import")
    report.append(f"{out.name}: from {source}")

    # cross-check two vendor sources of the same part (different exporters = independent second opinion)
    if fusion_lbrs and scrs and not eagle_lbrs:
        chk = dest / "_check.lbr"
        try:
            scr_to_lbr(scrs[0], chk)
            pa, ca = _signature(out)
            pb, cb = _signature(chk)
            if pa == pb and ca == cb:
                report.append("cross-check: Fusion library and Eagle script agree on every pad and pin connection")
            else:
                report.append("CROSS-CHECK MISMATCH between the Fusion library and the Eagle script - verify pads/pins "
                              f"against the datasheet (pads differ: {pa != pb}, connections differ: {ca != cb})")
        except Exception as exc:
            report.append(f"cross-check skipped: {exc}")
        finally:
            chk.unlink(missing_ok=True)

    if fusion_lbrs or info["models"]:
        fd = dest / "fusion360"
        fd.mkdir(exist_ok=True)
        for f in fusion_lbrs:
            _copy_keep(f, fd / f"{part}.lbr", guard)
        for n in info["models"]:
            _copy_keep(ex[n], fd / Path(n).name, guard)
        report.append(f"Fusion 360 copy + {len(info['models'])} 3D model(s) kept in {fd.name}/ for later "
                      f"(not linked inside the library - assign the STEP to the footprint in Fusion's 3D package editor)")
    if scrs:
        sd = dest / "eagle_script"
        sd.mkdir(exist_ok=True)
        for f in scrs:
            _copy_keep(f, sd / f.name, guard)
    docs = [ex[n] for n in info["all"] if n.lower().endswith((".pdf", ".html", ".htm", ".txt"))]
    if docs:
        dd = dest / "docs"
        dd.mkdir(exist_ok=True)
        for f in docs:
            _copy_keep(f, dd / (f.parent.name + "_" + f.name if f.parent != tmp else f.name), guard)
    shutil.rmtree(tmp, ignore_errors=True)
    return {"kind": info["kind"], "dest": str(dest), "library": str(out), "part": part, "report": report}


# ------------------------------------------------------------ Eagle .scr -> .lbr
class ScrError(ValueError):
    pass


def _tokens(stmt: str) -> List[str]:
    return re.findall(r"'(?:[^']|'')*'|\([^)]*\)|[^\s]+", stmt)


def _q(tok: str) -> str:
    return tok[1:-1].replace("''", "'") if tok.startswith("'") else tok


def _pt(tok: str, k: float):
    nums = tok.strip("()").split()
    return float(nums[0]) * k, float(nums[1]) * k


def _f(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def scr_to_lbr(scr: Path, out: Path) -> Dict:
    """Interpret the library-building subset of an Eagle script (what Ultra Librarian and most vendors emit):
    GRID, EDIT x.pac/.sym/.dev, LAYER, CHANGE, PAD, SMD, HOLE, WIRE (incl. arcs), CIRCLE, RECT, POLYGON, TEXT,
    PIN, PREFIX, DESCRIPTION, VALUE, ADD, PACKAGE, TECHNOLOGY, ATTRIBUTE, CONNECT. Unknown commands are
    reported, not guessed."""
    text = Path(scr).read_text(encoding="utf-8", errors="replace")
    unit = 0.0254          # mil -> mm by default
    layer, width_default, drill, size, ratio = 1, 0.1524, 0.8, 1.27, 8
    smd_round = 0
    pkgs: Dict[str, ET.Element] = {}
    syms: Dict[str, ET.Element] = {}
    devs: Dict[str, Dict] = {}
    cur = None
    kind = None
    unknown: Dict[str, int] = {}

    stmts = []
    for line in text.splitlines():
        line = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        buf, inq = "", False
        for ch in line:
            if ch == "'":
                inq = not inq
            if ch == ";" and not inq:
                stmts.append(buf)
                buf = ""
            else:
                buf += ch
        if buf.strip():
            stmts.append(buf)
    for stmt in stmts:
        t = _tokens(stmt.strip())
        if not t:
            continue
        cmd = t[0].upper()
        a = t[1:]
        if cmd == "GRID":
            for x in a:
                u = x.lower()
                if u in ("mil",):
                    unit = 0.0254
                elif u in ("mm",):
                    unit = 1.0
                elif u in ("inch", "in"):
                    unit = 25.4
                elif u in ("mic",):
                    unit = 0.001
            continue
        if cmd == "SET":
            continue
        if cmd == "EDIT":
            name = _q(a[0])
            base, _, ext = name.rpartition(".")
            ext = ext.lower()
            if ext == "pac":
                cur = pkgs.setdefault(base, ET.Element("package", name=base)); kind = "pac"
            elif ext == "sym":
                cur = syms.setdefault(base, ET.Element("symbol", name=base)); kind = "sym"
            elif ext == "dev":
                cur = devs.setdefault(base, {"prefix": "", "desc": "", "uservalue": "no", "gates": [], "package": None,
                                             "techs": {"": []}, "tech": "", "connects": []}); kind = "dev"
            else:
                raise ScrError(f"EDIT {name}: unknown object type")
            continue
        if cmd == "LAYER" and a:
            layer = int(float(a[0])) if a[0].lstrip("-").isdigit() else layer
            continue
        if cmd == "CHANGE" and len(a) >= 2:
            what, val = a[0].upper(), a[1]
            try:
                if what == "DRILL":
                    drill = float(val) * unit
                elif what == "SIZE":
                    size = float(val) * unit
                elif what == "RATIO":
                    ratio = int(float(val))
                elif what == "WIDTH":
                    width_default = float(val) * unit
                elif what == "ROUNDNESS":
                    smd_round = int(float(val))
                elif what == "LAYER":
                    layer = int(float(val))
            except ValueError:
                pass
            continue
        if kind == "pac" and cmd == "PAD":
            name = next((_q(x) for x in a if x.startswith("'")), "")
            shape = next((x.lower() for x in a if x.lower() in ("round", "square", "octagon", "long", "offset")), "round")
            rot = next((x for x in a if re.fullmatch(r"S?M?R-?[\d.]+", x)), "R0")
            nums = [float(x) for x in a if re.fullmatch(r"-?[\d.]+", x)]
            dia = (nums[-1] if nums else 0) * unit
            pt = _pt(next(x for x in a if x.startswith("(")), unit)
            attrs = dict(name=name, x=_f(pt[0]), y=_f(pt[1]), drill=_f(drill), shape=shape)
            if dia > 0:
                attrs["diameter"] = _f(dia)
            if rot != "R0":
                attrs["rot"] = rot
            ET.SubElement(cur, "pad", **attrs)
            continue
        if kind == "pac" and cmd == "SMD":
            name = next((_q(x) for x in a if x.startswith("'")), "")
            rot = next((x for x in a if re.fullmatch(r"S?M?R-?[\d.]+", x)), "R0")
            nums = [x for x in a if re.fullmatch(r"-?[\d.]+", x)]
            dx, dy = float(nums[0]) * unit, float(nums[1]) * unit
            rnd = abs(int(float(nums[2]))) if len(nums) > 2 else smd_round
            pt = _pt(next(x for x in a if x.startswith("(")), unit)
            attrs = dict(name=name, x=_f(pt[0]), y=_f(pt[1]), dx=_f(dx), dy=_f(dy), layer=str(layer if layer in (1, 16) else 1))
            if rnd:
                attrs["roundness"] = str(rnd)
            if rot != "R0":
                attrs["rot"] = rot
            ET.SubElement(cur, "smd", **attrs)
            continue
        if kind == "pac" and cmd == "HOLE":
            pt = _pt(next(x for x in a if x.startswith("(")), unit)
            nums = [float(x) for x in a if re.fullmatch(r"-?[\d.]+", x)]
            ET.SubElement(cur, "hole", x=_f(pt[0]), y=_f(pt[1]), drill=_f(nums[0] * unit if nums else drill))
            continue
        if cmd == "WIRE" and cur is not None and kind in ("pac", "sym"):
            pts = [x for x in a if x.startswith("(")]
            w = float(a[0]) * unit if a and re.fullmatch(r"-?[\d.]+", a[0]) else width_default
            seq = a[1:] if a and re.fullmatch(r"-?[\d.]+", a[0]) else a
            # walk: point [curve] point [curve] point ...
            prev, curve = None, None
            for x in seq:
                if x.startswith("("):
                    p = _pt(x, unit)
                    if prev is not None:
                        at = dict(x1=_f(prev[0]), y1=_f(prev[1]), x2=_f(p[0]), y2=_f(p[1]), width=_f(w), layer=str(layer))
                        if curve:
                            at["curve"] = _f(curve)
                        ET.SubElement(cur, "wire", **at)
                    prev, curve = p, None
                elif re.fullmatch(r"[-+]?[\d.]+", x):
                    curve = float(x)
            continue
        if cmd in ("CIRCLE",) and cur is not None:
            pts = [_pt(x, unit) for x in a if x.startswith("(")]
            w = float(a[0]) * unit if re.fullmatch(r"-?[\d.]+", a[0]) else width_default
            if len(pts) == 2:
                (cx, cy), (ex, ey) = pts
                import math
                ET.SubElement(cur, "circle", x=_f(cx), y=_f(cy), radius=_f(math.hypot(ex - cx, ey - cy)),
                              width=_f(w), layer=str(layer))
            continue
        if cmd in ("RECT", "RECTANGLE") and cur is not None:
            pts = [_pt(x, unit) for x in a if x.startswith("(")]
            if len(pts) == 2:
                ET.SubElement(cur, "rectangle", x1=_f(pts[0][0]), y1=_f(pts[0][1]), x2=_f(pts[1][0]),
                              y2=_f(pts[1][1]), layer=str(layer))
            continue
        if cmd == "POLYGON" and cur is not None:
            pts = [_pt(x, unit) for x in a if x.startswith("(")]
            w = float(a[0]) * unit if a and re.fullmatch(r"-?[\d.]+", a[0]) else width_default
            if len(pts) >= 3:
                pg = ET.SubElement(cur, "polygon", width=_f(w), layer=str(layer))
                for p in pts[:-1] if pts[0] == pts[-1] else pts:
                    ET.SubElement(pg, "vertex", x=_f(p[0]), y=_f(p[1]))
            continue
        if cmd == "TEXT" and cur is not None and kind in ("pac", "sym"):
            s = next((_q(x) for x in a if x.startswith("'")), "")
            rot = next((x for x in a if re.fullmatch(r"S?M?R-?[\d.]+", x)), "R0")
            pt = _pt(next(x for x in a if x.startswith("(")), unit)
            if s.upper() in (">NAME", ">VALUE"):
                s = s.upper()
            el = ET.SubElement(cur, "text", x=_f(pt[0]), y=_f(pt[1]), size=_f(size), layer=str(layer), ratio=str(ratio))
            if rot.lstrip("S") != "R0":
                el.set("rot", rot.lstrip("S"))
            el.text = s
            continue
        if kind == "sym" and cmd == "PIN":
            name = _q(a[0])
            words = [x.lower() for x in a[1:] if not x.startswith("(")]
            dirs = {"nc", "in", "out", "io", "oc", "pwr", "pas", "hiz", "sup"}
            funcs = {"none", "dot", "clk", "dotclk"}
            lens = {"point", "short", "middle", "long"}
            vis = {"off", "pad", "pin", "both"}
            at = dict(name=name)
            pt = _pt(next(x for x in a if x.startswith("(")), unit)
            at["x"], at["y"] = _f(pt[0]), _f(pt[1])
            for w in words:
                if w in dirs:
                    at["direction"] = w
                elif w in funcs and w != "none":
                    at["function"] = w
                elif w in lens:
                    at["length"] = w
                elif w in vis:
                    at["visible"] = w
                elif re.fullmatch(r"r-?[\d.]+", w):
                    if w != "r0":
                        at["rot"] = w.upper()
                elif re.fullmatch(r"\d+", w):
                    if w != "0":
                        at["swaplevel"] = w
            ET.SubElement(cur, "pin", **at)
            continue
        if kind == "dev":
            if cmd == "PREFIX":
                cur["prefix"] = _q(a[0]) if a else ""
            elif cmd == "DESCRIPTION":
                cur["desc"] = _q(a[0]) if a else ""
            elif cmd == "VALUE":
                cur["uservalue"] = "yes" if a and a[0].lower() == "on" else "no"
            elif cmd == "ADD":
                sym = _q(a[0])
                gname = next((_q(x) for x in a[1:] if x.startswith("'")), "G$1")
                addlevel = next((x.lower() for x in a if x.lower() in ("next", "must", "always", "can", "request")), "next")
                pt = _pt(next(x for x in a if x.startswith("(")), unit)
                cur["gates"].append(dict(name=gname, symbol=sym, x=_f(pt[0]), y=_f(pt[1]),
                                         **({"addlevel": addlevel} if addlevel != "next" else {})))
            elif cmd == "PACKAGE":
                cur["package"] = _q(a[0]) if a else None
                cur["variant"] = _q(a[1]) if len(a) > 1 else (cur["package"] or "")
            elif cmd == "TECHNOLOGY":
                cur["tech"] = _q(a[0]) if a else ""
                cur["techs"].setdefault(cur["tech"], [])
            elif cmd == "ATTRIBUTE" and len(a) >= 2:
                cur["techs"].setdefault(cur["tech"], []).append((_q(a[0]), _q(a[1])))
            elif cmd == "CONNECT":
                q = [_q(x) for x in a if x.startswith("'")]
                if len(q) >= 2:
                    g, _, pin = q[0].rpartition(".")
                    cur["connects"].append(dict(gate=g or (cur["gates"][0]["name"] if cur["gates"] else "G$1"),
                                                pin=pin, pad=q[1]))
            else:
                unknown[cmd] = unknown.get(cmd, 0) + 1
            continue
        unknown[cmd] = unknown.get(cmd, 0) + 1

    if not devs:
        raise ScrError("the script defines no device (.dev)")
    from .lbr_builder import LAYERS
    root = ET.Element("eagle", version="7.4.0")
    d = ET.SubElement(root, "drawing")
    st = ET.SubElement(d, "settings")
    ET.SubElement(st, "setting", alwaysvectorfont="no")
    ET.SubElement(st, "setting", verticaltext="up")
    ET.SubElement(d, "grid", distance="0.1", unitdist="inch", unit="inch", style="lines", multiple="1",
                  display="no", altdistance="0.01", altunitdist="inch", altunit="inch")
    ly = ET.SubElement(d, "layers")
    have = {n for n, _, _ in LAYERS}
    extra = [(46, "Milling", 3), (47, "Measures", 7), (48, "Document", 7), (49, "Reference", 7)]
    for num, name, color in sorted(LAYERS + [e for e in extra if e[0] not in have]):
        ET.SubElement(ly, "layer", number=str(num), name=name, color=str(color), fill="1", visible="yes", active="yes")
    lib = ET.SubElement(d, "library")
    ET.SubElement(lib, "description").text = f"Converted from {Path(scr).name} by EagleAI"
    pk = ET.SubElement(lib, "packages")
    for e in pkgs.values():
        pk.append(e)
    sy = ET.SubElement(lib, "symbols")
    for e in syms.values():
        sy.append(e)
    dss = ET.SubElement(lib, "devicesets")
    for name, dv in devs.items():
        ds = ET.SubElement(dss, "deviceset", name=name, prefix=dv["prefix"] or "U",
                           **({"uservalue": "yes"} if dv["uservalue"] == "yes" else {}))
        if dv["desc"]:
            ET.SubElement(ds, "description").text = dv["desc"]
        gs = ET.SubElement(ds, "gates")
        for g in dv["gates"] or [dict(name="G$1", symbol=next(iter(syms), ""), x="0", y="0")]:
            ET.SubElement(gs, "gate", **g)
        dvs = ET.SubElement(ds, "devices")
        de = ET.SubElement(dvs, "device", name=dv.get("variant", "") or "",
                           **({"package": dv["package"]} if dv["package"] else {}))
        if dv["connects"]:
            cs = ET.SubElement(de, "connects")
            for c in dv["connects"]:
                ET.SubElement(cs, "connect", **c)
        ts = ET.SubElement(de, "technologies")
        for tname, attrs in dv["techs"].items():
            te = ET.SubElement(ts, "technology", name=tname)
            for an, av in attrs:
                ET.SubElement(te, "attribute", name=an, value=av)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    with open(out, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle SYSTEM "eagle.dtd">\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)
    return {"path": str(out), "devices": list(devs), "packages": list(pkgs), "symbols": list(syms),
            "ignored_commands": unknown}
