"""Drive the installed Eagle 7.4 from the app.

convert_binary_lbr: Eagle 5-and-older libraries are binary; Eagle 6+/7.4 reads them
and writes XML. We copy each library into a converted-libs folder (your originals
are never touched), start Eagle on the copy with  -C "WRITE; QUIT;"  and check the
result really is XML.

UNVERIFIED on a real install: the exact behaviour of `eagle.exe -C` with an old
binary file (it may show an "update file" dialog that needs a click). Use
"Test on one file" first; if Eagle waits on a dialog, click OK/Yes and it continues.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Dict, List, Optional


def find_eagle() -> Optional[str]:
    cands: List[Path] = []
    for base in ("C:\\", os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        b = Path(base)
        if b.is_dir():
            cands += sorted(b.glob("EAGLE*/bin/eagle.exe"), reverse=True)
    for c in cands:
        if "7." in str(c):           # prefer 7.x over others
            return str(c)
    return str(cands[0]) if cands else None


def default_converted_dir() -> Path:
    return Path.home() / "Documents" / "EagleAI" / "converted_libs"


def is_xml_lbr(path: Path) -> bool:
    try:
        root = ET.parse(path).getroot()
        return root.tag == "eagle" and root.find(".//library") is not None
    except Exception:
        return False


def convert_binary_lbr(eagle_exe: str, src: Path, out_dir: Path, timeout: float = 90.0, guard=None) -> Dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / Path(src).name
    if guard is not None and dst.exists():
        chosen = guard.target(dst, f"converted library {dst.name}")
        if chosen is None:
            return {"src": str(src), "ok": False, "msg": "skipped - the existing converted copy was kept"}
        dst = chosen
    shutil.copy2(src, dst)
    cmd = [eagle_exe, "-C", "WRITE; QUIT;", str(dst)]
    try:
        r = subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)
        note = (r.stderr or r.stdout or "").strip()[:200]
    except subprocess.TimeoutExpired:
        note = f"Eagle did not finish in {timeout:.0f}s (waiting on a dialog?)"
    except OSError as exc:
        dst.unlink(missing_ok=True)
        return {"src": str(src), "ok": False, "msg": f"could not start Eagle: {exc}"}
    if is_xml_lbr(dst):
        return {"src": str(src), "ok": True, "dst": str(dst), "msg": "converted"}
    dst.unlink(missing_ok=True)
    return {"src": str(src), "ok": False, "msg": note or "Eagle ran but the file is still not XML"}


def convert_many(eagle_exe: str, paths: List[Path], out_dir: Path,
                 progress: Optional[Callable[[int, int, str], None]] = None, guard=None) -> List[Dict]:
    res = []
    for i, p in enumerate(paths):
        if progress:
            progress(i + 1, len(paths), Path(p).name)
        res.append(convert_binary_lbr(eagle_exe, Path(p), out_dir, guard=guard))
    return res
