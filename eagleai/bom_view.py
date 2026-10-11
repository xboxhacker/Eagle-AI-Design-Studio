"""Live, editable BOM. Used as the 'BOM' tab and as a pop-out window (both stay in sync).

Every number on screen is rebuilt from the current design whenever anything changes (AI edits, your edits,
imports, scripts, undo). Edits here go straight into the design. Exported files are never shown as if they
were current: they're listed in the banner with a live 'up to date' / 'differences' check.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget, QDialog, QPlainTextEdit, QDialogButtonBox,
                               QAbstractItemView)

from . import bom_sync

COLS = ["Refs", "Qty", "Library part", "Value", "Sees (worst case)", "Rating (min)", "MPN", "LCSC", "Note", "DNP",
        "Unit $", "Stock", "Line $ /board"]
EDITABLE = {"Value": "value", "MPN": "mpn", "LCSC": "lcsc", "Note": "note", "Sees (worst case)": "stress"}
ORANGE = QColor("#ffb454")


def stress_text(st: dict) -> str:
    """Plain numbers so the cell can be edited and read back: '28V 0.06W'."""
    return " ".join(f"{st[k]:g}{k}" for k in ("V", "W", "A") if k in st)


def parse_stress(text: str) -> dict:
    """'28V' '0.06W' '60mW' '1/8W' '20mA 0.1W' -> {"V": 28.0, ...}; '' -> {}. Raises ValueError."""
    out = {}
    text = re.sub(r"(\d+)\s*/\s*(\d+)", lambda m: f"{int(m.group(1)) / max(int(m.group(2)), 1):g}", text or "")
    for num, milli, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(m?)\s*([VWA])\b", text, re.I):
        out[unit.upper()] = float(num) / (1000 if milli else 1)
    if (text or "").strip() and not out:
        raise ValueError(f"'{text}' - write e.g. 28V, 0.06W, 60mW or 20mA")
    return out
RED = QColor("#7a2e2e")
GREY = QColor("#aab3c0")


def _age(ts: float) -> str:
    s = time.time() - ts
    return f"{int(s // 60)} min ago" if s < 3600 else f"{s / 3600:.1f} h ago" if s < 86400 else f"{s / 86400:.0f} d ago"


class BomEditor(QWidget):
    def __init__(self, get_project: Callable, on_edit: Callable[[str], None], is_busy: Callable[[], bool],
                 actions: dict, parent=None):
        """get_project() -> Project; on_edit(text) is called after each user edit (undo snapshot + chat note);
        actions: {'price': fn, 'export': fn} for the buttons."""
        super().__init__(parent)
        self.get_project, self.on_edit, self.is_busy, self.actions = get_project, on_edit, is_busy, actions
        self._loading = False
        self._status = []
        l = QVBoxLayout(self)
        self.banner = QLabel(""); self.banner.setWordWrap(True); self.banner.setTextFormat(Qt.RichText)
        self.banner.setStyleSheet("padding:6px; border-radius:4px;")
        l.addWidget(self.banner)
        bar = QHBoxLayout()
        self.group = QCheckBox("Group identical parts"); self.group.setChecked(True); self.group.toggled.connect(self.refresh)
        self.filter = QLineEdit(); self.filter.setPlaceholderText("Filter (ref, value, MPN, LCSC…)")
        self.filter.textChanged.connect(self.refresh)
        bar.addWidget(self.group); bar.addWidget(self.filter, 1)
        for label, key in (("Fill missing LCSC + prices", "price"), ("Find missing with AI (web)", "ai_find"),
                           ("Paste LCSC list…", "paste"), ("My JLC parts…", "memory"),
                           ("Export JLC BOM+CPL…", "export")):
            b = QPushButton(label); b.clicked.connect(lambda _=False, k=key: self.actions.get(k, lambda: None)()); bar.addWidget(b)
        self.diff_btn = QPushButton("Show differences"); self.diff_btn.clicked.connect(self.show_diffs)
        self.pull_btn = QPushButton("Pull edits from exported file"); self.pull_btn.clicked.connect(self.pull)
        self.open_btn = QPushButton("Open exported file"); self.open_btn.clicked.connect(self.open_file)
        for b in (self.diff_btn, self.pull_btn, self.open_btn):
            bar.addWidget(b)
        l.addLayout(bar)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.itemChanged.connect(self._edited)
        l.addWidget(self.table, 1)
        self.total = QLabel("")
        l.addWidget(self.total)
        self._timer = QTimer(self); self._timer.setSingleShot(True); self._timer.timeout.connect(self._do_refresh)

    # ----------------------------------------------------------------- data
    def refresh(self, *_):
        self._timer.start(120)          # debounce bursts of changes (AI batch edits)

    def _rows(self, p):
        if self.group.isChecked():
            return [dict(g, refs_list=[r.strip() for r in g["refs"].split(",")]) for g in p.design.bom()]
        out = []
        for ref, s in sorted(p.design.parts.items(), key=lambda kv: (kv[0].rstrip("0123456789"),
                                                                      int("0" + "".join(c for c in kv[0] if c.isdigit())))):
            out.append({"refs": ref, "refs_list": [ref], "qty": 1, "library": s.library, "deviceset": s.deviceset,
                        "device": s.device, "value": s.value, "mpn": s.mpn, "lcsc": s.lcsc, "note": s.note, "dnp": s.dnp})
        return out

    @staticmethod
    def _stress_of(p, refs):
        sts = [p.design.parts[r].stress for r in refs if r in p.design.parts and p.design.parts[r].stress]
        if not sts:
            return ""
        txt = stress_text({k: v for k, v in sts[0].items() if k in ("V", "W", "A")})
        return txt + (" …" if any(s != sts[0] for s in sts) else "")

    def _do_refresh(self):
        p = self.get_project()
        if p is None:
            self.table.setRowCount(0)
            self.banner.setText("No project open.")
            return
        prices = p.state().get("prices", {})
        busy = self.is_busy()
        from .ratings import analyze, group_text
        ratings = analyze(p.design, p.index)
        f = self.filter.text().strip().lower()
        rows = [r for r in self._rows(p) if not f or f in " ".join(str(v) for v in r.values()).lower()]
        self._loading = True
        scroll = self.table.verticalScrollBar().value()
        self.table.setRowCount(len(rows))
        total, unpriced, oldest = 0.0, 0, None
        for i, r in enumerate(rows):
            pr = prices.get(r["refs_list"][0])
            fresh = pr if (pr and pr.get("lcsc") and pr["lcsc"] == r["lcsc"]) else None
            if fresh:
                oldest = min(oldest or fresh["ts"], fresh["ts"])
            unit = fresh["unit"] if fresh else None
            line = unit * r["qty"] if unit is not None else None
            if not r["dnp"]:
                if line is None:
                    unpriced += 1
                else:
                    total += line
            rating = group_text(ratings, r["refs_list"])
            vals = [r["refs"], r["qty"], f"{r['library']}/{r['deviceset']}" + (f"/{r['device']}" if r["device"] else ""),
                    r["value"], self._stress_of(p, r["refs_list"]), rating, r["mpn"], r["lcsc"], r["note"], None,
                    f"{unit:.4f}" if unit is not None else "", fresh["stock"] if fresh else "",
                    f"{line:.4f}" if line is not None else ""]
            for j, v in enumerate(vals):
                if COLS[j] == "DNP":
                    it = QTableWidgetItem("")
                    it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    it.setCheckState(Qt.Checked if r["dnp"] else Qt.Unchecked)
                else:
                    it = QTableWidgetItem(str(v))
                    flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
                    if COLS[j] in EDITABLE and not busy:
                        flags |= Qt.ItemIsEditable
                    it.setFlags(flags)
                it.setData(Qt.UserRole, r["refs_list"])
                if r["dnp"]:
                    it.setForeground(QBrush(GREY))
                elif not r["lcsc"]:
                    it.setBackground(QBrush(RED))
                if COLS[j] == "LCSC" and not r["lcsc"] and not r["dnp"]:
                    it.setToolTip("No LCSC number - JLC can't assemble this line. Type one, use Fill missing LCSC, or Find missing with AI (web).")
                if COLS[j] == "Rating (min)":
                    det = [f"{x}: {ratings[x].detail()}" for x in r["refs_list"] if x in ratings and ratings[x].detail()]
                    it.setToolTip("\n".join(det) or "No rating to work out (ICs, connectors: see their datasheets).")
                    if "UNDER-RATED" in rating or rating == "?":
                        it.setForeground(QBrush(ORANGE))
                if COLS[j] == "Sees (worst case)":
                    it.setToolTip("What the part really sees (worst case): volts across, watts dissipated, amps "
                                  "through - e.g. 28V, 0.06W, 20mA. Empty = worked out from the net voltages "
                                  "(Nets tab). The studio derates it into 'Rating (min)'.")
                if COLS[j] == "Unit $" and pr and not fresh:
                    it.setToolTip(f"Old price for {pr.get('lcsc')} hidden - LCSC changed since. Re-run pricing.")
                self.table.setItem(i, j, it)
        self.table.resizeColumnsToContents()
        self.table.verticalScrollBar().setValue(scroll)
        self._loading = False
        n_parts = sum(r["qty"] for r in rows)
        missing = sum(1 for r in rows if not r["lcsc"] and not r["dnp"])
        self.total.setText(f"Live from the design: {n_parts} parts, {len(rows)} lines"
                           f"{', ' + str(missing) + ' without LCSC (red)' if missing else ''}  |  "
                           f"priced ≈ ${(f'{total:.2f}' if total >= 1 else f'{total:.4f}')}/board{' (' + str(unpriced) + ' lines unpriced)' if unpriced else ''}"
                           f"{'  |  prices from ' + _age(oldest) + ' (snapshot, not a quote)' if oldest else ''}"
                           f"{'  |  editing locked while the AI works' if busy else ''}")
        self._update_banner(p)

    def _update_banner(self, p):
        try:
            self._status = bom_sync.export_status(p)
        except Exception as exc:
            self._status = []
            self.banner.setText(f"Export check failed: {exc}")
            return
        if not self._status:
            self.banner.setStyleSheet("padding:6px; border-radius:4px; background:#2b3138; color:#f2f4f8;")
            self.banner.setText("Live BOM (from the design). Nothing exported yet.")
            self.diff_btn.setEnabled(False); self.pull_btn.setEnabled(False); self.open_btn.setEnabled(False)
            return
        lines, bad = [], False
        for st in self._status[:4]:
            when = time.strftime("%m-%d %H:%M", time.localtime(st["at"])) if st.get("at") else "?"
            if st["error"] and not st["differences"]:
                lines.append(f"⚠ <b>{st['name']}</b> ({st['side']}, {when}): {st['error']}")
                bad = True
            elif st["up_to_date"]:
                lines.append(f"✔ <b>{st['name']}</b> ({st['side']}, exported {when}) matches the design"
                             + (" — <b>edited outside the app</b>" if st["edited_outside"] else ""))
            else:
                lines.append(f"✖ <b>{st['name']}</b> ({st['side']}, exported {when}) is <b>OUT OF DATE</b>: "
                             f"{len(st['differences'])} difference(s) — re-export before uploading to JLC"
                             + (" — also <b>edited outside the app</b>" if st["edited_outside"] else ""))
                bad = True
        self.banner.setStyleSheet("padding:6px; border-radius:4px; color:#ffffff; background:%s;" % ("#7a2e2e" if bad else "#1f5e32"))
        self.banner.setText("<br>".join(lines))
        self.diff_btn.setEnabled(any(s["differences"] for s in self._status))
        self.pull_btn.setEnabled(any(s["edited_outside"] for s in self._status))
        self.open_btn.setEnabled(True)

    # ---------------------------------------------------------------- edits
    def _edited(self, it: QTableWidgetItem):
        if self._loading:
            return
        p = self.get_project()
        if p is None or self.is_busy():
            self.refresh()
            return
        col = COLS[it.column()]
        refs: List[str] = it.data(Qt.UserRole) or []
        from .tools import tool_set_value
        if col == "DNP":
            val = it.checkState() == Qt.Checked
            self.on_edit(f"BOM edit: {', '.join(refs)} DNP={'yes' if val else 'no'}")
            for r in refs:
                tool_set_value(p, r, dnp=val)
        elif col in EDITABLE:
            val = it.text().strip()
            field = EDITABLE[col]
            if field == "stress":
                try:
                    st = parse_stress(val)
                except ValueError as exc:
                    QMessageBox.warning(self, "BOM", str(exc))
                    self.refresh()
                    return
                self.on_edit(f"BOM edit: {', '.join(refs)} sees {val or '(cleared)'}")
                from .tools import tool_set_stress
                tool_set_stress(p, {r: st for r in refs})
                self.refresh()
                return
            if field == "lcsc" and val and not (val.upper().startswith("C") and val[1:].isdigit() or val.isdigit()):
                QMessageBox.warning(self, "BOM", f"'{val}' doesn't look like an LCSC number (C followed by digits).")
                self.refresh()
                return
            self.on_edit(f"BOM edit: {', '.join(refs)} {col} = '{val}'")
            for r in refs:
                tool_set_value(p, r, **{field: val})
        self.refresh()

    # ------------------------------------------------------------- export sync
    def show_diffs(self):
        txt = []
        for st in self._status:
            if st["differences"]:
                txt.append(f"{st['name']} ({st['side']}):")
                txt += ["  " + d for d in st["differences"]]
        dlg = QDialog(self); dlg.setWindowTitle("Exported BOM vs. current design"); dlg.resize(760, 480)
        l = QVBoxLayout(dlg)
        ed = QPlainTextEdit("\n".join(txt) or "No differences."); ed.setReadOnly(True); l.addWidget(ed)
        bb = QDialogButtonBox(QDialogButtonBox.Close); bb.rejected.connect(dlg.reject); l.addWidget(bb)
        dlg.exec()

    def pull(self):
        p = self.get_project()
        st = next((s for s in self._status if s["edited_outside"]), None)
        if p is None or st is None:
            return
        changes = bom_sync.pull_candidates(p, Path(st["path"]))
        if not changes:
            QMessageBox.information(self, "BOM", f"{st['name']} was saved outside the app, but its values and LCSC "
                                                 f"numbers already match the design.")
            bom_sync.mark_seen(p, Path(st["path"]))
            self.refresh()
            return
        lst = "\n".join(f"{r}: {f} '{a}' → '{b}'" for r, f, a, b in changes[:40])
        if QMessageBox.question(self, "Pull edits from exported BOM",
                                f"Apply these {len(changes)} change(s) from {st['name']} to the design?\n\n{lst}") \
                == QMessageBox.Yes:
            self.on_edit(f"BOM edits pulled from {st['name']}: " + "; ".join(f"{r} {f}={b}" for r, f, _a, b in changes[:30]))
            bom_sync.apply_pull(p, changes)
            bom_sync.mark_seen(p, Path(st["path"]))
            self.refresh()

    def open_file(self):
        if self._status:
            path = self._status[0]["path"]
            try:
                os.startfile(path)
            except AttributeError:
                import subprocess
                subprocess.Popen(["xdg-open", path])
