"""Keep the BOM honest: the design is the only source of truth, exported files are checked against it.

* export_jlc()     - writes BOM/CPL and records what was exported (board, side, file times).
* export_status()  - for each recorded export: re-runs the export in memory from the CURRENT design and
                     board and compares it with what is in the file. Result: up to date / differences /
                     edited outside the app / file missing.
* pull_from_file() - when you edited an exported BOM in Excel, bring Comment (value) and LCSC changes back
                     into the design (after you confirm).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, List, Tuple

from . import jlc_export

BOM_COLS = ["Comment", "Designator", "Footprint", "LCSC Part #"]


def _rel(p, folder: Path) -> str:
    try:
        return str(Path(p).relative_to(folder))
    except ValueError:
        return str(p)


class Cancelled(Exception):
    pass


def export_jlc(project, brd: Path, side: str, bottom_mode: str = "eagle", offsets: Path = None) -> Dict:
    bom_p, cpl_p = jlc_export.export_paths(Path(brd), project.folder / "jlc", side)
    guard = getattr(project, "guard", None)
    if guard is not None:
        chosen = guard.targets([bom_p, cpl_p], "JLC BOM + CPL files")
        if chosen is None:
            raise Cancelled("export cancelled - the existing BOM/CPL files were left as they are")
        bom_p, cpl_p = chosen[bom_p], chosen[cpl_p]
    res = jlc_export.export(project.design, Path(brd), project.folder / "jlc", side, bottom_mode, offsets,
                            bom_path=bom_p, cpl_path=cpl_p)
    exports = project.state().get("exports", {})
    key = _rel(res["bom_path"], project.folder)
    exports[key] = {"brd": str(brd), "side": side, "bottom_mode": bottom_mode, "cpl": _rel(res["cpl_path"], project.folder),
                    "at": time.time(), "bom_mtime": os.path.getmtime(res["bom_path"]),
                    "cpl_mtime": os.path.getmtime(res["cpl_path"])}
    project.save_state(exports=exports)
    return res


def read_bom_xlsx(path: Path) -> List[Dict]:
    from openpyxl import load_workbook
    ws = load_workbook(path, read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    head = [str(h or "").strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        d = {head[i]: ("" if v is None else str(v).strip()) for i, v in enumerate(r) if i < len(head)}
        if any(d.values()):
            out.append(d)
    return out


def _by_ref(rows: List[Dict]) -> Dict[str, Tuple[str, str, str]]:
    """{designator: (comment, footprint, lcsc)} - a BOM line with several designators is split up."""
    out = {}
    for r in rows:
        for ref in str(r.get("Designator", "")).replace(";", ",").split(","):
            ref = ref.strip().upper()
            if ref:
                out[ref] = (str(r.get("Comment", "")), str(r.get("Footprint", "")), str(r.get("LCSC Part #", "")))
    return out


def compare(file_rows: List[Dict], live_rows: List[Dict]) -> List[str]:
    f, l = _by_ref(file_rows), _by_ref(live_rows)
    diffs = []
    for ref in sorted(set(f) | set(l), key=lambda r: (r.rstrip("0123456789"), int("0" + "".join(c for c in r if c.isdigit())))):
        if ref not in f:
            diffs.append(f"{ref}: missing from the exported file (now {l[ref][0]} {l[ref][2] or 'no LCSC'})")
        elif ref not in l:
            diffs.append(f"{ref}: in the exported file but no longer in the design / on this side / now DNP")
        else:
            for i, name in enumerate(("value", "footprint", "LCSC")):
                if f[ref][i] != l[ref][i]:
                    diffs.append(f"{ref}: {name} in file '{f[ref][i]}' - now '{l[ref][i]}'")
    return diffs


def export_status(project) -> List[Dict]:
    out = []
    for key, rec in sorted(project.state().get("exports", {}).items(), key=lambda kv: -kv[1].get("at", 0)):
        path = project.folder / key
        st = {"path": str(path), "name": Path(key).name, "side": rec.get("side"), "at": rec.get("at"),
              "edited_outside": False, "differences": [], "error": ""}
        if not path.exists():
            st["error"] = "file was deleted"
            out.append(st)
            continue
        st["edited_outside"] = abs(os.path.getmtime(path) - rec.get("bom_mtime", 0)) > 1
        try:
            file_rows = read_bom_xlsx(path)
            brd = Path(rec["brd"])
            if not brd.exists():
                st["error"] = f"board file {brd.name} is gone - can't check placement side"
            live = jlc_export.build(project.design, jlc_export.read_brd(brd), rec.get("side", "both"),
                                    rec.get("bottom_mode", "eagle"))["bom"] if brd.exists() else []
            if brd.exists():
                st["differences"] = compare(file_rows, live)
                st["brd_changed"] = os.path.getmtime(brd) > rec.get("at", 0)
        except Exception as exc:
            st["error"] = f"{type(exc).__name__}: {exc}"
        st["up_to_date"] = not st["differences"] and not st["error"]
        out.append(st)
    return out


def pull_candidates(project, bom_path: Path) -> List[Tuple[str, str, str, str]]:
    """Edits made in the exported file that differ from the design: [(ref, field, design_value, file_value)]."""
    changes = []
    for ref, (comment, _fp, lcsc) in _by_ref(read_bom_xlsx(bom_path)).items():
        ps = project.design.parts.get(ref)
        if ps is None:
            continue
        if comment and comment != ps.value and comment != ps.deviceset:
            changes.append((ref, "value", ps.value, comment))
        if lcsc != ps.lcsc and (lcsc or ps.lcsc):
            changes.append((ref, "lcsc", ps.lcsc, lcsc))
    return changes


def apply_pull(project, changes) -> int:
    for ref, field, _old, new in changes:
        ps = project.design.parts.get(ref)
        if ps is not None:
            setattr(ps, field, new.upper() if field == "lcsc" else new)
    project.changed()
    return len(changes)


def mark_seen(project, bom_path: Path):
    """After pulling edits, accept the file's current timestamp so it's no longer 'edited outside'."""
    exports = project.state().get("exports", {})
    key = _rel(bom_path, project.folder)
    if key in exports:
        exports[key]["bom_mtime"] = os.path.getmtime(bom_path)
        project.save_state(exports=exports)
