"""Eagle link: one key in Eagle 7.4 syncs with the studio.

Eagle 7 has no API and ULPs can't run on a timer, so the link is a ULP you bind to a key (F12 by default):
  1. It writes what is open in Eagle (board: parts, positions, pads, signals, outline; schematic: parts) to
     <bridge>/state.txt - the studio reloads it at once.
  2. If the studio has queued commands for this editor (<bridge>/pending.scr, numbered in pending.id), it runs
     them with SCRIPT (each batch once - done.id remembers the last one). The script ends by running the ULP
     again, so the studio sees the result. Everything it does can be undone in Eagle (Ctrl+Z / UNDO).

Nothing is changed in your eagle.rc or eagle.scr; the ASSIGN command is shown to paste once.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from .eagle_cli import default_converted_dir


def bridge_dir() -> Path:
    d = default_converted_dir().parent / "eagle_link"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _e(p: Path) -> str:
    return str(p).replace("\\", "/")


ULP = r'''#usage "<b>Eagle AI Design Studio link</b><p>Bind it to a key once:<br>%(assign)s<p>Each press: sends the open board/schematic to the studio and runs commands the studio queued."

string BRIDGE = "%(bridge)s";

string readfirst(string f) {
  string a[];
  string lines[];
  if (fileglob(a, f) == 0) return "";
  int n = fileread(lines, f);
  if (n < 1) return "";
  return lines[0];
}

void dump_board() {
  board(B) {
    output(BRIDGE + "/state.tmp", "wt") {
      printf("BOARD\t%%s\n", B.name);
      B.wires(W) {
        if (W.layer == 20) printf("O\t%%.4f\t%%.4f\t%%.4f\t%%.4f\n", u2mm(W.x1), u2mm(W.y1), u2mm(W.x2), u2mm(W.y2));
      }
      B.elements(E) {
        printf("E\t%%s\t%%s\t%%s\t%%s\t%%.4f\t%%.4f\t%%.2f\t%%d\n", E.name, E.value, E.package.name,
               E.package.library, u2mm(E.x), u2mm(E.y), E.angle, E.mirror);
        E.package.wires(W) {
          if (W.layer == 20) printf("O\t%%.4f\t%%.4f\t%%.4f\t%%.4f\n", u2mm(W.x1), u2mm(W.y1), u2mm(W.x2), u2mm(W.y2));
          if (W.layer == 1 || W.layer == 16) printf("PW\t%%s\t%%d\t%%.4f\t%%.4f\t%%.4f\t%%.4f\t%%.4f\n", E.name, W.layer,
                                                   u2mm(W.x1), u2mm(W.y1), u2mm(W.x2), u2mm(W.y2), u2mm(W.width));
        }
        E.package.contacts(C) {
          real dx = 0, dy = 0;
          string kind = "S";
          if (C.smd) { dx = u2mm(C.smd.dx); dy = u2mm(C.smd.dy); }
          else if (C.pad) { dx = u2mm(C.pad.diameter[LAYER_TOP]); dy = dx; kind = "T"; }
          printf("P\t%%s\t%%s\t%%.4f\t%%.4f\t%%.4f\t%%.4f\t%%s\n", E.name, C.name, u2mm(C.x), u2mm(C.y), dx, dy, kind);
        }
      }
      B.signals(S) {
        S.contactrefs(R) { printf("S\t%%s\t%%s\t%%s\n", S.name, R.element.name, R.contact.name); }
        S.wires(W) {
          if (W.layer == 1 || W.layer == 16) printf("W\t%%s\t%%d\t%%.4f\t%%.4f\t%%.4f\t%%.4f\t%%.4f\n", S.name, W.layer,
                                                   u2mm(W.x1), u2mm(W.y1), u2mm(W.x2), u2mm(W.y2), u2mm(W.width));
        }
        S.vias(V) { printf("V\t%%s\t%%.4f\t%%.4f\t%%.4f\t%%.4f\n", S.name, u2mm(V.x), u2mm(V.y),
                           u2mm(V.diameter[LAYER_TOP]), u2mm(V.drill)); }
      }
      printf("END\n");
    }
  }
}

void dump_schematic() {
  schematic(SCH) {
    output(BRIDGE + "/state.tmp", "wt") {
      printf("SCHEMATIC\t%%s\n", SCH.name);
      SCH.parts(P) { printf("PART\t%%s\t%%s\t%%s\t%%s\n", P.name, P.value, P.device.library, P.device.name); }
      printf("END\n");
    }
  }
}

string ctx = "";
if (board) { ctx = "board"; dump_board(); }
else if (schematic) { ctx = "sch"; dump_schematic(); }
else { dlgMessageBox("Open a board or schematic first."); exit(0); }

// tell the studio a fresh state is ready (it watches stamp.txt)
output(BRIDGE + "/state.ctx", "wt") { printf("%%s\n", ctx); }
string tmp[];
int nt = fileread(tmp, BRIDGE + "/state.tmp");
output(BRIDGE + "/state.txt", "wt") { for (int i = 0; i < nt; i++) printf("%%s\n", tmp[i]); }
output(BRIDGE + "/stamp.txt", "wt") { printf("%%d\n", time()); }

string pid = readfirst(BRIDGE + "/pending.id");
string pctx = readfirst(BRIDGE + "/pending.ctx");
string did = readfirst(BRIDGE + "/done.id");
if (pid != "" && pid != did && pctx == ctx) {
  output(BRIDGE + "/done.id", "wt") { printf("%%s\n", pid); }
  exit("SCRIPT '" + BRIDGE + "/pending.scr';");
}
status("Eagle AI Design Studio: synced (" + ctx + ")");
exit(0);
'''


def install(folder: Optional[Path] = None) -> Path:
    d = Path(folder) if folder else bridge_dir()
    d.mkdir(parents=True, exist_ok=True)
    ulp = d / "studio.ulp"
    ulp.write_text(ULP % {"bridge": _e(d), "assign": assign_command(folder=d).replace('"', "&quot;")}, encoding="utf-8")
    return ulp


def assign_command(key: str = "F12", folder: Optional[Path] = None) -> str:
    d = Path(folder) if folder else bridge_dir()
    # Eagle strings use single quotes; a quote inside a quoted string is doubled
    return f"ASSIGN {key} 'RUN ''{_e(d / 'studio.ulp')}'';';"


def queue(script: str, ctx: str = "board", label: str = "", folder: Optional[Path] = None) -> str:
    """Queue commands for the next key press in Eagle. Returns the batch id."""
    d = Path(folder) if folder else bridge_dir()
    bid = f"{int(time.time() * 1000)}"
    body = (f"# Eagle AI Design Studio - {label}\n" if label else "") + script.rstrip() + "\n"
    body += f"RUN '{_e(d / 'studio.ulp')}';\n"         # re-sync so the studio sees the result
    (d / "pending.scr").write_text(body, encoding="utf-8")
    (d / "pending.ctx").write_text(ctx + "\n", encoding="utf-8")
    (d / "pending.id").write_text(bid + "\n", encoding="utf-8")     # written last: the ULP checks this
    return bid


def pending(folder: Optional[Path] = None) -> Optional[str]:
    """Id of a queued batch Eagle hasn't run yet, else None."""
    d = Path(folder) if folder else bridge_dir()
    try:
        pid = (d / "pending.id").read_text().strip()
    except OSError:
        return None
    try:
        did = (d / "done.id").read_text().strip()
    except OSError:
        did = ""
    return pid if pid and pid != did else None


def cancel(folder: Optional[Path] = None):
    d = Path(folder) if folder else bridge_dir()
    try:
        (d / "done.id").write_text((d / "pending.id").read_text())
    except OSError:
        pass


def state(folder: Optional[Path] = None):
    """(kind 'board'|'sch'|None, path to state.txt, age in seconds)."""
    d = Path(folder) if folder else bridge_dir()
    st = d / "state.txt"
    if not st.exists():
        return None, st, None
    first = st.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
    kind = "board" if first.startswith("BOARD") else "sch" if first.startswith("SCHEMATIC") else None
    return kind, st, time.time() - os.path.getmtime(st)
