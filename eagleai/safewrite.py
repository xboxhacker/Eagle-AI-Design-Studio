"""Overwrite protection for every file the studio writes into your folders.

Before an existing file is replaced, the guard asks (through the UI):
    Overwrite      - the old file is first copied to <project>/.eagleai/backups/<name>.<date-time>
    Save as new    - writes <name>_v2, _v3 ... next to it instead
    Cancel         - nothing is written
"Don't ask again for this file" remembers Overwrite until the studio is closed (handy while you regenerate the
same .sch repeatedly). Without a UI (tests, scripts) the guard never destroys anything: it picks a new name.

Files the studio owns and rewrites on purpose are not routed through here: design.json, state/caches, the
Schematic MD (it has its own outside-edit detection) and the Eagle-link files.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ask(path, what, extra_warnings) -> ("overwrite", remember: bool) | ("new", None) | ("cancel", None)
AskFn = Callable[[Path, str, List[str]], Tuple[str, Optional[bool]]]


def versioned(path: Path) -> Path:
    path = Path(path)
    stem = path.stem
    import re
    m = re.match(r"^(.*)_v(\d+)$", stem)
    base, n = (m.group(1), int(m.group(2)) + 1) if m else (stem, 2)
    while True:
        cand = path.with_name(f"{base}_v{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


class OverwriteGuard:
    def __init__(self, project_folder: Path):
        self.folder = Path(project_folder)
        self.backup_dir = self.folder / ".eagleai" / "backups"
        self.ask: Optional[AskFn] = None
        self.session_ok: set = set()
        self.log: List[str] = []

    def backup(self, path: Path) -> Optional[Path]:
        path = Path(path)
        if not path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dst = self.backup_dir / f"{path.name}.{time.strftime('%Y%m%d_%H%M%S')}"
        if path.is_dir():
            shutil.copytree(path, dst)
        else:
            shutil.copy2(path, dst)
        self.log.append(f"backed up {path.name} -> {dst.name}")
        return dst

    def target(self, path: Path, what: str = "file", warnings: Optional[List[str]] = None) -> Optional[Path]:
        """Where to write `path`. None = the user cancelled. Backs up before an approved overwrite."""
        path = Path(path)
        if not path.exists():
            return path
        key = str(path.resolve()).lower()
        if key in self.session_ok:
            self.backup(path)
            return path
        if self.ask is None:
            return versioned(path)
        choice, remember = self.ask(path, what, warnings or [])
        if choice == "overwrite":
            if remember:
                self.session_ok.add(key)
            self.backup(path)
            return path
        if choice == "new":
            return versioned(path)
        return None

    def targets(self, paths: List[Path], what: str = "files", warnings: Optional[List[str]] = None) -> Optional[Dict[Path, Path]]:
        """Several files written together (e.g. BOM + CPL): one question for all of them."""
        existing = [Path(p) for p in paths if Path(p).exists()]
        if not existing:
            return {Path(p): Path(p) for p in paths}
        keys = {str(p.resolve()).lower() for p in existing}
        if keys <= self.session_ok:
            choice, remember = "overwrite", True
        elif self.ask is None:
            choice, remember = "new", None
        else:
            listing = existing[0] if len(existing) == 1 else Path(", ".join(p.name for p in existing))
            choice, remember = self.ask(listing if len(existing) > 1 else existing[0], what, warnings or [])
        if choice == "cancel" or choice is None:
            return None
        out = {}
        if choice == "overwrite":
            for p in existing:
                self.backup(p)
                if remember:
                    self.session_ok.add(str(p.resolve()).lower())
            return {Path(p): Path(p) for p in paths}
        # new: one shared version number so the pair stays together
        n = 2
        while any(Path(p).with_name(f"{Path(p).stem}_v{n}{Path(p).suffix}").exists() for p in paths):
            n += 1
        for p in paths:
            p = Path(p)
            out[p] = p.with_name(f"{p.stem}_v{n}{p.suffix}")
        return out
