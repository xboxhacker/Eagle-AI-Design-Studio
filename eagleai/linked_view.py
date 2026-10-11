"""'Linked designs…' window: link the other boards this one works with, and the shared system requirements file."""
from __future__ import annotations

import html
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from . import linked


class LinkedDesignsDialog(QDialog):
    """pick_dir(parent, caption, dir) -> str and pick_file(parent, caption, dir, filter) -> (str, _) are the
    studio's crash-safe file dialogs; pick_save has the same shape as pick_file."""

    def __init__(self, project, pick_dir, pick_file, pick_save, parent=None):
        super().__init__(parent)
        self.p = project
        self.pick_dir, self.pick_file, self.pick_save = pick_dir, pick_file, pick_save
        self.messages = []                # shown in the chat when the window closes
        self.setWindowTitle(f"Linked designs - {project.folder.name}")
        self.resize(900, 680)
        v = QVBoxLayout(self)
        intro = QLabel("Link the other boards that work together with this one (for example a controller and "
                       "its display board). The AI gets a short summary of each linked board with every message, can read "
                       "its parts, nets and MD on demand, and is told when the other board changes. Linked boards "
                       "are <b>read only</b> here - open that project to edit it.")
        intro.setWordWrap(True)
        v.addWidget(intro)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Design", "Role / note", "Read from", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._sel_changed)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit_note())
        v.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.add_dir_btn = QPushButton("Link project folder…"); self.add_dir_btn.clicked.connect(self.add_folder)
        self.add_file_btn = QPushButton("Link .sch / .md file…"); self.add_file_btn.clicked.connect(self.add_file)
        self.note_btn = QPushButton("Role / note…"); self.note_btn.clicked.connect(self.edit_note)
        self.open_btn = QPushButton("Open folder"); self.open_btn.clicked.connect(self.open_folder)
        self.remove_btn = QPushButton("Unlink"); self.remove_btn.clicked.connect(self.remove)
        for b in (self.add_dir_btn, self.add_file_btn, self.note_btn, self.open_btn, self.remove_btn):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.back = QCheckBox("Also link the other project back to this one (when it is a studio project folder)")
        self.back.setChecked(True)
        v.addWidget(self.back)

        g = QGroupBox("System requirements file (shared by the linked boards)")
        gl = QVBoxLayout(g)
        self.sys_lbl = QLabel(); self.sys_lbl.setWordWrap(True)
        self.sys_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        gl.addWidget(self.sys_lbl)
        sr = QHBoxLayout()
        self.sys_new = QPushButton("Create…"); self.sys_new.clicked.connect(self.create_sys)
        self.sys_pick = QPushButton("Use existing…"); self.sys_pick.clicked.connect(self.pick_sys)
        self.sys_open = QPushButton("Open"); self.sys_open.clicked.connect(self.open_sys)
        self.sys_unlink = QPushButton("Unlink"); self.sys_unlink.clicked.connect(self.unlink_sys)
        for b in (self.sys_new, self.sys_pick, self.sys_open, self.sys_unlink):
            sr.addWidget(b)
        sr.addStretch(1)
        gl.addLayout(sr)
        v.addWidget(g)

        self.preview_lbl = QLabel()
        v.addWidget(self.preview_lbl)
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True)
        v.addWidget(self.preview, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject); bb.accepted.connect(self.accept)
        v.addWidget(bb)
        self.refresh()

    # ------------------------------------------------------------- view
    def _links(self):
        return linked.links(self.p)

    def selected(self):
        r = self.table.currentRow()
        ls = self._links()
        return ls[r] if 0 <= r < len(ls) else None

    def refresh(self):
        ls = self._links()
        self.table.setRowCount(len(ls))
        for i, l in enumerate(ls):
            c = linked.companion(l["path"])
            status = c.error or f"{len(c.parts)} parts, {len(c.nets)} nets" + (f", MD {c.md_path.name}" if c.md_path else "")
            cells = [linked.label(l["path"], c), l["note"], os.path.relpath(l["path"], self.p.folder)
                     if Path(l["path"]).anchor == self.p.folder.anchor else str(l["path"]), status]
            for j, t in enumerate(cells):
                it = QTableWidgetItem(t)
                it.setToolTip(str(l["path"]) if j == 2 else t)
                if j == 3 and c.error:
                    it.setForeground(Qt.red)
                self.table.setItem(i, j, it)
        self.table.resizeColumnsToContents()
        sd = linked.system_doc_path(self.p)
        if sd is None:
            self.sys_lbl.setText("None. Create one to hold the values both boards must agree on (frequency, "
                                 "connector pinout, supply limits); every linked studio project uses the same file.")
        else:
            self.sys_lbl.setText(f"<b>{html.escape(sd.name)}</b> - {html.escape(str(sd))}"
                                 + ("" if sd.exists() else " <span style='color:#ff6b6b'>(missing)</span>"))
        for b in (self.sys_open, self.sys_unlink):
            b.setEnabled(sd is not None)
        ctx = linked.context(self.p)
        self.preview_lbl.setText(f"What the AI gets with every message (about {len(ctx) // 4} tokens):"
                                 if ctx else "Nothing linked yet - the AI only sees this project.")
        self.preview.setPlainText(ctx)
        self._sel_changed()

    def _sel_changed(self):
        on = self.selected() is not None
        for b in (self.note_btn, self.open_btn, self.remove_btn):
            b.setEnabled(on)

    def _start_dir(self) -> str:
        ls = self._links()
        return str(Path(ls[-1]["path"]).parent if ls else self.p.folder.parent)

    # ------------------------------------------------------------- links
    def _add(self, path: Path):
        note, ok = QInputDialog.getText(self, "Role", "What is this board to the open one? (optional, e.g. "
                                        "'display board for this controller')")
        if not ok:
            return
        msg = linked.add_link(self.p, path, note.strip(), back=self.back.isChecked())
        self.messages.append(msg)
        self.refresh()
        if msg.startswith(("That is", f"{path.name} is already")):
            QMessageBox.information(self, self.windowTitle(), msg)

    def add_folder(self):
        d = self.pick_dir(self, "Linked design: the other board's project folder", self._start_dir())
        if d:
            self._add(Path(d))

    def add_file(self):
        f, _ = self.pick_file(self, "Linked design: the other board's .sch or schematic MD", self._start_dir(),
                              "Eagle schematic or Markdown (*.sch *.md);;design.json (design.json)")
        if f:
            self._add(Path(f))

    def edit_note(self):
        l = self.selected()
        if l is None:
            return
        note, ok = QInputDialog.getText(self, "Role", "What is this board to the open one?", text=l["note"])
        if ok:
            linked.set_note(self.p, l["path"], note.strip())
            self.refresh()

    def open_folder(self):
        l = self.selected()
        if l is not None:
            folder = l["path"] if Path(l["path"]).is_dir() else Path(l["path"]).parent
            if folder.exists():
                os.startfile(str(folder))

    def remove(self):
        l = self.selected()
        if l is None:
            return
        name = linked.label(l["path"], linked.companion(l["path"]))
        if QMessageBox.question(self, self.windowTitle(), f"Unlink {name}? (Nothing is deleted; the other "
                                f"project keeps its own link to this one.)") == QMessageBox.Yes:
            linked.remove_link(self.p, l["path"])
            self.messages.append(f"Unlinked {name}")
            self.refresh()

    # ------------------------------------------------------------- system requirements
    def _common_folder(self) -> Path:
        dirs = [self.p.folder] + [Path(l["path"]) if Path(l["path"]).is_dir() else Path(l["path"]).parent
                                  for l in self._links()]
        try:
            return Path(os.path.commonpath([str(d.resolve()) for d in dirs]))
        except ValueError:
            return self.p.folder

    def create_sys(self):
        f, _ = self.pick_save(self, "New system requirements file", str(self._common_folder() / "SYSTEM.md"),
                              "Markdown (*.md)")
        if not f:
            return
        path = Path(f)
        if path.suffix.lower() != ".md":
            path = path.with_suffix(".md")
        self.messages.append(linked.set_system_doc(self.p, path, create=True))
        self.refresh()
        self.open_sys()

    def pick_sys(self):
        f, _ = self.pick_file(self, "System requirements file", str(self._common_folder()), "Markdown (*.md *.txt)")
        if f:
            self.messages.append(linked.set_system_doc(self.p, Path(f)))
            self.refresh()

    def open_sys(self):
        sd = linked.system_doc_path(self.p)
        if sd is not None and sd.exists():
            os.startfile(str(sd))

    def unlink_sys(self):
        self.messages.append(linked.set_system_doc(self.p, None))
        self.refresh()
