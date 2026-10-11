"""Your JLC parts, remembered across projects (Documents/EagleAI/jlc_memory.json).

Every LCSC number that is confirmed at JLC (priced, assigned by the AI after a check, or applied from the AI
web search) is stored under up to three keys:
  DEV:<library/deviceset/device>|<value>   same library part with the same value  (strongest)
  MPN:<part number>                        same manufacturer part number
  R:/C:/L:<value>|<size>                   same passive value in the same size (capacitors keep their voltage)
On a new project, Fill missing / jlc_find try these first; the number is looked up at JLC again (stock and price
change), and only if it's out of stock does the normal search run. No AI tokens are spent for remembered parts.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import jlc


def _canon_value(prefix: str, value: str) -> str:
    v = (value or "").replace("µ", "u").replace("μ", "u").strip()
    first = re.split(r"\s", v)[0] if v else ""
    num = jlc.parse_value(first)
    if num is None:
        return jlc._norm(v)
    return f"{num:.4g}"


def keys_for(prefix: str, value: str, mpn: str, package: str, library: str = "", deviceset: str = "",
             device: str = "") -> List[str]:
    keys = []
    if deviceset:
        keys.append(f"DEV:{library}/{deviceset}/{device}|{jlc._norm(value)}".upper())
    for name in (mpn, value, deviceset):
        if jlc._looks_like_mpn(name):
            keys.append("MPN:" + jlc._norm(jlc.mpn_variants(name)[-1]))
            break
    size = jlc.footprint_size(package)
    if prefix in ("R", "C", "L") and size and jlc.parse_value(re.split(r"\s", (value or "").replace("µ", "u"))[0] if value else ""):
        keys.append(f"{prefix}:{_canon_value(prefix, value)}|{size}")
    return list(dict.fromkeys(keys))


class JlcMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self.data: Dict[str, Dict] = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def remember(self, keys: List[str], cand: "jlc.Candidate", project: str = ""):
        if not keys or cand is None:
            return
        rec = {"lcsc": cand.lcsc, "mfr": cand.mfr, "package": cand.package, "description": cand.description[:120],
               "volts": jlc.rated_volts(cand.description), "basic": bool(cand.basic or cand.preferred),
               "ts": time.time(), "project": project}
        with self._lock:
            changed = False
            for k in keys:
                old = self.data.get(k)
                if old and old.get("lcsc") == cand.lcsc:
                    old.update(ts=rec["ts"], uses=old.get("uses", 1) + (1 if old.get("project") != project else 0),
                               project=project or old.get("project", ""))
                else:
                    self.data[k] = dict(rec, uses=1)
                changed = True
            if changed:
                self._save()

    def recall(self, keys: List[str], need_volts: Optional[float] = None) -> Optional[Dict]:
        for k in keys:
            rec = self.data.get(k)
            if not rec:
                continue
            if k.startswith("C:") and need_volts and (rec.get("volts") or 0) < need_volts:
                continue
            return dict(rec, key=k)
        return None

    def forget(self, lcsc: str) -> int:
        with self._lock:
            gone = [k for k, v in self.data.items() if v.get("lcsc") == lcsc.upper()]
            for k in gone:
                del self.data[k]
            if gone:
                self._save()
        return len(gone)

    def __len__(self):
        return len({v.get("lcsc") for v in self.data.values()})
