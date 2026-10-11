"""Finding out why the app died inside a Windows file dialog.

1. Breadcrumbs: every file/folder dialog writes 'OPEN' (what, start folder, options) before it opens and
   'CLOSE' after it returns, flushed to disk at once. If the app dies inside the dialog, the log ends with an
   OPEN that never closed - the next start sees that and says so.
2. Windows' own crash record: when a program crashes, Windows logs an 'Application Error' event (Event Viewer ->
   Windows Logs -> Application, event 1000) naming the FAULTING MODULE - the DLL that crashed. Inside a file
   dialog that is almost always an Explorer add-on (cloud drive, archiver, git/svn overlay, PDF previewer...).
   read_windows_crashes() pulls those events with PowerShell (read-only) and names the DLL and its owner.
3. faulthandler.log (run.py) has the Python side: which studio function was running.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "dialog_debug.log"
_lock = threading.Lock()


def _write(line: str):
    with _lock:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())        # must be on disk before a native crash can happen
        except OSError:
            pass


def session_start(native: bool):
    try:
        if LOG.exists() and LOG.stat().st_size > 300_000:
            LOG.write_text(LOG.read_text(encoding="utf-8", errors="replace")[-100_000:], encoding="utf-8")
    except OSError:
        pass
    _write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} SESSION pid={os.getpid()} dialog_mode={native} "
           f"python={sys.version.split()[0]}")


def opened(kind: str, caption: str, start: str, native: bool) -> float:
    t = time.time()
    exists = bool(start) and Path(start).exists()
    _write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} OPEN {kind} mode={native} start={start!r} "
           f"start_exists={exists} caption={caption!r}")
    return t


def closed(kind: str, t0: float, result: str):
    _write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} CLOSE {kind} after {time.time() - t0:.1f}s result={result!r}")


def session_end():
    _write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} EXIT")


def last_session_died_in_dialog() -> Optional[str]:
    """The OPEN line of the previous session if it never closed (the app died inside that dialog)."""
    try:
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    starts = [i for i, l in enumerate(lines) if " SESSION " in l]
    if len(starts) < 2:
        return None
    prev = lines[starts[-2]:starts[-1]]          # the session before this one
    opens = [l for l in prev if " OPEN " in l]
    closes = [l for l in prev if " CLOSE " in l]
    if opens and len(opens) > len(closes) and not any(" EXIT" in l for l in prev):
        return opens[-1]
    return None


def read_windows_crashes(timeout: float = 20.0) -> Dict:
    """Recent crashes (event 1000, with the faulting module) and freezes (event 1002, 'stopped interacting with
    Windows and was closed') of python/pythonw from the Windows event log (wevtutil - built in, no PowerShell).
    Other programs' freezes are counted too: if their Open dialogs freeze as well, an Explorer add-on is the cause."""
    if os.name != "nt":
        return {"error": "only available on Windows", "events": []}
    cmd = ["wevtutil", "qe", "Application", "/q:*[System[(EventID=1000 or EventID=1002)]]", "/c:60", "/rd:true",
           "/f:text"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"could not read the event log: {exc}", "events": []}
    events, other_hangs = parse_events(r.stdout or "")
    if not events:
        return {"events": [], "other_hangs": other_hangs,
                "note": "Windows has no crash or freeze record for Python. The studio did not crash in "
                        "Windows' eyes - it was closed (or closed itself) normally."}
    owners = module_owners([e["module_path"] for e in events if e["module_path"]])
    for e in events:
        e["owner"] = owners.get(e["module_path"], "") or KNOWN.get(e["module"].lower(), "")
    return {"events": events, "other_hangs": other_hangs}


def parse_events(text: str, limit: int = 8):
    """wevtutil text output -> (python crash/hang events, names of other programs that froze)."""
    events, other_hangs = [], []
    for blk in re.split(r"\n(?=Event\[\d+\])", text):
        when = (re.search(r"Date:\s*([^\r\n]+)", blk) or [None, ""])[1].strip()
        hang = re.search(r"The program (\S+) version \S+ stopped interacting with Windows", blk)
        if hang:
            if "python" in hang.group(1).lower():
                events.append({"time": when, "kind": "hang", "app": hang.group(1), "module": "",
                               "module_path": "", "exception": "", "offset": "", "owner": ""})
            elif len(other_hangs) < 8:
                other_hangs.append(f"{when}  {hang.group(1)}")
        elif "python" in blk.lower() and "Faulting application name" in blk:
            events.append(dict(parse_event(blk, when), kind="crash"))
        if len(events) >= limit:
            break
    return events, other_hangs


def parse_event(msg: str, when: str = "") -> Dict:
    """Fields of a Windows 'Application Error' (event 1000) message."""
    def get(label):
        m = re.search(label + r":[ \t]*([^\r\n]+)", msg)
        return m.group(1).strip() if m else ""
    return {"time": when, "app": get("Faulting application name").split(",")[0],
            "module": get("Faulting module name").split(",")[0], "module_path": get("Faulting module path"),
            "exception": get("Exception code"), "offset": get("Fault offset"), "owner": ""}


KNOWN = {
    "shell32.dll": "Windows shell itself - usually triggered by an add-on it loaded; check the other entries",
    "twinui.dll": "Windows shell UI", "explorerframe.dll": "Windows Explorer frame (the dialog itself)",
    "windows.storage.dll": "Windows storage layer - often a bad network/cloud drive path",
    "qt6core.dll": "Qt itself", "qt6gui.dll": "Qt itself", "qt6widgets.dll": "Qt itself",
    "pyside6.abi3.dll": "PySide6 (Qt for Python) - typically while the studio was closing with background jobs "
                        "still running",
    "python3.dll": "Python", "ucrtbase.dll": "C runtime - the real cause is usually the module that called it",
    "ntdll.dll": "Windows core - memory got corrupted by something else loaded into the app",
}


def module_owners(paths: List[str], timeout: float = 12.0) -> Dict[str, str]:
    """Maker of each DLL (from its version info), in ONE PowerShell call with a time limit."""
    paths = sorted({p for p in paths if p})
    if not paths or os.name != "nt":
        return {}
    lst = ",".join("'" + p.replace("'", "''") + "'" for p in paths)
    cmd = (f"foreach($p in @({lst})){{ $v=(Get-Item -LiteralPath $p -ErrorAction SilentlyContinue).VersionInfo; "
           f"Write-Output ($p + '|' + $v.CompanyName + ' - ' + $v.FileDescription) }}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                           text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in (r.stdout or "").splitlines():
        if "|" in line:
            p, info = line.split("|", 1)
            out[p.strip()] = info.strip(" -")
    return out


def tail(path: Path, n: int = 60) -> str:
    try:
        return "\n".join(Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return "(no file)"


def local_report(native) -> str:
    """The studio's own logs - instant."""
    try:
        from . import __version__
        import PySide6
        from PySide6.QtCore import qVersion
        ver = f"studio {__version__}, Python {sys.version.split()[0]}, PySide6 {PySide6.__version__}, Qt {qVersion()}"
    except Exception as exc:
        ver = f"versions unknown ({exc})"
    lines = [f"Eagle AI Design Studio crash report - file dialogs: {native}", ver, ""]
    lines += ["== File dialog trace (dialog_debug.log) ==", tail(LOG, 40)]
    lines += ["", "== Python side (faulthandler.log) ==", tail(ROOT / "faulthandler.log", 80)]
    lines += ["", "== Python errors (crash.log) ==", tail(ROOT / "crash.log", 40)]
    return "\n".join(lines)


def windows_report() -> str:
    w = read_windows_crashes()
    lines = ["== Windows crash / freeze records (python / pythonw) =="]
    if w.get("error"):
        lines.append(w["error"])
    if w.get("note"):
        lines.append(w["note"])
    for e in w.get("events", []):
        if e.get("kind") == "hang":
            lines.append(f"{e['time']}  {e['app']}  FROZE (stopped responding and was closed) - inside a file "
                         f"dialog this is an Explorer add-on that hung; Windows doesn't record which one")
            continue
        lines.append(f"{e['time']}  {e['app']}  crashed in {e['module']}  (exception {e['exception']}, "
                     f"offset {e['offset']})")
        if e["module_path"]:
            lines.append(f"    module: {e['module_path']}")
        if e["owner"]:
            lines.append(f"    made by: {e['owner']}")
    if w.get("other_hangs"):
        lines += ["", "Other programs that froze recently (if their Open dialogs froze too, the cause is an "
                      "Explorer add-on - find it with NirSoft ShellExView by disabling non-Microsoft entries):"]
        lines += ["    " + h for h in w["other_hangs"]]
    return "\n".join(lines)


def report(native) -> str:
    return local_report(native) + "\n\n" + windows_report()
