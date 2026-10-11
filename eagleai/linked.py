"""Linked designs: other boards that work together with the open one (a main board and its daughter board, a
transmitter and its receiver...). Only one design is open in the studio; the linked ones are READ ONLY here.

- The links live in .eagleai/state.json ("linked": [{"path", "note", "seen"}]); paths are relative to the project.
- A short card per linked design (title, summary, key parts, power nets, connectors) goes into the AI's system
  prompt with every message; the linked_design tool gives the AI full detail only when it asks for it.
- An optional shared "system requirements" file (state "system_doc") holds the rules that span boards (operating
  frequency, connector pinout, supply limits). Every linked board can point at the same file.
- "seen" is a fingerprint of the linked design the last time this project looked at it, so the studio can say
  what changed on the other board since then.
"""
from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .importer import looks_like_md_schematic, parse_md
from .md_writer import digest, extract_doc

CARD_CHARS = 3000          # per linked design in the system prompt
SYSTEM_DOC_CHARS = 8000    # shared requirements file in the system prompt
TOOL_CHARS = 12000

SYSTEM_TEMPLATE = """# System requirements

Rules shared by the boards that work together. Every linked board reads this file (the AI gets it with every
message), so put here what one board needs to know about another.

## Boards
- (name) - (what it does)

## Shared values
- Operating frequency:
- Supply / current limits:
- Coil / magnetic coupling:

## Interfaces (connectors, wires, coupling)
-

## Cross-board checks
- (e.g. both boards use the same connector pinout and logic level)
"""


@dataclass
class Companion:
    path: Path                      # what was linked: folder, design.json, .sch or .md
    name: str = ""
    source: str = ""                # "design.json" | ".sch" | ".md"
    title: str = ""
    subtitle: str = ""
    summary: List[str] = field(default_factory=list)
    parts: Dict[str, Dict] = field(default_factory=dict)   # ref -> {part, value, mpn, note, dnp}
    nets: Dict[str, List[str]] = field(default_factory=dict)
    md_path: Optional[Path] = None
    md_text: str = ""
    error: str = ""

    @property
    def folder(self) -> Path:
        return self.path if self.path.is_dir() else self.path.parent


# ------------------------------------------------------------------ loading a linked design
def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _newest(files: List[Path]) -> Optional[Path]:
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def _project_md(folder: Path) -> Optional[Path]:
    """The Schematic MD of a folder: the one its studio project links, else the newest schematic MD in it."""
    try:
        rec = json.loads(_read(folder / ".eagleai" / "state.json") or "{}").get("md") or {}
    except ValueError:
        rec = {}
    if rec.get("path"):
        p = Path(rec["path"])
        p = p if p.is_absolute() else folder / p
        if p.exists():
            return p
    return _newest([f for f in folder.glob("*.md") if looks_like_md_schematic(f)])


def _from_design_json(c: Companion, path: Path):
    from .model import Design
    d = Design.load(path)
    c.name = d.name
    c.source = "design.json"
    for s in d.parts.values():
        c.parts[s.ref] = {"part": s.deviceset + (f"/{s.device}" if s.device else ""), "value": s.value,
                          "mpn": s.mpn, "note": s.note, "dnp": s.dnp}
    c.nets = {n: list(m) for n, m in d.nets.items()}
    doc = d.doc or {}
    c.title, c.subtitle, c.summary = doc.get("title", ""), doc.get("subtitle", ""), list(doc.get("summary") or [])


def _from_sch(c: Companion, path: Path):
    """Parts and nets straight from the Eagle XML (no libraries needed - this is only for reading)."""
    root = ET.parse(path).getroot()
    c.source = ".sch"
    c.name = path.stem
    for p in root.iter("part"):
        attrs = {a.get("name", "").upper(): a.get("value", "") for a in p.iter("attribute")}
        c.parts[p.get("name", "")] = {"part": p.get("deviceset", "") + (f"/{p.get('device')}" if p.get("device") else ""),
                                      "value": p.get("value", "") or "",
                                      "mpn": attrs.get("MPN", "") or attrs.get("MF_PN", ""), "note": "", "dnp": False}
    for n in root.iter("net"):
        members = {f"{r.get('part')}.{r.get('pin')}" for r in n.iter("pinref")}
        if members:
            name = n.get("name", "?")
            c.nets[name] = sorted(set(c.nets.get(name, [])) | members)


def _from_md(c: Companion, path: Path):
    parsed = parse_md(path)
    c.source = ".md"
    c.name = path.stem
    for row in parsed["bom"]:
        for ref in row["refs"]:
            c.parts[ref] = {"part": row.get("part_number") or row.get("description", ""), "value": row.get("value", ""),
                            "mpn": row.get("part_number", ""), "note": row.get("package", ""), "dnp": False}
    c.nets = {n: list(m) for n, m in parsed["nets"].items()}


def load_companion(path: Path) -> Companion:
    path = Path(path)
    c = Companion(path)
    try:
        if not path.exists():
            raise FileNotFoundError(f"{path} not found (moved or renamed?)")
        folder = path if path.is_dir() else path.parent
        if path.is_dir() or path.name.lower() == "design.json":
            if (folder / "design.json").exists():
                _from_design_json(c, folder / "design.json")
            else:
                sch = _newest(list(folder.glob("*.sch")))
                md = _project_md(folder)
                if sch:
                    _from_sch(c, sch)
                elif md:
                    _from_md(c, md)
                else:
                    raise FileNotFoundError(f"no design.json, .sch or schematic MD in {folder}")
            c.md_path = _project_md(folder)
            c.name = folder.name
        elif path.suffix.lower() == ".sch":
            _from_sch(c, path)
            same = path.with_suffix(".md")
            c.md_path = same if same.exists() else _project_md(folder)
        elif path.suffix.lower() == ".md":
            _from_md(c, path)
            c.md_path = path
        else:
            raise ValueError(f"{path.name}: link a project folder, design.json, .sch or .md")
        if c.md_path:
            c.md_text = _read(c.md_path)
            doc = extract_doc(c.md_text)
            c.title = c.title or doc.get("title", "")
            c.subtitle = c.subtitle or doc.get("subtitle", "")
            c.summary = c.summary or list(doc.get("summary") or [])
    except Exception as exc:
        c.error = f"{type(exc).__name__}: {exc}"
    return c


def _stamp(path: Path) -> Tuple:
    """Changes when any file the companion is read from changes."""
    path = Path(path)
    folder = path if path.is_dir() else path.parent
    files = [path] if path.is_file() else []
    files += [folder / "design.json", folder / ".eagleai" / "state.json"] + list(folder.glob("*.sch")) \
        + list(folder.glob("*.md"))
    out = []
    for f in files:
        try:
            out.append((str(f), f.stat().st_mtime_ns, f.stat().st_size))
        except OSError:
            pass
    return tuple(sorted(out))


_cache: Dict[str, Tuple[Tuple, Companion]] = {}


def companion(path: Path) -> Companion:
    key = str(Path(path).resolve())
    st = _stamp(path)
    hit = _cache.get(key)
    if hit and hit[0] == st:
        return hit[1]
    c = load_companion(path)
    _cache[key] = (st, c)
    return c


# ------------------------------------------------------------------ links in the project state
def _rel(base: Path, target: Path) -> str:
    try:
        return os.path.relpath(Path(target).resolve(), Path(base).resolve())
    except ValueError:                    # other drive
        return str(Path(target).resolve())


def _abs(base: Path, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (Path(base) / q).resolve()


def links(p) -> List[Dict]:
    """[{path: absolute Path, note, seen, rec}] for the project p (a tools.Project)."""
    out = []
    for rec in p.state().get("linked") or []:
        out.append({"path": _abs(p.folder, rec["path"]), "note": rec.get("note", ""), "seen": rec.get("seen"),
                    "rec": rec})
    return out


def _save_links(p, recs: List[Dict]):
    p.save_state(linked=recs)


def _same(a: Path, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _studio_folder(path: Path) -> Optional[Path]:
    """The other side's studio project folder, if the link points at one (folder or its design.json)."""
    path = Path(path)
    folder = path if path.is_dir() else (path.parent if path.name.lower() == "design.json" else None)
    if folder is not None and ((folder / "design.json").exists() or (folder / ".eagleai").is_dir()):
        return folder
    return None


def _state_file(folder: Path) -> Path:
    return Path(folder) / ".eagleai" / "state.json"


def _load_state(folder: Path) -> Dict:
    try:
        return json.loads(_read(_state_file(folder)) or "{}")
    except ValueError:
        return {}


def _write_state(folder: Path, st: Dict):
    f = _state_file(folder)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st, indent=2), encoding="utf-8")


def add_link(p, path: Path, note: str = "", back: bool = True) -> str:
    path = Path(path)
    if _same(path, p.folder) or _same(path, p.design_path):
        return "That is this project - pick the other board's folder or file."
    recs = p.state().get("linked") or []
    if any(_same(_abs(p.folder, r["path"]), path) for r in recs):
        return f"{path.name} is already linked."
    c = companion(path)
    recs.append({"path": _rel(p.folder, path), "note": note, "seen": fingerprint(c)})
    _save_links(p, recs)
    msg = f"Linked {c.name or path.name}" + (f" ({c.error})" if c.error else
                                              f": {len(c.parts)} parts, {len(c.nets)} nets")
    other = _studio_folder(path)
    if back and other is not None:
        st = _load_state(other)
        theirs = st.get("linked") or []
        if not any(_same(_abs(other, r["path"]), p.folder) for r in theirs):
            mine = companion(p.folder)
            theirs.append({"path": _rel(other, p.folder), "note": "", "seen": fingerprint(mine)})
            st["linked"] = theirs
            mine_doc = p.state().get("system_doc")
            if mine_doc and not st.get("system_doc"):
                st["system_doc"] = _rel(other, _abs(p.folder, mine_doc))
            _write_state(other, st)
            msg += f"; {other.name} is linked back to this project"
    if not p.state().get("system_doc") and other is not None and _load_state(other).get("system_doc"):
        theirs_doc = _abs(other, _load_state(other)["system_doc"])
        if theirs_doc.exists():
            p.save_state(system_doc=_rel(p.folder, theirs_doc))
            msg += f"; uses its system requirements file {theirs_doc.name}"
    return msg


def remove_link(p, path: Path) -> bool:
    recs = p.state().get("linked") or []
    keep = [r for r in recs if not _same(_abs(p.folder, r["path"]), path)]
    _save_links(p, keep)
    return len(keep) != len(recs)


def set_note(p, path: Path, note: str):
    recs = p.state().get("linked") or []
    for r in recs:
        if _same(_abs(p.folder, r["path"]), path):
            r["note"] = note
    _save_links(p, recs)


def label(path: Path, c: Companion) -> str:
    return c.name or Path(path).name


def find(p, name: Optional[str]) -> Tuple[Optional[Companion], str]:
    ls = links(p)
    if not ls:
        return None, "no linked designs - the user links them with the 'Linked designs…' button"
    names = [(label(l["path"], companion(l["path"])), l) for l in ls]
    if not name:
        if len(ls) == 1:
            return companion(ls[0]["path"]), ""
        return None, "several linked designs - give name: " + ", ".join(n for n, _ in names)
    key = name.strip().lower()
    hits = [l for n, l in names if n.lower() == key] or \
        [l for n, l in names if key in n.lower() or key in str(l["path"]).lower() or key in (l["note"] or "").lower()]
    if len(hits) != 1:
        return None, (f"'{name}' matches {len(hits)} linked designs" if hits else f"no linked design '{name}'") + \
            " - linked: " + ", ".join(n for n, _ in names)
    return companion(hits[0]["path"]), ""


# ------------------------------------------------------------------ what the AI sees
POWER = re.compile(r"^(A|P|D)?(GND|VSS|VCC|VDD|VIN|VBAT|VBUS|VOUT|VSYS|V\+|\+)|^\+?\d+V\d*|V\d+$|^\d+V\d+$", re.I)
CONN = re.compile(r"^(J|CN|CON|X|P|TP|JP)\d", re.I)


def _part_str(ref: str, d: Dict) -> str:
    v = d.get("value") or ""
    part = (d.get("part") or "").split("/")[0]
    body = v if v and (not part or v.lower() in part.lower() or part.upper() in ("R", "C", "L") or len(part) > 24) \
        else " ".join(x for x in (part, v) if x)
    return f"{ref}={body or '?'}" + (" DNP" if d.get("dnp") else "")


def _ref_key(ref: str):
    m = re.match(r"([A-Za-z]+)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0)) if m else (ref, 0)


def card(c: Companion, note: str = "", path: Optional[Path] = None, limit: int = CARD_CHARS) -> str:
    head = f"### {c.name or Path(c.path).name}  ({path or c.path}, from {c.source or '?'})"
    if c.error:
        return head + f"\nCould not be read: {c.error}"
    L = [head]
    if note:
        L.append(f"Role (from the user): {note}")
    if c.title or c.subtitle:
        L.append(" - ".join(x for x in (c.title, c.subtitle) if x))
    L += [f"- {b if len(b) <= 400 else b[:400] + '…'}" for b in c.summary[:14]]
    refs = sorted(c.parts, key=_ref_key)
    dnp = sum(1 for r in refs if c.parts[r].get("dnp"))
    L.append(f"Parts ({len(refs)}{f', {dnp} DNP' if dnp else ''}): " + "; ".join(_part_str(r, c.parts[r]) for r in refs))
    power = [n for n in c.nets if POWER.search(n)]
    if power:
        L.append("Power nets: " + ", ".join(power))
    others = [n for n in c.nets if n not in power]
    if others:
        L.append("Other nets: " + ", ".join(others[:40]) + (f" (+{len(others) - 40})" if len(others) > 40 else ""))
    conns = [r for r in refs if CONN.match(r)]
    for r in conns[:8]:
        pins = sorted(f"{m.split('.', 1)[1]}={n}" for n, ms in c.nets.items() for m in ms if m.split(".", 1)[0] == r)
        if pins:
            L.append(f"{r} pins: " + " ".join(pins))
    text = "\n".join(L)
    return text if len(text) <= limit else text[:limit] + "\n… (use linked_design for the rest)"


def system_doc_path(p) -> Optional[Path]:
    rec = p.state().get("system_doc")
    return _abs(p.folder, rec) if rec else None


def context(p) -> str:
    """Appended to the AI's system prompt. Empty when nothing is linked."""
    out = []
    ls = links(p)
    if ls:
        out.append("LINKED DESIGNS - other boards that work together with this one. They are READ ONLY here (you "
                   "cannot edit them); use linked_design for parts, nets, pins or MD sections you need. Keep shared "
                   "values consistent; when a change here affects a linked board, tell the user exactly what to change "
                   "there.")
        for l in ls:
            out.append(card(companion(l["path"]), l["note"], _rel(p.folder, l["path"])))
    sd = system_doc_path(p)
    if sd is not None:
        text = _read(sd)
        if text:
            out.append(f"SYSTEM REQUIREMENTS shared by the linked boards ({sd.name}). Follow them; when a shared value "
                       f"is decided or changes, update the file with update_system_doc:\n"
                       + (text if len(text) <= SYSTEM_DOC_CHARS else text[:SYSTEM_DOC_CHARS] + "\n… (cut)"))
        else:
            out.append(f"SYSTEM REQUIREMENTS file {sd} is missing or empty - use update_system_doc to write it.")
    return "\n\n".join(out)


# ------------------------------------------------------------------ change notices
def fingerprint(c: Companion) -> Dict:
    return {"parts": {r: _part_str(r, d).split("=", 1)[1] for r, d in c.parts.items()},
            "nets": {n: sorted(m) for n, m in c.nets.items()},
            "summary": digest("\n".join([c.title, c.subtitle] + c.summary)), "error": c.error}


def diff(old: Dict, new: Dict, limit: int = 14) -> List[str]:
    if not old:
        return []
    out = []
    op, np_ = old.get("parts", {}), new.get("parts", {})
    added = sorted(set(np_) - set(op), key=_ref_key)
    gone = sorted(set(op) - set(np_), key=_ref_key)
    if added:
        out.append("added " + ", ".join(f"{r} ({np_[r]})" for r in added))
    if gone:
        out.append("removed " + ", ".join(gone))
    for r in sorted(set(op) & set(np_), key=_ref_key):
        if op[r] != np_[r]:
            out.append(f"{r}: {op[r]} -> {np_[r]}")
    on, nn = old.get("nets", {}), new.get("nets", {})
    for n in sorted(set(nn) - set(on)):
        out.append(f"new net {n}")
    for n in sorted(set(on) - set(nn)):
        out.append(f"net {n} removed")
    for n in sorted(set(on) & set(nn)):
        a, b = set(on[n]), set(nn[n])
        if a != b:
            ch = [f"+{x}" for x in sorted(b - a)] + [f"-{x}" for x in sorted(a - b)]
            out.append(f"net {n}: " + " ".join(ch[:6]) + (" …" if len(ch) > 6 else ""))
    if old.get("summary") != new.get("summary"):
        out.append("its description/summary was edited")
    if new.get("error") and new.get("error") != old.get("error"):
        out.append("can no longer be read: " + new["error"])
    return out[:limit] + ([f"… and {len(out) - limit} more"] if len(out) > limit else [])


def changes(p, update: bool = True) -> List[str]:
    """One line per linked design (and the system requirements file) that changed since this project last looked.
    Marks them as seen when update is true."""
    msgs = []
    recs = p.state().get("linked") or []
    dirty = False
    for r in recs:
        path = _abs(p.folder, r["path"])
        c = companion(path)
        fp = fingerprint(c)
        d = diff(r.get("seen") or {}, fp)
        if d:
            msgs.append(f"Linked design {label(path, c)} changed since this project last saw it: " + "; ".join(d)
                        + ". Check whether this board needs a matching change.")
        if r.get("seen") != fp:
            r["seen"] = fp
            dirty = True
    st = p.state()
    sd = system_doc_path(p)
    if sd is not None:
        dg = digest(_read(sd)) if sd.exists() else ""
        if st.get("system_doc_seen") and dg != st.get("system_doc_seen"):
            msgs.append(f"The system requirements file {sd.name} was changed (by you or a linked project).")
        if update and dg != st.get("system_doc_seen"):
            p.save_state(system_doc_seen=dg)
    if update and dirty:
        _save_links(p, recs)
    return msgs


# ------------------------------------------------------------------ the shared requirements file
def set_system_doc(p, path: Optional[Path], create: bool = False, share: bool = True) -> str:
    if path is None:
        p.save_state(system_doc="", system_doc_seen="")
        return "System requirements file unlinked."
    path = Path(path)
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SYSTEM_TEMPLATE, encoding="utf-8")
    p.save_state(system_doc=_rel(p.folder, path), system_doc_seen=digest(_read(path)))
    msg = f"System requirements: {path.name}"
    if share:
        n = 0
        for l in links(p):
            other = _studio_folder(l["path"])
            if other is None:
                continue
            st = _load_state(other)
            if not st.get("system_doc"):
                st["system_doc"] = _rel(other, path)
                _write_state(other, st)
                n += 1
        if n:
            msg += f" (also used by {n} linked project{'s' if n > 1 else ''})"
    return msg


def write_system_doc(p, text: str, mode: str = "append") -> Dict:
    sd = system_doc_path(p)
    if sd is None:
        return {"error": "no system requirements file - the user sets one in 'Linked designs…'"}
    old = _read(sd)
    if old:
        sd.with_name(sd.name + ".bak").write_text(old, encoding="utf-8")
    if mode == "replace":
        new = text.rstrip() + "\n"
    elif mode == "append":
        new = (old.rstrip() + "\n\n" if old.strip() else "") + text.rstrip() + "\n"
    else:
        return {"error": "mode must be append or replace"}
    sd.write_text(new, encoding="utf-8")
    p.save_state(system_doc_seen=digest(new))
    return {"ok": True, "file": str(sd), "chars": len(new), "backup": sd.name + ".bak" if old else None,
            "stamp": time.strftime("%Y-%m-%d %H:%M")}


# ------------------------------------------------------------------ detail for the AI tool
def _md_section(text: str, section: str) -> str:
    """'## heading' (substring match) or a '### REF' part block, up to the next heading of the same level."""
    lines = text.splitlines()
    key = section.strip().lower()
    for lvl in ("### ", "## ", "# "):
        for i, l in enumerate(lines):
            if l.startswith(lvl) and key in l[len(lvl):].lower():
                out = [l]
                for m in lines[i + 1:]:
                    if m.startswith("#") and len(m) - len(m.lstrip("#")) <= len(lvl) - 1:
                        break
                    out.append(m)
                return "\n".join(out)
    return ""


def detail(c: Companion, what: str = "summary", ref: Optional[str] = None, section: Optional[str] = None) -> Dict:
    if c.error:
        return {"error": f"{c.name or c.path}: {c.error}"}
    what = (what or "summary").lower()
    if what == "summary":
        return {"design": c.name, "card": card(c, limit=TOOL_CHARS)}
    if what == "parts":
        return {"design": c.name, "parts": "; ".join(
            _part_str(r, d) + (f" mpn {d['mpn']}" if d.get("mpn") and d["mpn"] not in _part_str(r, d) else "")
            + (f" ({d['note']})" if d.get("note") else "") for r, d in sorted(c.parts.items(), key=lambda x: _ref_key(x[0])))}
    if what == "nets":
        return {"design": c.name, "nets": "\n".join(f"{n}: {' '.join(m)}" for n, m in c.nets.items())[:TOOL_CHARS]}
    if what in ("part", "pin", "pins"):
        if not ref:
            return {"error": "give ref (e.g. C2)"}
        r = ref.strip().upper()
        d = c.parts.get(r) or next((v for k, v in c.parts.items() if k.upper() == r), None)
        if d is None:
            return {"error": f"{c.name} has no part {ref}; parts: " + ", ".join(sorted(c.parts, key=_ref_key))}
        pins = {m.split(".", 1)[1]: n for n, ms in c.nets.items() for m in ms if m.split(".", 1)[0].upper() == r}
        block = _md_section(c.md_text, r) if c.md_text else ""
        return {"design": c.name, "ref": r, **d, "pins": pins or "no nets", **({"md": block[:4000]} if block else {})}
    if what == "md":
        if not c.md_text:
            return {"error": f"{c.name} has no schematic MD"}
        if section:
            s = _md_section(c.md_text, section)
            if s:
                return {"design": c.name, "file": c.md_path.name, "text": s[:TOOL_CHARS]}
        heads = [l for l in c.md_text.splitlines() if l.startswith("## ")]
        if section:
            return {"error": f"no section '{section}'", "sections": heads}
        return {"design": c.name, "file": c.md_path.name, "sections": heads, "text": c.md_text[:TOOL_CHARS]}
    return {"error": "what must be summary, parts, nets, part or md"}
