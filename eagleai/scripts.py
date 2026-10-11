"""Small local helper scripts the AI can write and reuse (saves tokens: compute, don't reason).

* Stored once for all projects in Documents/EagleAI/scripts/<name>.py, so a helper written
  for one board is reused on the next.
* NOTHING runs until you approve that exact code (a dialog shows it). Approval is remembered by
  a hash of the code; if the AI edits the script, you're asked again.
* Runs with the app's own Python, working folder = the project, 60 s time limit, output capped.
  Environment: EAGLEAI_PROJECT (folder), EAGLEAI_DESIGN (design.json). `import eagleai` works.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

APP_DIR = Path(__file__).resolve().parents[1]


def default_scripts_dir() -> Path:
    return Path.home() / "Documents" / "EagleAI" / "scripts"


class ScriptStore:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.approved_path = self.folder / "approved.json"

    def _approved(self) -> Dict[str, str]:
        try:
            return json.loads(self.approved_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _hash(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    @staticmethod
    def safe(name: str) -> str:
        n = re.sub(r"[^A-Za-z0-9_]", "_", name.strip().removesuffix(".py"))[:60]
        if not n:
            raise ValueError("empty script name")
        return n

    def list(self) -> List[Dict]:
        out = []
        for f in sorted(self.folder.glob("*.py")) if self.folder.is_dir() else []:
            code = f.read_text(encoding="utf-8", errors="ignore")
            m = re.match(r'\s*"""(.*?)"""', code, re.S)
            out.append({"name": f.stem, "description": (m.group(1).strip().splitlines() or [""])[0] if m else "",
                        "approved": self._approved().get(f.stem) == self._hash(code)})
        return out

    def write(self, name: str, code: str, description: str = "") -> Dict:
        name = self.safe(name)
        self.folder.mkdir(parents=True, exist_ok=True)
        if description and not code.lstrip().startswith('"""'):
            code = f'"""{description.strip()}"""\n' + code
        f = self.folder / f"{name}.py"
        replaced = ""
        if f.exists() and f.read_text(encoding="utf-8", errors="replace") != code:
            import time as _t
            hist = self.folder / "_history"
            hist.mkdir(exist_ok=True)
            old = hist / f"{name}.{_t.strftime('%Y%m%d_%H%M%S')}.py"
            old.write_text(f.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            replaced = f"replaced the previous version (kept as _history/{old.name})"
        f.write_text(code, encoding="utf-8")
        return {"saved": name, **({"note": replaced} if replaced else {}), "approved": self._approved().get(name) == self._hash(code)}

    def code(self, name: str) -> Optional[str]:
        f = self.folder / f"{self.safe(name)}.py"
        return f.read_text(encoding="utf-8") if f.exists() else None

    def approve(self, name: str, code: str):
        ap = self._approved()
        ap[self.safe(name)] = self._hash(code)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.approved_path.write_text(json.dumps(ap, indent=1), encoding="utf-8")

    def run(self, name: str, args: List[str], project_dir: Path, design_path: Path,
            ask: Optional[Callable[[str, str], bool]], timeout: float = 60.0) -> Dict:
        name = self.safe(name)
        code = self.code(name)
        if code is None:
            return {"error": f"no script '{name}' (list_scripts shows what exists)"}
        if self._approved().get(name) != self._hash(code):
            if ask is None or not ask(name, code):
                return {"error": "the user did not approve running this script"}
            self.approve(name, code)
        env = dict(os.environ, EAGLEAI_PROJECT=str(project_dir), EAGLEAI_DESIGN=str(design_path),
                   PYTHONPATH=str(APP_DIR) + os.pathsep + os.environ.get("PYTHONPATH", ""), PYTHONIOENCODING="utf-8")
        try:
            r = subprocess.run([sys.executable, str(self.folder / f"{name}.py"), *[str(a) for a in args]],
                               cwd=str(project_dir), env=env, capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return {"error": f"timed out after {timeout:.0f}s"}
        out = {"exit_code": r.returncode, "stdout": r.stdout[-4000:]}
        if r.stderr.strip():
            out["stderr"] = r.stderr[-2000:]
        return out
