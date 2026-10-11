"""Tools the Claude agent can call. Every tool works on the validated Design model,
so the AI can't write a bad pin name or a raw XML error: it gets the error back."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import jlc
from .costs import CostTracker
from .lbr_builder import SpecError, build_lbr, extract_spec_from_datasheet, safe_name, SPEC_TOOL
from . import coils
from .library import LibraryIndex
from .model import Design
from .sch_writer import write_sch
from .eagle_cli import default_converted_dir
from .jlc_memory import JlcMemory, keys_for
from .scripts import ScriptStore, default_scripts_dir
from .local import PartMemory, clean_value, match_bom_row
from .importer import import_sch, load_embedded, looks_like_md_schematic, parse_md


@dataclass
class Settings:
    brand: str = "claude"              # claude | openai | xai
    model: str = "claude-sonnet-5-5"
    datasheet_model: str = "claude-sonnet-5-5"
    budget_usd: Optional[float] = 5.0
    extended_fee: float = jlc.DEFAULT_EXTENDED_FEE
    build_qty: int = 10
    lib_dirs: List[str] = field(default_factory=list)
    jlc_csv: str = ""
    eagle_exe: str = ""
    converted_dir: str = ""
    passive_style: str = "US"          # R-US_/C-US vs R-EU_/C-EU symbols
    memory_path: str = ""              # part memory shared by all projects
    jlc_memory_path: str = ""          # remembered JLC/LCSC choices shared by all projects
    trim_history: bool = True
    datasheet_pages: bool = True       # send only pinout/package pages of datasheets
    scripts_dir: str = ""
    lbr_dir: str = ""                  # where new libraries (datasheet parts, coils) are saved; "" = <project>/libs
    lbr_ask: bool = False              # ask where to save every new library
    rules_preset: str = "jlc_standard"  # default design rules for new projects


def _same_md(a: str, b: str) -> bool:
    """Equal apart from the 'last updated' stamp (so opening a project doesn't rewrite an unchanged file)."""
    strip = lambda t: re.sub(r" \(last updated [^)]*\)", "", t)
    return strip(a) == strip(b)


class Project:
    """Everything a session needs: design, libraries, JLC client, costs."""

    def __init__(self, folder: Path, settings: Settings, client=None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.client = client                      # anthropic.Anthropic or None
        self.design_path = self.folder / "design.json"
        self.meta_dir = self.folder / ".eagleai"
        self.meta_dir.mkdir(exist_ok=True)
        self.embedded_dir = self.meta_dir / "embedded_libs"
        self.state_path = self.meta_dir / "state.json"
        self.generated_libs = self.folder / "libs"
        self.generated_libs.mkdir(exist_ok=True)
        self.design = Design.load(self.design_path) if self.design_path.exists() else Design(self.folder.name)
        # an existing project that has never had a Schematic MD linked: ask the user before creating one
        st = self.state()
        self.md_needs_choice = bool(self.design.parts) and not (st.get("md") or {}).get("path")
        self.converted_dir = Path(settings.converted_dir) if settings.converted_dir else default_converted_dir()
        # project folder first (its own .lbr files win), then converted libs, then the Eagle library folders.
        # Every folder is scanned with all its sub-folders.
        dirs = [self.folder, self.converted_dir] + [Path(d) for d in settings.lib_dirs] \
            + ([Path(settings.lbr_dir)] if settings.lbr_dir else [])
        self.index = LibraryIndex(dirs)
        self.costs = CostTracker(self.folder, settings.budget_usd)
        self.jlc = jlc.LocalParts(Path(settings.jlc_csv)) if settings.jlc_csv and Path(settings.jlc_csv).exists() \
            else jlc.JlcSearchClient()
        self.last_candidates: Dict[str, List[jlc.Candidate]] = {}
        self.pending_import: Optional[Dict] = None   # parsed .md waiting for part choices
        self.memory = PartMemory(Path(settings.memory_path) if settings.memory_path
                                 else default_converted_dir().parent / "part_memory.json")
        self.jlc_mem = JlcMemory(Path(settings.jlc_memory_path) if settings.jlc_memory_path
                                 else default_converted_dir().parent / "jlc_memory.json")
        self.listeners: List[Callable[[], None]] = []
        self.scripts = ScriptStore(Path(settings.scripts_dir) if settings.scripts_dir else default_scripts_dir())
        self.ask_script: Optional[Callable[[str, str], bool]] = None   # set by the UI (approval dialog)
        # set by the UI: (part name, suggested path) -> chosen .lbr path, or None if cancelled
        self.ask_lbr_path: Optional[Callable[[str, Path], Optional[Path]]] = None
        # set by the UI: (board, PlacementResult, live: bool) -> "eagle" | "brd" | None (rejected)
        self.ask_placement = None
        self.ask_route = None            # same idea for routing results
        from .safewrite import OverwriteGuard
        self.guard = OverwriteGuard(self.folder)   # asks before any existing file of yours is replaced
        self.bridge_dir: Optional[Path] = None      # Eagle link folder (None = default)

    # ------------------------------------------------------------- new libraries
    def lbr_out(self, name: str, save_to: Optional[str] = None) -> Optional[Path]:
        """Where a new library part goes. save_to (file or folder) wins; else Settings 'New libraries go to'
        (default <project>/libs); with 'ask every time' the user picks the file (an existing .lbr = add to it)."""
        base = Path(self.settings.lbr_dir) if self.settings.lbr_dir else self.generated_libs
        if save_to:
            t = Path(save_to)
            t = t if t.is_absolute() else self.folder / t
            return t if t.suffix.lower() == ".lbr" else t / f"{name}.lbr"
        default = base / f"{name}.lbr"
        if self.settings.lbr_ask and self.ask_lbr_path:
            return self.ask_lbr_path(name, default)
        return default

    # ------------------------------------------------------------- JLC memory
    def jlc_keys(self, ref: str) -> List[str]:
        ps = self.design.parts.get(ref.upper())
        if ps is None:
            return []
        dev, _ = self.design.device_for(self.index, ps.ref)
        prefix = re.sub(r"[^A-Z]", "", (dev.prefix if dev else "").upper()) or re.sub(r"\d", "", ps.ref)
        return keys_for(prefix, ps.value, ps.mpn, dev.package if dev else ps.device, ps.library, ps.deviceset, ps.device)

    def remember_lcsc(self, refs, cand):
        for r in ([refs] if isinstance(refs, str) else refs):
            self.jlc_mem.remember(self.jlc_keys(r.strip()), cand, self.design.name)

    def recall_lcsc(self, ref: str, qty: int):
        """Remembered LCSC for this part, re-checked at JLC. Returns (Candidate, rec) or (None, rec|None)."""
        ps = self.design.parts.get(ref.upper())
        need = max(jlc.rated_volts(f"{ps.value} {ps.note}") or 0, self.min_volts([ref]) or 0) if ps else 0
        rec = self.jlc_mem.recall(self.jlc_keys(ref), need or None)
        if not rec:
            return None, None
        c = self.jlc.lookup(rec["lcsc"])
        if c is not None and c.stock >= qty:
            return c, rec
        return None, rec

    def min_volts(self, refs) -> Optional[float]:
        """Recommended minimum voltage rating (worst of refs), from ratings.py; None if unknown."""
        from .ratings import analyze_part
        vals = []
        for r in refs:
            r = r.strip().upper()
            if r in self.design.parts:
                try:
                    vals.append(analyze_part(self.design, self.index, r).need.get("V"))
                except Exception:
                    pass
        vals = [v for v in vals if v]
        return max(vals) if vals else None

    # ------------------------------------------------------------- state file
    def state(self) -> Dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_state(self, **kw):
        st = self.state()
        st.update(kw)
        self.state_path.write_text(json.dumps(st, indent=2), encoding="utf-8")

    def changed(self):
        self.design.save(self.design_path)
        try:
            self.sync_md()
        except Exception as exc:          # the document must never block a design change
            self.md_status = f"could not write the MD: {exc}"
        for cb in self.listeners:
            cb()

    # ------------------------------------------------------------- Markdown document
    md_status: str = ""
    md_conflict: bool = False

    def md_path(self) -> Path:
        rec = self.state().get("md", {}) or {}
        if rec.get("path"):
            p = Path(rec["path"])
            return p if p.is_absolute() else self.folder / p
        name = re.sub(r'[<>:"/\\|?*]+', "_", self.design.name or self.folder.name).strip() or "schematic"
        p = self.folder / f"{name}.md"
        if p.exists():                    # someone else's MD (e.g. the one this design was imported from)
            p = self.folder / f"{name}_studio.md"
        return p

    def _md_rec(self, path: Path, text: str):
        from .md_writer import digest
        try:
            rel = str(Path(path).relative_to(self.folder))
        except ValueError:
            rel = str(path)               # a file outside the project folder is kept by its full path
        self.save_state(md={"path": rel, "digest": digest(text)})

    def write_owned(self, path: Path, text: str, what: str) -> Optional[Path]:
        """Write a file the studio keeps up to date (e.g. design_rules.dru). If it exists and wasn't written by the
        studio - or was edited since - the overwrite guard asks first."""
        from .md_writer import digest
        path = Path(path)
        owned = self.state().get("owned", {})
        rec = owned.get(path.name)
        if path.exists():
            cur = path.read_text(encoding="utf-8", errors="replace")
            if cur == text:
                return path
            if rec != digest(cur):
                path = self.guard.target(path, what, ["The studio did not write this version of the file."])
                if path is None:
                    return None
        path.write_text(text, encoding="utf-8")
        owned[path.name] = digest(text)
        self.save_state(owned=owned)
        return path

    def adopt_md(self, path: Path) -> str:
        """Make an existing (older / AI-written) MD the project's Schematic MD: keep its title, subtitle, summary,
        citations and section names, back it up once as <file>.orig, then keep it in sync with the design."""
        from .md_writer import extract_doc
        path = Path(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        prose = extract_doc(text)
        for k, v in prose.items():
            if isinstance(v, dict):
                self.design.doc.setdefault(k, {})
                for kk, vv in v.items():
                    self.design.doc[k].setdefault(kk, vv)
            elif not self.design.doc.get(k):
                self.design.doc[k] = v
        orig = path.with_name(path.name + ".orig")
        if not orig.exists():
            orig.write_text(text, encoding="utf-8")
        self._md_rec(path, text)
        self.md_needs_choice = False
        self.design.save(self.design_path)
        return self.sync_md(force=True)

    def create_md(self) -> str:
        """The user chose not to link an older MD: write a new one."""
        self.md_needs_choice = False
        self.save_state(md={})
        return self.sync_md(force=True)

    def md_text(self) -> str:
        from . import __version__
        from .md_writer import write_md
        return write_md(self.design, self.index, __version__, time.strftime("%Y-%m-%d %H:%M"))

    def sync_md(self, force: bool = False) -> str:
        """Rewrite the MD from the design. If the file was edited outside the studio, leave it alone (unless
        force) and flag a conflict so the user can import those edits or overwrite them."""
        from .md_writer import digest
        if not self.design.parts:
            return ""                     # never replace a document with an empty one
        if self.pending_import:
            return "import in progress - the MD is written when it finishes"
        if self.md_needs_choice:
            self.md_status = "waiting for your choice: open an existing MD for this project, or create one"
            return self.md_status
        path = self.md_path()
        rec = self.state().get("md", {}) or {}
        text = self.md_text()
        if path.exists() and not force:
            cur = path.read_text(encoding="utf-8", errors="replace")
            if rec.get("digest") and digest(cur) != rec["digest"] and not _same_md(cur, text):
                self.md_conflict = True
                self.md_status = f"{path.name} was edited outside the studio - import those edits or overwrite"
                return self.md_status
            if _same_md(cur, text):
                self.md_conflict = False
                self.md_status = f"{path.name} is up to date"
                if rec.get("digest") != digest(cur):
                    self._md_rec(path, cur)
                return self.md_status
        path.write_text(text, encoding="utf-8")
        self._md_rec(path, text)
        self.md_conflict = False
        self.md_status = f"{path.name} updated"
        return self.md_status

    def load_libraries(self, progress=None) -> str:
        cache = self.meta_dir / "library_cache.json"
        ok, msg = self.index.load_cache(cache)
        if not ok:
            self.index.scan(progress)
            self.index.save_cache(cache)
        n = load_embedded(self.index, self.embedded_dir)
        # libs generated in earlier sessions stay flagged until the user verifies them
        for f in self.generated_libs.glob("*.lbr"):
            if (self.generated_libs / (f.stem + ".verified")).exists():
                self.index.unverified.discard(f.stem)
            else:
                self.index.unverified.add(f.stem)
        if self.design.parts:
            try:
                self.sync_md()            # the MD needs the library to name pins - (re)write it now
            except Exception as exc:
                self.md_status = f"could not write the MD: {exc}"
        return self.index.stats() + (" (from cache)" if ok else "")

    def rescan(self, progress=None) -> str:
        (self.meta_dir / "library_cache.json").unlink(missing_ok=True)
        return self.load_libraries(progress)

    # ------------------------------------------------------------- importing
    def design_candidates(self) -> List[Path]:
        """Existing designs in the project folder (top level and one sub-folder deep)."""
        out = []
        for pat in ("*.sch", "*/*.sch", "*.md", "*/*.md"):
            for f in sorted(self.folder.glob(pat)):
                if ".eagleai" in f.parts or f.name.lower().startswith("readme"):
                    continue
                if f.suffix.lower() == ".sch" or looks_like_md_schematic(f):
                    out.append(f)
        return out

    def sch_candidates(self) -> List[Path]:
        return [f for f in self.design_candidates() if f.suffix.lower() == ".sch"]

    def md_candidates(self) -> List[Path]:
        return [f for f in self.design_candidates() if f.suffix.lower() == ".md"]

    def best_pick(self, files: List[Path], match: Optional[Path] = None) -> Optional[Path]:
        """The most likely file: same name as `match` (v2.sch -> v2.md), else in the project folder itself,
        else the newest."""
        if not files:
            return None
        if match is not None:
            same = [f for f in files if f.stem.lower() == match.stem.lower()]
            if same:
                return same[0]
        top = [f for f in files if f.parent == self.folder] or files
        return max(top, key=lambda f: f.stat().st_mtime)

    def externally_changed_sch(self) -> List[Path]:
        """.sch files that changed since we last generated/imported them (edited in Eagle)."""
        known = self.state().get("sch_mtimes", {})
        out = []
        for f in self.folder.glob("*.sch"):
            m = known.get(f.name)
            if m is not None and abs(f.stat().st_mtime - m) > 1:
                out.append(f)
        return out

    def _remember_sch(self, path: Path):
        st = self.state().get("sch_mtimes", {})
        st[path.name] = path.stat().st_mtime
        self.save_state(sch_mtimes=st)

    def import_sch(self, path: Path) -> List[str]:
        d, report = import_sch(path, self.index, keep_libs_in=self.embedded_dir)
        self.design = d
        if path.parent == self.folder:
            self._remember_sch(path)
        self.save_state(source=str(path))
        self.changed()
        return report

    def import_md(self, path: Path) -> Dict:
        from .md_writer import extract_doc
        parsed = parse_md(path)
        self.pending_import = parsed
        self.design = Design(re.sub(r"[^\w\-]+", "_", parsed["title"])[:40] or path.stem)
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        self.design.doc = extract_doc(text)
        orig = Path(path).with_name(Path(path).name + ".orig")
        if not orig.exists():
            orig.write_text(text, encoding="utf-8")
        self._md_rec(Path(path), text)       # this file stays the project's Schematic MD
        self.md_needs_choice = False
        self.save_state(source=str(path))
        self.changed()
        return parsed

    def auto_place_bom(self, bom: List[Dict]) -> Tuple[List[str], List[Dict]]:
        """Place every BOM row code can decide on its own. Returns (log lines, rows left for Claude)."""
        done, left = [], []
        for row in bom:
            hit, how = match_bom_row(row, self.index, self.memory, self.design, self.settings.passive_style)
            if hit is None:
                left.append(dict(row, why=how))
                continue
            for ref in row["refs"]:
                if ref in self.design.parts:
                    continue
                pre = re.sub(r"[^A-Z]", "", ref)
                value = clean_value(row.get("value", "")) if pre in ("R", "C", "L") else (row.get("value", "") or "")
                mpn = "" if row.get("part_number", "").lower() == "generic" else row.get("part_number", "")
                ps, err = self.design.add_part(self.index, *hit, ref=ref, value=value, mpn=mpn)
                if err:
                    left.append(dict(row, why=err))
                    break
                self.memory.learn(self.index, *hit, mpn=mpn)
            else:
                done.append(f"{', '.join(row['refs'])} -> {'/'.join(hit)} ({how})")
        self.changed()
        return done, left

    def output_path_for(self, name: str) -> Tuple[Path, Optional[str]]:
        """Never overwrite a .sch you edited in Eagle: write NAME_ai.sch instead."""
        target = self.folder / name
        if not target.exists():
            return target, None
        known = self.state().get("sch_mtimes", {}).get(target.name)
        if known is not None and abs(target.stat().st_mtime - known) <= 1:
            return target, None
        stem, i = target.stem, 1
        alt = self.folder / f"{stem}_ai.sch"
        while alt.exists() and self.state().get("sch_mtimes", {}).get(alt.name) is None:
            i += 1
            alt = self.folder / f"{stem}_ai{i}.sch"
        return alt, f"{target.name} was edited outside the app, so it was left alone; wrote {alt.name}"


# --------------------------------------------------------------------- tools
def _t(name, desc, props, required=()):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": list(required)}}


S = {"type": "string"}
TOOL_DEFS = [
    _t("search_library", "Search the installed Eagle libraries for a part. Always do this before add_part; "
       "never guess library/deviceset names.",
       {"query": S, "prefix": {"type": "string", "description": "optional ref prefix filter: R, C, U, J, D..."}}, ["query"]),
    _t("get_pinout", "List the real pins (name + pad numbers) of a library device. Call before connecting a part you "
       "haven't seen.", {"library": S, "deviceset": S, "device": S}, ["library", "deviceset"]),
    _t("add_part", "Place a library part in the design. Returns the reference designator.",
       {"library": S, "deviceset": S, "device": S, "ref": S, "value": S, "mpn": S}, ["library", "deviceset"]),
    _t("add_parts", "Add MANY parts in one call (preferred over repeated add_part).",
       {"parts": {"type": "array", "items": {"type": "object", "required": ["library", "deviceset"], "properties": {
           "library": S, "deviceset": S, "device": S, "ref": S, "value": S, "mpn": S}}}}, ["parts"]),
    _t("add_passive", "Add resistor/capacitor/inductor by value and package size; the library part is picked "
       "locally (no search needed). kind = R, C or L.",
       {"kind": S, "value": S, "package": {"type": "string", "description": "0402/0603/0805/1206..."},
        "ref": S, "count": {"type": "integer"}, "polarized": {"type": "boolean"}}, ["kind", "value", "package"]),
    _t("remove_part", "Remove a part and all its connections.", {"ref": S}, ["ref"]),
    _t("set_value", "Set value / manufacturer part number / LCSC number / note / do-not-populate on a part.",
       {"ref": S, "value": S, "mpn": S, "note": S, "lcsc": S, "dnp": {"type": "boolean"}}, ["ref"]),
    _t("connect", "Put pins on a named net. Pins are REF.PIN using the pin name or pad number. Invalid pins are "
       "rejected with the list of valid pins.", {"net": S, "pins": {"type": "array", "items": S}}, ["net", "pins"]),
    _t("connect_nets", "Connect MANY nets in one call: {net_name: [REF.PIN, ...]} (preferred over repeated connect).",
       {"nets": {"type": "object", "additionalProperties": {"type": "array", "items": S}}}, ["nets"]),
    _t("disconnect", "Remove a single pin (REF.PIN) from whatever net it is on.", {"pin": S}, ["pin"]),
    _t("get_design", "Current parts and nets.", {}),
    _t("run_checks", "Full problem list incl. warnings. (Errors are already reported after every edit as design_errors.)", {}),
    _t("jlc_find", "Find the cheapest in-stock JLCPCB/LCSC part for a design part (by ref) or free text.",
       {"ref": S, "query": S, "package": {"type": "string", "description": "e.g. 0603"}, "qty": {"type": "integer"}}),
    _t("jlc_assign", "Store an LCSC number on a part (ref may be 'R1,R2'). The number is checked against JLC first; "
       "an unknown number is refused.", {"ref": S, "lcsc": S}, ["ref", "lcsc"]),
    _t("jlc_lookup", "Check one LCSC number at JLC: part, package, stock, price tiers, Basic/Extended.",
       {"lcsc": S}, ["lcsc"]),
    _t("board_state", "The board as it is now: outline, every part's position/rotation/side/size. Live from Eagle if the "
       "user synced with the Eagle link, else the saved .brd.", {"brd": S}),
    _t("layout_rules_from_datasheet", "Read an IC datasheet's layout guidelines (only those pages) and return which "
       "external part goes next to which pin and how close, with quotes. Then map roles to refs for place_parts.",
       {"pdf_path": S, "ic_ref": S, "hint": S}, ["pdf_path"]),
    _t("place_parts", "Place parts on the board. You give intent, code finds positions (inside the outline, no overlaps, "
       "short connections). rules: [{ref, near, pin, own_pin, max_dist (mm)} | {ref, edge: left|right|top|bottom} | "
       "{ref, at: [x,y]} | {ref, fixed: true}] plus optional side/angle per rule. clearance = space between parts "
       "(default 1 mm, leaves room for traces; raise it if routing fails). keep = refs not to move; move_only = "
       "only these. Locked parts and board-sized parts (e.g. a PCB coil) never move. The user sees a preview and "
       "applies it (Eagle open: through the Eagle link; Eagle closed: the .brd is rewritten, backup kept).",
       {"rules": {"type": "array", "items": {"type": "object"}}, "side": {"enum": ["top", "bottom"]},
        "keep": {"type": "array", "items": S}, "move_only": {"type": "array", "items": S},
        "clearance": {"type": "number"}, "edge_clearance": {"type": "number"}, "brd": S}),
    _t("set_doc", "Prose for the project's Markdown document (the MD is regenerated from the design automatically - "
       "never write it yourself). title, subtitle, summary (bullets: configuration, power notes), citations "
       "{ref or MPN: 'Manufacturer part datasheet, rev - key pins'}, sections {ref: 'Power input'} to group parts.",
       {"title": S, "subtitle": S, "summary": {"type": "array", "items": S}, "citations": {"type": "object"},
        "sections": {"type": "object"}}),
    _t("design_rules", "Get or set this project's PCB design rules (default: JLC standard). preset = jlc_standard | "
       "jlc_minimum_1oz | jlc_standard_2oz | jlc_4layer; dru_path = an Eagle .dru to load; overrides = {field: value} "
       "(mm). Placement and routing always use these.",
       {"preset": S, "dru_path": S, "overrides": {"type": "object"}}),
    _t("route_board", "Route the board's traces with the design rules (2 layers, A* router in code). You decide intent: "
       "net_classes [{name, nets:[...], current_a or width (mm), clearance}] - width from current uses IPC-2221; "
       "order = nets to route first (critical/short loops); skip_nets (e.g. a net left for a pour); ground_pour = net "
       "name for a copper pour (pour_layers: bottom/top). The result is DRC-checked in code; the user approves a "
       "preview, then it goes to Eagle (link) or into the .brd.",
       {"net_classes": {"type": "array", "items": {"type": "object"}}, "order": {"type": "array", "items": S},
        "skip_nets": {"type": "array", "items": S}, "ground_pour": S,
        "pour_layers": {"type": "array", "items": {"enum": ["bottom", "top"]}}, "grid": {"type": "number"},
        "time_limit": {"type": "number"}, "brd": S}),
    _t("build_coil", "Make a PCB-trace coil (spiral inductor / wireless-power coil) as an Eagle library part. The copper "
       "is drawn by exact code - never draw coil traces yourself. params: name, shape (racetrack|round), length & width "
       "(board, mm; round: width = diameter), clearance (edge to outer trace edge, room for pads), trace, gap, turns "
       "(per layer), layers (top|both; both = series, centre pad = CT), center_tap, start_angle, pad_dia, pad_drill, "
       "center_dia, center_drill, outline, copper_oz. Returns DC resistance and (round) inductance estimates. "
       "save_to: optional .lbr file (existing library = add to it) or folder.",
       {"params": {"type": "object"}, "save_to": S}, ["params"]),
    _t("build_lbr", "Create a library part (package + symbol + device) whenever the design needs one that search_library "
       "doesn't have - from a datasheet's land pattern you know, from the user's description, or designed from "
       "requirements (connectors, pads, test points, antennas, shields, odd mechanical parts). Use a template type "
       "for standard packages; 'custom' for anything else: explicit pads plus 'graphics' (copper, outline, silkscreen, "
       "holes...). All geometry in mm, origin = part centre, y up. The result includes a geometry report (closest pads, "
       "overlaps, drill / annular ring) - fix every problem and rebuild with the same name (it replaces). Output is "
       "flagged UNVERIFIED. save_to = .lbr file (existing = add to it) or folder, only if the user named one.",
       {"spec": SPEC_TOOL["input_schema"], "save_to": S}, ["spec"]),
    _t("build_lbr_from_datasheet", "Read a datasheet PDF (path inside the project's datasheets folder or absolute) "
       "and create the library part from it. Output is flagged UNVERIFIED.",
       {"pdf_path": S, "hint": {"type": "string", "description": "which part number/variant"},
        "save_to": {"type": "string", "description": ".lbr file (existing = add to it) or folder; only if the user named one"}},
       ["pdf_path"]),
    _t("generate_sch", "Write the Eagle 7.4 .sch file.", {"filename": S}),
    _t("list_scripts", "List the local helper scripts that already exist (reuse before writing new ones).", {}),
    _t("write_script", "Save a small Python helper (runs locally on the user's PC). Use for calculations, parsing "
       "files, repetitive edits. It can read design.json via env EAGLEAI_DESIGN and `import eagleai`. Print results "
       "to stdout. The user must approve it before the first run.",
       {"name": S, "description": S, "code": S}, ["name", "code"]),
    _t("run_script", "Run a saved helper script with optional command-line args; returns stdout. If the script "
       "changed design.json, the app reloads it.", {"name": S, "args": {"type": "array", "items": S}}, ["name"]),
    _t("import_apply_nets", "Markdown import, step 2: after every BOM ref has been added with add_part (same ref), "
       "connect all nets parsed from the markdown. Returns pins that didn't match the chosen library part.", {}),
    _t("set_net_voltage", "Worst-case MAX voltage of nets (to GND), for part ratings in the BOM/MD: input tolerance "
       "(car 12V -> 14.4 running, surge = the TVS clamp), AC/resonant peaks (tank peak), switch nodes. 0 = ground. "
       "null removes it. Nets named 3V3/+12V/VCC9/GND are guessed from the name until set.",
       {"nets": {"type": "object", "additionalProperties": {"type": ["number", "null"]},
                 "description": "{\"+12V\": 14.4, \"DRAIN1\": 28}"}}, ["nets"]),
    _t("set_stress", "What a part really sees, where net voltages can't tell (LED / series / sense resistors, tank "
       "capacitors, inductor or fuse current): V across it, W it dissipates, A through it (worst case, not the "
       "rating). The studio derates it and recommends the minimum rating. {} clears it.",
       {"parts": {"type": "object", "description": "{\"R1\": {\"W\": 0.06, \"why\": \"20 mA x 150R\"}, "
                  "\"L1\": {\"A\": 0.4}, \"C2\": {\"V\": 28}}"}}, ["parts"]),
    _t("get_ratings", "Recommended minimum ratings for every part (resistor watts, capacitor volts, inductor amps) "
       "worked out from net voltages and set_stress, the rating chosen (value/note/package), and what is unknown.",
       {}),
    _t("linked_design", "Read a LINKED design (another board that works with this one; read only). what: summary | "
       "parts | nets | part (needs ref: its value, pins and nets, MD block) | md (optional section: a heading or a "
       "part ref).",
       {"name": {"type": "string", "description": "linked design name; optional if only one is linked"},
        "what": {"type": "string", "enum": ["summary", "parts", "nets", "part", "md"]}, "ref": S, "section": S}),
    _t("update_system_doc", "Edit the SYSTEM REQUIREMENTS file shared by the linked boards (shared values, "
       "interfaces, cross-board checks). append adds text at the end; replace rewrites the whole file (send it all). "
       "A .bak backup is kept.",
       {"text": S, "mode": {"type": "string", "enum": ["append", "replace"]}}, ["text"]),
]
LINK_TOOLS = {"linked_design", "update_system_doc"}


MUTATING = {"run_script", "add_part", "add_parts", "add_passive", "remove_part", "set_value", "connect", "connect_nets",
            "disconnect", "import_apply_nets", "build_lbr", "build_lbr_from_datasheet", "build_coil",
            "set_net_voltage", "set_stress"}


def dispatch(proj: Project, name: str, args: Dict) -> Dict:
    try:
        fn = globals().get(f"tool_{name}")
        if fn is None:
            return {"error": f"unknown tool {name}"}
        out = fn(proj, **args)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # report to the model instead of crashing the loop
        return {"error": f"{type(exc).__name__}: {exc}"}
    if name in MUTATING and isinstance(out, dict):
        # checks run locally after every edit, so Claude doesn't spend a round trip on run_checks
        errs = [str(x) for x in proj.design.validate(proj.index) if x.level == "error"]
        out["design_errors"] = errs[:8] + ([f"+{len(errs) - 8} more"] if len(errs) > 8 else []) if errs else "none"
    return out


def tool_search_library(p: Project, query, prefix=None):
    hits = p.index.search(query, prefix, limit=10)
    if not hits:
        return {"count": 0, "hint": "no match. Try fewer words, or build_lbr_from_datasheet if the part isn't installed."}
    # compact one-line results: library/deviceset/device [package] - description
    return {"results": [f"{e['library']}/{e['deviceset']}/{e['device']} [{e['package']}] {e['prefix']} - "
                        f"{e['description'][:60]}" for e in hits]}


def tool_get_pinout(p: Project, library, deviceset, device=None):
    dev, err = p.index.find_device(library, deviceset, device)
    if dev is None:
        return {"error": err}
    gates = len(dev.gates) > 1
    pins = "; ".join(f"{x.name}[{','.join(x.pads)}]{x.direction}" + (f"@{x.gate}" if gates else "") for x in dev.pins)
    return {"id": dev.id, "package": dev.package, "prefix": dev.prefix, "pins": pins}


def _learn(p: Project, ps):
    try:
        p.memory.learn(p.index, ps.library, ps.deviceset, ps.device, ps.mpn, ps.value)
    except Exception:
        pass


def tool_add_part(p: Project, library, deviceset, device=None, ref=None, value="", mpn=""):
    ps, err = p.design.add_part(p.index, library, deviceset, device, ref, value, mpn)
    if err:
        return {"error": err}
    _learn(p, ps)
    p.changed()
    return {"ref": ps.ref, "device": ps.device}


def tool_add_parts(p: Project, parts):
    """Batch version: many parts in one call."""
    added, errors = [], []
    for spec in parts:
        try:
            ps, err = p.design.add_part(p.index, spec["library"], spec["deviceset"], spec.get("device"),
                                        spec.get("ref"), spec.get("value", ""), spec.get("mpn", ""))
        except KeyError as exc:
            ps, err = None, f"missing {exc}"
        if err:
            errors.append(f"{spec.get('ref') or spec.get('deviceset')}: {err}")
        else:
            _learn(p, ps)
            added.append(ps.ref)
    p.changed()
    return {"added": added, **({"errors": errors} if errors else {})}


def tool_add_passive(p: Project, kind, value, package, ref=None, count=1, polarized=False):
    """R/C/L picked locally from the libraries (no search round trip)."""
    from .local import clean_value, pick_passive
    kind = kind.upper()[:1]
    hit = pick_passive(p.index, kind, package, bool(polarized), p.memory, p.design, p.settings.passive_style)
    if hit is None:
        return {"error": f"no {kind} in {package} found in the libraries - use search_library"}
    refs = []
    for i in range(max(1, int(count))):
        ps, err = p.design.add_part(p.index, *hit, ref=ref if count == 1 else None, value=clean_value(value))
        if err:
            return {"error": err, "added": refs}
        refs.append(ps.ref)
    p.changed()
    return {"added": refs, "part": "/".join(hit)}


def tool_remove_part(p: Project, ref):
    err = p.design.remove_part(ref)
    p.changed()
    return {"error": err} if err else {"ok": True}


def tool_set_value(p: Project, ref, value=None, mpn=None, note=None, lcsc=None, dnp=None):
    ps = p.design.parts.get(ref.upper())
    if ps is None:
        return {"error": f"no part {ref}"}
    if lcsc is not None:
        lc = lcsc.strip().upper()
        ps.lcsc = lc if (not lc or lc.startswith("C")) else f"C{lc}"
    if dnp is not None:
        ps.dnp = bool(dnp)
    if value is not None:
        ps.value = value
    if mpn is not None:
        ps.mpn = mpn
        _learn(p, ps)
    if note is not None:
        ps.note = note
    p.changed()
    return {"ok": True}


def tool_connect(p: Project, net, pins):
    added, errors = p.design.connect(p.index, net, pins)
    p.changed()
    out: Dict = {"net": net, "added": len(added)}
    if errors:
        out["errors"] = errors
    return out


def tool_connect_nets(p: Project, nets):
    """Batch version: {"VIN": ["U1.VIN","C1.1"], "GND": [...]} in one call."""
    total, errors = 0, []
    for net, pins in nets.items():
        a, e = p.design.connect(p.index, net, pins)
        total += len(a)
        errors += e
    p.changed()
    return {"pins_added": total, **({"errors": errors} if errors else {})}


def tool_disconnect(p: Project, pin):
    n = p.design.disconnect(pin)
    p.changed()
    return {"removed": n}


def tool_get_design(p: Project):
    """Compact text form - a fraction of the tokens of JSON."""
    parts = "; ".join(f"{s.ref}={s.deviceset}{('/' + s.device) if s.device else ''}"
                      f"{(' ' + s.value) if s.value else ''}" for s in p.design.parts.values())
    nets = "\n".join(f"{n}: {' '.join(m)}" for n, m in p.design.nets.items())
    return {"parts": parts, "nets": nets}


def tool_run_checks(p: Project):
    from .ratings import problems as rating_problems
    probs = [str(x) for x in p.design.validate(p.index)]
    probs += [f"[{lvl}] {where}: {msg}" for lvl, where, msg in rating_problems(p.design, p.index)]
    return {"problems": probs or "none"}


def tool_set_net_voltage(p: Project, nets):
    done, errors = [], []
    for net, v in (nets or {}).items():
        if v is None or v == "":
            p.design.net_volts.pop(net, None)
            done.append(f"{net}: cleared")
            continue
        try:
            v = float(str(v).upper().rstrip("V").strip())
        except ValueError:
            errors.append(f"{net}: '{v}' is not a number of volts")
            continue
        if net not in p.design.nets:
            errors.append(f"{net}: no such net (saved anyway)")
        p.design.net_volts[net] = v
        done.append(f"{net}={v:g}V")
    p.changed()
    return {"set": done, **({"errors": errors} if errors else {})}


def tool_set_stress(p: Project, parts):
    done, errors = [], []
    for ref, st in (parts or {}).items():
        ps = p.design.parts.get(ref.upper())
        if ps is None:
            errors.append(f"no part {ref}")
            continue
        clean = {}
        for k, v in (st or {}).items():
            if k.lower() == "why":
                clean["why"] = str(v)[:200]
            elif k.upper() in ("V", "W", "A"):
                try:
                    clean[k.upper()] = float(v)
                except (TypeError, ValueError):
                    errors.append(f"{ref}.{k}: '{v}' is not a number")
            else:
                errors.append(f"{ref}: unknown key {k} (use V, W, A, why)")
        ps.stress = clean
        done.append(ps.ref)
    p.changed()
    from .ratings import analyze
    rs = analyze(p.design, p.index)
    return {"set": done, "ratings": {r: rs[r].detail() for r in done if r in rs},
            **({"errors": errors} if errors else {})}


def tool_get_ratings(p: Project):
    from .ratings import analyze, unknown_nets
    rs = analyze(p.design, p.index)
    rows = [f"{ref}: {r.detail()}" for ref, r in rs.items() if r.need or r.missing or r.problems]
    out = {"ratings": rows or "no passives with a rating to work out"}
    nets = unknown_nets(p.design, rs)
    if nets:
        out["set_net_voltage_for"] = nets
    from .ratings import net_voltage
    guessed = [f"{n}={v:g}V" for n in sorted(p.design.nets)
               for v, src in [net_voltage(p.design, n)] if src == "name" and v]
    if guessed:
        out["guessed_from_names"] = guessed
    return out


def tool_list_scripts(p: Project):
    return {"scripts": p.scripts.list() or "none yet"}


def tool_write_script(p: Project, name, code, description=""):
    return p.scripts.write(name, code, description)


def tool_run_script(p: Project, name, args=None):
    before = p.design_path.stat().st_mtime if p.design_path.exists() else 0
    p.design.save(p.design_path)
    before = p.design_path.stat().st_mtime
    out = p.scripts.run(name, args or [], p.folder, p.design_path, p.ask_script)
    if p.design_path.exists() and p.design_path.stat().st_mtime != before:
        p.design = Design.load(p.design_path)
        p.changed()
        out["note"] = "design.json was changed by the script and reloaded"
    return out


READ_ONLY_TOOLS = {"search_library", "get_pinout", "get_design", "run_checks", "list_scripts", "jlc_find", "jlc_lookup",
                   "board_state", "linked_design", "get_ratings"}


def tool_linked_design(p: Project, name=None, what="summary", ref=None, section=None):
    from . import linked
    c, err = linked.find(p, name)
    if c is None:
        return {"error": err}
    return linked.detail(c, what, ref, section)


def tool_update_system_doc(p: Project, text, mode="append"):
    from . import linked
    return linked.write_system_doc(p, text, mode)


def tool_jlc_find(p: Project, ref=None, query=None, package=None, qty=None):
    qty = int(qty or p.settings.build_qty)
    if ref:
        ps = p.design.parts.get(ref.upper())
        if ps is None:
            return {"error": f"no part {ref}"}
        dev, _ = p.design.device_for(p.index, ps.ref)
        pkg = package or (dev.package if dev else "")
        prefix = re.sub(r"[^A-Z]", "", (dev.prefix if dev else "").upper()) or re.sub(r"[^A-Z]", "", ps.ref.upper())
        ranked, how = jlc.best_for_part(p.jlc, prefix, ps.value, ps.mpn, pkg, qty, p.settings.extended_fee,
                                        names=(ps.deviceset, ps.device), note=ps.note,
                                        min_volts=p.min_volts([ps.ref]))
        try:
            mc, rec = p.recall_lcsc(ps.ref, qty)
        except Exception:
            mc, rec = None, None
        if mc is not None:
            ranked = [mc] + [c for c in ranked if c.lcsc != mc.lcsc]
            how = f"remembered from your earlier projects ({rec.get('project', '')}), still in stock; then {how}"
        elif rec:
            how = f"your usual {rec['lcsc']} is out of stock now; {how}"
    else:
        if not query:
            return {"error": "give ref or query"}
        ranked, how = jlc.rank(p.jlc.find(query), qty, extended_fee=p.settings.extended_fee), f"text '{query}'"
    p.last_candidates[(ref or query).upper()] = ranked
    return {"searched": how, "qty": qty, "note": f"ranked by unit price x qty (+${p.settings.extended_fee} for Extended parts), in stock only",
            "top": [dict(c.brief(qty), line_cost=round(c.line_cost(qty, p.settings.extended_fee) or 0, 4))
                    for c in ranked[:5]]}


def tool_jlc_lookup(p: Project, lcsc):
    try:
        c = p.jlc.lookup(lcsc)
    except Exception as exc:
        return {"error": f"JLC lookup failed: {exc}"}
    if c is None:
        return {"error": f"{lcsc} is not listed at JLC"}
    q = p.settings.build_qty
    return dict(c.brief(q), unit_price_at_build_qty=c.unit_price(q), tiers=c.tiers[:6])


def tool_jlc_assign(p: Project, ref, lcsc):
    refs = [r.strip().upper() for r in str(ref).split(",") if r.strip()]
    missing = [r for r in refs if r not in p.design.parts]
    if missing:
        return {"error": f"no part {', '.join(missing)}"}
    lcsc = lcsc.strip().upper()
    lcsc = lcsc if lcsc.startswith("C") else f"C{lcsc}"
    warn = ""
    try:
        c = p.jlc.lookup(lcsc)
        if c is None:
            return {"error": f"{lcsc} is not listed at JLC - check the number (nothing assigned)"}
        if c.stock < p.settings.build_qty:
            warn = f"only {c.stock} in stock"
        p.remember_lcsc(refs, c)
    except Exception as exc:
        warn = f"could not verify ({str(exc)[:60]})"
    for r in refs:
        p.design.parts[r].lcsc = lcsc
    p.changed()
    return {"ok": True, "refs": refs, "lcsc": lcsc, **({"warning": warn} if warn else {})}


def _register(p: Project, path: Path, warnings: List[str], spec: Dict, unverified: bool = True):
    geometry = spec.pop("_geometry", None)
    p.index.add_file(path, unverified=unverified)
    # make the cached scan stale so the next start re-reads
    cache = p.folder / "library_cache.json"
    if cache.exists():
        cache.unlink()
    p.changed()
    return {"library": path.stem, "deviceset": spec["name"], "path": str(path), "warnings": warnings,
            "status": ("UNVERIFIED - a human must check the footprint against the datasheet drawing" if unverified
                       else "generated from exact geometry - check it in Eagle before ordering boards"),
            "pins": tool_get_pinout(p, path.stem, safe_name(spec["name"]), "").get("pins", ""),
            **({"geometry": geometry} if geometry else {})}


def _guard_lbr(p: Project, out: Path, part: str) -> Optional[Path]:
    """Adding a part to an existing library modifies your file: back it up. Replacing a part of the same name
    asks first ("new" writes a separate library file instead)."""
    out = Path(out)
    if not out.exists():
        return out
    try:
        root = ET.parse(out).getroot()
        names = {e.get("name") for tag in ("package", "symbol", "deviceset") for e in root.iter(tag)}
    except ET.ParseError:
        names = set()
    if part in names:
        return p.guard.target(out, f"library part '{part}' (it already exists in {out.name} and would be replaced)")
    p.guard.backup(out)
    return out


def tool_build_lbr(p: Project, spec, save_to=None):
    try:
        out = p.lbr_out(safe_name(spec.get("name", "")), save_to)
        out = _guard_lbr(p, out, safe_name(spec.get("name", ""))) if out else None
        if out is None:
            return {"error": "cancelled by the user - no library was written"}
        path, warnings = build_lbr(spec, out_path=out)
    except SpecError as exc:
        return {"error": f"spec rejected: {exc}"}
    return _register(p, path, warnings, spec)


# ------------------------------------------------------------------ board / placement
def _board_files(p: Project) -> List[Path]:
    fs = [f for f in p.folder.rglob("*.brd") if ".eagleai" not in f.parts and "before_placement" not in f.name]
    return sorted(fs, key=lambda f: -f.stat().st_mtime)


def load_board(p: Project, brd: Optional[str] = None):
    """Live state from the Eagle link (if it's this project's board and fresh), else the saved .brd."""
    from . import board as bmod, eagle_link
    kind, st, age = eagle_link.state(p.bridge_dir)
    files = [Path(brd)] if brd else _board_files(p)
    if kind == "board" and age is not None and age < 6 * 3600:
        live = bmod.read_state(st)
        same = not files or any(Path(live.path).name.lower() == f.name.lower() for f in files)
        if same and live.elements:
            return live, f"live from Eagle ({int(age // 60)} min ago)"
    if not files:
        raise FileNotFoundError("no .brd in the project - create the board in Eagle (BOARD command) and save it, "
                                "or press the Eagle link key with the board open")
    f = files[0] if files[0].is_absolute() else p.folder / files[0]
    return bmod.read_brd(f), f"saved file {f.name}"


def tool_board_state(p: Project, brd=None):
    b, src = load_board(p, brd)
    x0, y0, x1, y1 = b.outline
    return {"source": src, "board": Path(b.path).name, "outline_mm": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            "has_outline": b.has_outline, "signals": len(b.signals),
            "elements": "; ".join(f"{e.name}({e.package}) @({e.x:g},{e.y:g}) {'M' if e.mirror else ''}R{e.angle:g}"
                                  f" {e.body[2] - e.body[0]:.1f}x{e.body[3] - e.body[1]:.1f}mm" + (" LOCKED" if e.locked else "")
                                  for e in list(b.elements.values())[:120])}


def tool_layout_rules_from_datasheet(p: Project, pdf_path, ic_ref="", hint=""):
    from .lbr_builder import extract_layout_rules
    if p.client is None:
        return {"error": "no Claude API client configured"}
    pdf = Path(pdf_path)
    if not pdf.is_absolute():
        pdf = p.folder / "datasheets" / pdf
    if not pdf.exists():
        return {"error": f"file not found: {pdf}"}
    out = extract_layout_rules(p.client, p.settings.datasheet_model, pdf, hint or ic_ref, p.costs)
    out["ic_ref"] = ic_ref
    out["next"] = ("Map each role to the design part (e.g. input capacitor -> C3), then call place_parts with rules "
                   "like {ref:'C3', near:'%s', pin:'VIN', max_dist:2}." % (ic_ref or "U1"))
    return out


def tool_place_parts(p: Project, rules=None, side="top", keep=None, move_only=None, clearance=1.0,
                     edge_clearance=None, brd=None):
    from . import eagle_link, placement
    b, src = load_board(p, brd)
    if edge_clearance is None:          # room for the routing edge rule plus a trace
        rl = project_rules(p)
        edge_clearance = max(1.0, rl.edge_clearance + rl.trace)
    live = src.startswith("live")

    def pin_to_pads(ref, pin):
        dev, _ = p.design.device_for(p.index, ref)
        if dev is None:
            return []
        return [pd for x in dev.pins if x.name.upper() == str(pin).upper() for pd in x.pads]

    cfg = {"rules": rules or [], "side": side, "keep": keep or [], "move_only": move_only or [],
           "clearance": clearance, "edge_clearance": edge_clearance}
    unknown = sorted({str(r.get("ref", "")).upper() for r in cfg["rules"]} - {e.upper() for e in b.elements})
    res = placement.place(b, cfg, pin_to_pads)
    out = {"board": src, **res.summary()}
    if unknown:
        out["unknown_refs"] = unknown
    if not any(x.moved for x in res.placed.values()):
        out["result"] = "nothing to move"
        return out
    choice = p.ask_placement(b, res, live) if p.ask_placement else ("eagle" if live else None)
    if choice == "eagle":
        eagle_link.queue(placement.eagle_script(b, res), "board", "placement", p.bridge_dir)
        out["result"] = "queued - the user presses the Eagle link key in the board editor to apply it (undo in Eagle)"
    elif choice == "brd":
        backup, warns = placement.write_brd(Path(b.path), b, res)
        out["result"] = f"written to {Path(b.path).name} (backup {backup.name}); reopen it in Eagle"
        if warns:
            out["brd_warnings"] = warns
    else:
        out["result"] = "the user did not apply this placement - ask what to change"
    return out


def project_rules(p: Project):
    from . import rules as R
    return R.load_project_rules(p.state(), p.settings.rules_preset)


def tool_design_rules(p: Project, preset=None, dru_path=None, overrides=None):
    from dataclasses import asdict
    from . import rules as R
    r = project_rules(p)
    changed = False
    if preset:
        if preset not in R.PRESETS:
            return {"error": f"unknown preset {preset} - one of {sorted(R.PRESETS)}"}
        r, changed = R.PRESETS[preset], True
    if dru_path:
        f = Path(dru_path)
        f = f if f.is_absolute() else p.folder / f
        try:
            r, changed = R.read_dru(f), True
        except (OSError, ValueError) as exc:
            return {"error": str(exc)}
    if overrides:
        d = asdict(r)
        bad = [k for k in overrides if k not in d]
        if bad:
            return {"error": f"unknown rule fields {bad} - fields: {sorted(d)}"}
        d.update(overrides)
        d["source"] = "edited"
        r, changed = R.DesignRules(**d), True
    if changed:
        p.save_state(design_rules=asdict(r))
        p.write_owned(p.folder / "design_rules.dru", R.dru_text(r), "design rules file")
    return {"rules": asdict(r), "problems": r.check() or "none", "presets": sorted(R.PRESETS),
            "dru_file": str(p.folder / "design_rules.dru")}


def tool_route_board(p: Project, net_classes=None, order=None, skip_nets=None, ground_pour=None,
                     pour_layers=None, grid=None, time_limit=90, brd=None):
    from . import eagle_link, router, rules as R
    rl = project_rules(p)
    probs = rl.check()
    if probs:
        return {"error": "design rules break the fab limits: " + "; ".join(probs)}
    b, src = load_board(p, brd)
    widths, clrs, report = {}, {}, []
    for c in net_classes or []:
        w = float(c.get("width") or 0)
        if c.get("current_a"):
            w = max(w, R.trace_width_for_current(float(c["current_a"]), rl.copper_oz))
        w = max(w, rl.min_trace) if w else rl.trace
        for net in c.get("nets", []):
            widths[net] = w
            if c.get("clearance"):
                clrs[net] = max(float(c["clearance"]), rl.min_clearance)
        report.append(f"{c.get('name', 'class')}: {w:g} mm" + (f" ({c['current_a']} A)" if c.get("current_a") else ""))
    missing = sorted(set(widths) - set(b.signals))
    skip = list(skip_nets or [])
    res = router.route(b, rl, widths, clrs, order, skip, grid, float(time_limit))
    if ground_pour:
        x0, y0, x1, y1 = b.outline
        e = rl.edge_clearance
        res.pour = {"net": ground_pour, "layers": [16 if l == "bottom" else 1 for l in (pour_layers or ["bottom"])],
                    "box": (x0 + e, y0 + e, x1 - e, y1 - e), "isolate": max(rl.clearance, 0.3)}
    out = {"board": src, "rules": rl.name, "classes": report or f"all nets {rl.trace:g} mm", **res.summary()}
    if missing:
        out["unknown_nets"] = missing
    if not res.segs and not res.pour:
        out["result"] = "nothing routed"
        return out
    choice = p.ask_route(b, res, src.startswith("live")) if p.ask_route else None
    if choice == "eagle":
        eagle_link.queue(router.eagle_script(res, rl), "board", "routing", p.bridge_dir)
        p.write_owned(p.folder / "design_rules.dru", R.dru_text(rl), "design rules file")
        out["result"] = ("queued - the user presses the Eagle link key in the board editor. For Eagle's own DRC: "
                         f"DRC -> Load {p.folder / 'design_rules.dru'}")
    elif choice == "brd":
        backup = router.write_brd(Path(b.path), res, rl)
        out["result"] = f"written to {Path(b.path).name} with the design rules (backup {backup.name}); reopen it in Eagle"
    else:
        out["result"] = "the user did not apply this routing - ask what to change"
    return out


def tool_set_doc(p: Project, title=None, subtitle=None, summary=None, citations=None, sections=None):
    d = p.design.doc
    if title is not None:
        d["title"] = str(title)
    if subtitle is not None:
        d["subtitle"] = str(subtitle)
    if summary is not None:
        d["summary"] = [str(x) for x in summary]
    if citations:
        d.setdefault("citations", {}).update({str(k): str(v) for k, v in citations.items()})
    if sections:
        d.setdefault("sections", {}).update({str(k).upper(): str(v) for k, v in sections.items()})
    p.changed()
    return {"ok": True, "md": p.md_status}


def tool_build_coil(p: Project, params, save_to=None):
    try:
        cp = coils.params_from(params)
        spec, stats = coils.build_spec(cp)
        out = p.lbr_out(safe_name(cp.name), save_to)
        out = _guard_lbr(p, out, safe_name(cp.name)) if out else None
        if out is None:
            return {"error": "cancelled by the user - no library was written"}
        path, warnings = build_lbr(spec, out_path=out, note="PCB coil - generated by Eagle AI Design Studio")
    except (SpecError, TypeError, ValueError) as exc:
        return {"error": f"coil rejected: {exc}"}
    res = _register(p, path, warnings, spec, unverified=False)
    res["coil"] = stats
    return res


def tool_build_lbr_from_datasheet(p: Project, pdf_path, hint="", save_to=None):
    if p.client is None:
        return {"error": "no Claude API client configured"}
    pdf = Path(pdf_path)
    if not pdf.is_absolute():
        pdf = p.folder / "datasheets" / pdf
    if not pdf.exists():
        return {"error": f"file not found: {pdf}"}
    spec = extract_spec_from_datasheet(p.client, p.settings.datasheet_model, pdf, hint, p.costs,
                                       pages_only=p.settings.datasheet_pages)
    retried = False
    while True:
        unsure = " ".join(spec.get("uncertain", [])).lower()
        missing = spec.get("_pages_only") and any(k in unsure for k in ("land pattern", "footprint", "pin table",
                                                                       "not among", "not shown", "not included"))
        try:
            if missing:
                raise SpecError("the selected pages lacked the pinout or land pattern")
            out = p.lbr_out(safe_name(spec.get("name", "")), save_to)
            out = _guard_lbr(p, out, safe_name(spec.get("name", ""))) if out else None
            if out is None:
                return {"error": "cancelled by the user - no library was written", "spec": spec}
            path, warnings = build_lbr(spec, out_path=out)
            break
        except SpecError as exc:
            if spec.get("_pages_only") and not retried:
                # page selection was not enough: one retry with the whole datasheet
                retried = True
                spec = extract_spec_from_datasheet(p.client, p.settings.datasheet_model, pdf, hint, p.costs,
                                                   pages_only=False)
                continue
            return {"error": f"datasheet spec rejected: {exc}", "spec": spec}
    spec.pop("_pages_only", None)
    res = _register(p, path, warnings, spec)
    res["uncertain"] = spec.get("uncertain", [])
    if retried:
        res["note"] = "needed the full datasheet (page selection missed something)"
    return res


def tool_generate_sch(p: Project, filename=None):
    name = re.sub(r"[^\w.\-]", "_", filename or p.design.name or "design")
    if not name.lower().endswith(".sch"):
        name += ".sch"
    probs = [x for x in p.design.validate(p.index) if x.level == "error"]
    if probs:
        return {"error": "fix these first", "problems": [str(x) for x in probs]}
    target = p.folder / name
    warnings = []
    if target.exists():
        known = p.state().get("sch_mtimes", {}).get(target.name)
        if known is None:
            warnings.append("The studio did not write this file - it may be your own schematic.")
        elif abs(target.stat().st_mtime - known) > 1:
            warnings.append("It was changed in Eagle after the studio wrote it - overwriting loses those edits.")
        brd = target.with_suffix(".brd")
        if brd.exists():
            warnings.append(f"{brd.name} belongs to it: a regenerated schematic breaks Eagle's board/schematic link "
                            "(forward/back annotation). Saving as a new file is safer.")
    path = p.guard.target(target, "schematic", warnings)
    if path is None:
        return {"error": f"cancelled - {target.name} was left as it is"}
    res = write_sch(p.design, p.index, path)
    p._remember_sch(path)
    if path != target:
        res["note"] = f"{target.name} was left alone; wrote {path.name}"
    elif warnings:
        res["note"] = f"overwrote {target.name} (backup in .eagleai/backups)"
    return res


def tool_import_apply_nets(p: Project):
    """Second half of a Markdown import: connect the parsed nets once the parts exist."""
    imp = p.pending_import
    if not imp:
        return {"error": "no Markdown import in progress"}
    missing = sorted({m.split(".")[0] for ms in imp["nets"].values() for m in ms} - set(p.design.parts))
    if missing:
        return {"error": f"add these parts first (use the refs exactly): {missing}"}
    errors, added = [], 0
    for net, members in imp["nets"].items():
        a, e = p.design.connect(p.index, net, members)
        added += len(a)
        for x in e:
            ref_pin = x.split(":")[0]
            hint = imp["pin_names"].get(ref_pin)
            errors.append(x + (f"  [markdown called this pin '{hint}']" if hint else ""))
    p.changed()
    if not errors:
        p.pending_import = None
    return {"connected_pins": added, "errors": errors,
            "next": "fix each error with connect/disconnect (the markdown used datasheet pin numbers; the library "
                    "symbol may number or name them differently), then run_checks" if errors else "run_checks"}


def price_bom(p: Project, assign: bool = False, progress=None) -> Dict:
    """Cheapest in-stock JLC option for every BOM line, with a total.
    Network errors are reported per line, never raised."""
    qty_build = p.settings.build_qty
    rows, total, missing, from_memory = [], 0.0, 0, 0
    groups = [g for g in p.design.bom() if not g["dnp"]]
    price_cache = p.state().get("prices", {})
    for i, g in enumerate(groups):
        if progress:
            progress(i + 1, len(groups), g["refs"])
        ref0 = g["refs"].split(",")[0].strip()
        qty = g["qty"] * qty_build
        row = {"refs": g["refs"], "qty": g["qty"], "value": g["value"], "mpn": g["mpn"], "lcsc": g["lcsc"],
               "unit": None, "line": None, "stock": None, "basic": None, "error": ""}
        _how, ranked = "no in-stock match", []
        try:
            if g["lcsc"]:
                c0 = p.jlc.lookup(g["lcsc"])
                ranked = [c0] if c0 else []
                if c0 is not None:
                    p.remember_lcsc(g["refs"].split(","), c0)      # confirmed at JLC -> reuse next project
            else:
                mc, rec = p.recall_lcsc(ref0, qty)
                if mc is not None:
                    ranked, _how = [mc], "remembered"
                    from_memory += 1
            if not g["lcsc"] and not ranked:
                dev, _ = p.design.device_for(p.index, ref0)
                prefix = re.sub(r"[^A-Z]", "", (dev.prefix if dev else "").upper()) or re.sub(r"[^A-Z]", "", ref0.upper())
                ranked, _how = jlc.best_for_part(p.jlc, prefix, g["value"], g["mpn"], dev.package if dev else g.get("device", ""),
                                                 qty, p.settings.extended_fee,
                                                 names=(g.get("deviceset", ""), g.get("device", "")), note=g.get("note", ""),
                                                 min_volts=p.min_volts(g["refs"].split(",")))
            if ranked:
                c = ranked[0]
                row.update(lcsc=c.lcsc, unit=c.unit_price(qty), stock=c.stock, basic=c.basic or c.preferred,
                           line=(c.line_cost(qty, p.settings.extended_fee) or 0) / qty * g["qty"] if qty else None)
                for r in g["refs"].split(","):      # per-ref price cache; shown only while the LCSC still matches
                    price_cache[r.strip()] = {"lcsc": c.lcsc, "unit": row["unit"], "stock": c.stock,
                                              "basic": row["basic"], "ts": time.time()}
                if assign and not g["lcsc"]:
                    for r in g["refs"].split(","):
                        p.design.parts[r.strip()].lcsc = c.lcsc
            else:
                row["error"] = ("not listed at JLC" if g["lcsc"] else _how)
        except Exception as exc:
            row["error"] = str(exc)[:80]
        if row["line"] is None:
            missing += 1
        else:
            total += row["line"]
        rows.append(row)
    p.save_state(prices=price_cache)
    if assign:
        p.changed()
    return {"rows": rows, "total_per_board": round(total, 4), "unpriced_lines": missing, "build_qty": qty_build,
            "from_memory": from_memory}
