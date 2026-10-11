"""Eagle AI Design Studio - PySide6 desktop shell.

Layout: chat (left, with AI brand + model + "double-check with" pickers) | tabs: Preview / BOM+JLC / Nets /
Libraries / design.json (centre) ; Problems (bottom) ; running cost (status bar).
Agent, library scans, JLC lookups, imports and conversions run on worker threads.
"""
from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QSettings, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QKeySequence, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QToolBar,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QButtonGroup)

from . import __version__, bom_sync, eagle_cli, eagle_link, jlc, jlc_ai, jlc_export, placement, router, rules as rules_mod, ul_import
from .bom_view import BomEditor
from .agent import Agent
from .library import repair_xml_lbr
from .models import BUILTIN, diff_new, fetch_all
from .preview import render_svg
from .svg_view import SvgPreview
from .providers import BRANDS, DEFAULT_MODEL, get_key, set_key
from .tools import Project, Settings, dispatch, price_bom

APP = "Eagle AI Design Studio"
MAX_RECENT = 15


def anthropic_client():
    k = get_key("claude")
    if not k:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=k)


# -------------------------------------------------------------------- workers

# ------------------------------------------------------------------- file dialogs
# DIALOG_MODE
#   "safe"   - Windows' own dialog, shown by a small helper process (native_dialog_child.py). Explorer add-ons
#              load into the helper, not the studio, so a broken one can't crash the app. (default)
#   "inproc" - Windows' own dialog inside the studio (Qt native).
#   "qt"     - Qt's built-in dialog.
# Every dialog is traced in dialog_debug.log (crashdiag.py).
DIALOG_MODE = "safe"
DIALOG_MODES = {"safe": "Windows dialogs - crash-safe (separate helper process)",
                "inproc": "Windows dialogs - inside the app",
                "qt": "Built-in Qt dialogs"}
_child_failed_note = []
_USE_QT = object()                                # the user stopped the helper and asked for the built-in dialog
CHILD = Path(__file__).with_name("native_dialog_child.py")


def _fd_options(extra=None):
    opts = QFileDialog.Option(0) if extra is None else extra
    if DIALOG_MODE == "qt":
        opts |= QFileDialog.DontUseNativeDialog
    return opts


def _tk_types(qt_filter: str):
    out = []
    for part in (qt_filter or "").split(";;"):
        m = __import__("re").match(r"\s*(.*?)\s*\(([^)]*)\)", part)
        if m:
            out.append((m.group(1) or m.group(2), m.group(2)))
    if not any(p[1] in ("*", "*.*") for p in out):
        out.append(("All files", "*.*"))
    return out


def _child_dialog(kind, caption, directory, qt_filter="", no_confirm=False):
    """Show the Windows dialog from the helper process; keeps the studio responsive. Returns the path ('' =
    cancelled), None if the helper crashed (caller falls back to the built-in dialog), _USE_QT if the user stopped
    it and asked for the built-in dialog, or False if this Python has no Tk (caller uses the in-app Windows
    dialog)."""
    import json as _json
    import threading as _th
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    if os.name == "nt" and w.exists():
        exe = w                                   # no console window flashing up
    d = Path(directory) if directory else None
    req = {"kind": kind, "title": caption, "types": _tk_types(qt_filter), "no_confirm": no_confirm}
    if d is not None and d.suffix and kind == "save":
        req.update(dir=str(d.parent), file=d.name, ext=d.suffix)
    elif d is not None:
        req["dir"] = str(d if d.is_dir() or not d.suffix else d.parent)
    box = {}
    try:
        proc = subprocess.Popen([str(exe), "-I", str(CHILD), _json.dumps(req)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        _child_failed_note.append(f"could not start the dialog helper: {exc}")
        return None
    if os.name == "nt":
        try:                                      # let the helper's dialog come to the front
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(proc.pid)
        except Exception:
            pass

    def run():
        try:
            out, err = proc.communicate()
            box["rc"], box["out"], box["err"] = proc.returncode, out, err
        except Exception as exc:
            box["rc"], box["out"], box["err"] = -1, "", str(exc)
    t = _th.Thread(target=run, daemon=True)
    t.start()
    # A small "waiting" box (not the main window disabled): if the dialog hides behind other windows you can
    # bring it to the front or cancel it - the studio never looks frozen. Closing the box in any way (button,
    # X, Esc) stops the helper; "built-in" also covers a Windows dialog that froze (a hung Explorer add-on).
    parent = QApplication.activeWindow()
    wait = QDialog(parent); wait.setWindowTitle("File dialog")
    wl = QVBoxLayout(wait)
    wl.addWidget(QLabel(f"<b>{html.escape(caption or 'Choose a file')}</b><br>The Windows file dialog is open "
                        "(it runs separately so it can't crash the studio).<br>Can't see it? It may be behind "
                        "this window or on the taskbar.<br>Frozen or 'Not responding'? Use the built-in dialog."))
    wr = QHBoxLayout()
    builtin = QPushButton("Use built-in dialog instead")
    builtin.clicked.connect(lambda: (box.__setitem__("builtin", True), wait.reject()))
    cancel = QPushButton("Cancel dialog")
    cancel.clicked.connect(wait.reject)
    wr.addStretch(1); wr.addWidget(builtin); wr.addWidget(cancel); wl.addLayout(wr)

    def stop_helper():
        if t.is_alive():                          # the helper may have answered just before the box closed
            box["cancelled"] = True
            try:
                proc.kill()
            except OSError:
                pass
    wait.rejected.connect(stop_helper)
    wait.setModal(True)
    timer = QTimer(wait)
    timer.timeout.connect(lambda: (not t.is_alive()) and wait.accept())
    timer.start(50)
    wait.show()
    if os.name == "nt":
        QTimer.singleShot(150, wait.lower)        # keep the waiting box behind the real dialog
    wait.exec()
    timer.stop()
    t.join(timeout=2)
    wait.deleteLater()
    if parent:
        parent.raise_(); parent.activateWindow()
    if box.get("cancelled"):
        return _USE_QT if box.get("builtin") else ""
    try:
        res = _json.loads(box.get("out") or "")
        if res.get("ok"):
            return res.get("path", "")
    except ValueError:
        pass
    rc = box.get("rc")
    if "tkinter" in (box.get("err") or ""):
        return False                              # this Python has no Tk: use the in-app Windows dialog instead
    if isinstance(rc, int) and (rc < 0 or rc > 255):
        rc_txt = f"{rc & 0xFFFFFFFF:#010x}"       # Windows crash code, e.g. 0xc0000005 / 0xc0000374
    else:
        rc_txt = str(rc)
    _child_failed_note.append(f"dialog helper failed: exit {rc_txt} {(box.get('err') or '').strip()[-300:]}")
    return None


def _traced(kind, caption, directory, qt_call, child_args):
    from . import crashdiag
    directory = str(directory or "")
    t0 = crashdiag.opened(kind, caption, directory, DIALOG_MODE)
    res = None
    if DIALOG_MODE == "safe":
        path = _child_dialog(kind, caption, directory, *child_args)
        if path is _USE_QT:
            crashdiag.closed(kind, t0, "helper stopped by the user (frozen?) - built-in dialog used")
            t0 = crashdiag.opened(kind + " (built-in)", caption, directory, "qt")
            res = qt_call(QFileDialog.DontUseNativeDialog)
        elif path is False:
            crashdiag.closed(kind, t0, "helper unavailable (no Tk in this Python) - in-app Windows dialog used")
            t0 = crashdiag.opened(kind + " (in-app)", caption, directory, "inproc")
            res = qt_call(QFileDialog.Option(0))
        elif path is None:
            crashdiag.closed(kind, t0, "HELPER FAILED: " + _child_failed_note[-1])
            t0 = crashdiag.opened(kind + " (fallback Qt)", caption, directory, "qt")
            res = qt_call(QFileDialog.DontUseNativeDialog)
            QTimer.singleShot(0, lambda: _report_child_failure())
        else:
            res = path if kind == "folder" else (path, "")
    else:
        res = qt_call(QFileDialog.Option(0))
    crashdiag.closed(kind, t0, res if isinstance(res, str) else res[0])
    return res


def _report_child_failure():
    win = QApplication.activeWindow()
    if _child_failed_note and win is not None:
        QMessageBox.warning(win, APP, "The Windows file dialog crashed (in its helper process, so the studio "
                                      "kept running) and the built-in dialog was used instead.\n\n"
                                      + _child_failed_note[-1] + "\n\nAbout -> Crash report names the Explorer "
                                      "add-on that crashed. If it keeps happening: Settings -> File dialogs -> "
                                      "Built-in Qt dialogs.")


def _dlg_dir(parent, caption="", directory="", options=None):
    base = options if options is not None else QFileDialog.ShowDirsOnly
    return _traced("folder", caption, directory,
                   lambda extra: QFileDialog.getExistingDirectory(parent, caption, str(directory or ""),
                                                                  _fd_options(base) | extra), ())


def _dlg_open(parent, caption="", directory="", filter="", options=None):
    return _traced("open", caption, directory,
                   lambda extra: QFileDialog.getOpenFileName(parent, caption, str(directory or ""), filter,
                                                             options=_fd_options(options) | extra), (filter,))


def _dlg_save(parent, caption="", directory="", filter="", options=None):
    no_confirm = bool(options is not None and options & QFileDialog.DontConfirmOverwrite)
    return _traced("save", caption, directory,
                   lambda extra: QFileDialog.getSaveFileName(parent, caption, str(directory or ""), filter,
                                                             options=_fd_options(options) | extra),
                   (filter, no_confirm))


class ChatInput(QPlainTextEdit):
    """The message box. Pasted / dropped pictures (screenshots, copied images, image files) become attachments
    instead of text. Copies from Word/Excel carry a picture of the text too - those still paste as text."""
    images = Signal(list)

    def _pictures(self, md) -> list:
        from . import images as im
        out = []
        if md.hasUrls():
            for u in md.urls():
                if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in im.IMAGE_EXT:
                    a = im.from_file(u.toLocalFile())
                    if a:
                        out.append(a)
        if not out and md.hasImage() and not (md.hasText() and md.text().strip()):
            a = im.from_qimage(md.imageData())
            if a:
                out.append(a)
        return out

    def canInsertFromMimeData(self, md):
        return md.hasImage() or md.hasUrls() or super().canInsertFromMimeData(md)

    def insertFromMimeData(self, md):
        pics = self._pictures(md)
        if pics:
            self.images.emit(pics)
        elif md.hasText():
            super().insertFromMimeData(md)


class AgentWorker(QThread):
    text = Signal(str)
    tool = Signal(str, str)
    done = Signal(str)
    failed = Signal(str)
    design_changed = Signal()

    def __init__(self, agent: Agent, prompt: str, brand: str, model: str, review: bool = False, images=None):
        super().__init__()
        self.agent, self.prompt, self.brand, self.model, self.review = agent, prompt, brand, model, review
        self.images = images or []

    def run(self):
        try:
            res = self.agent.run(self.prompt, on_text=self.text.emit, brand=self.brand, model=self.model,
                                 review=self.review, images=self.images,
                                 on_tool=lambda n, a, r: (self.tool.emit(n, json.dumps(a)[:300] + "  ->  " + json.dumps(r)[:300]),
                                                          self.design_changed.emit()))
            self.done.emit(res)
        except Exception as exc:
            traceback.print_exc()
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class FuncWorker(QThread):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.result.emit(self.fn(self.progress.emit))
        except Exception as exc:
            traceback.print_exc()
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ProjectSignals(QObject):
    """Project.changed() may run on the AI's worker thread; this hops the notification to the UI thread."""
    changed = Signal()


class ScriptApprover(QObject):
    """Lets the agent thread ask the user (on the UI thread) before a new script runs."""
    ask = Signal(str, str, object)

    def __init__(self, parent):
        super().__init__()
        self.parent_w = parent
        self.ask.connect(self._ask, Qt.BlockingQueuedConnection)

    def __call__(self, name: str, code: str) -> bool:
        box = [False]
        if QThread.currentThread() is QApplication.instance().thread():
            self._ask(name, code, box)
        else:
            self.ask.emit(name, code, box)          # blocks this worker until the user answers
        return box[0]

    @Slot(str, str, object)
    def _ask(self, name, code, box):
        dlg = QDialog(self.parent_w)
        dlg.setWindowTitle(f"Run script '{name}'?")
        dlg.resize(760, 560)
        l = QVBoxLayout(dlg)
        l.addWidget(QLabel("The AI wants to run this Python script on your PC. Read it before approving.\n"
                           "Approval is remembered until the script changes."))
        ed = QPlainTextEdit(code); ed.setReadOnly(True); ed.setFont(QFont("Consolas", 10))
        l.addWidget(ed, 1)
        bb = QDialogButtonBox()
        bb.addButton("Approve and run", QDialogButtonBox.AcceptRole)
        bb.addButton("Don't run", QDialogButtonBox.RejectRole)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        box[0] = dlg.exec() == QDialog.Accepted


class LbrPathAsker(QObject):
    """Lets the agent thread ask (on the UI thread) where a new library should be saved."""
    ask = Signal(str, object, object)

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.ask.connect(self._ask, Qt.BlockingQueuedConnection)

    def __call__(self, name, default):
        box = [None]
        if QThread.currentThread() is QApplication.instance().thread():
            self._ask(name, default, box)
        else:
            self.ask.emit(name, default, box)
        return box[0]

    @Slot(str, object, object)
    def _ask(self, name, default, box):
        box[0] = self.main.choose_lbr_path(name, default)


class OverwriteAsker(QObject):
    """Any thread -> UI: an existing file is about to be replaced. Returns (choice, remember)."""
    ask = Signal(object, str, object, object)

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.ask.connect(self._ask, Qt.BlockingQueuedConnection)

    def __call__(self, path, what, warnings):
        box = [("cancel", None)]
        if QThread.currentThread() is QApplication.instance().thread():
            self._ask(path, what, warnings, box)
        else:
            self.ask.emit(path, what, warnings, box)
        return box[0]

    @Slot(object, str, object, object)
    def _ask(self, path, what, warnings, box):
        import time as _t
        from .safewrite import versioned
        path = Path(path)
        dlg = QDialog(self.main); dlg.setWindowTitle("File already exists")
        l = QVBoxLayout(dlg)
        info = ""
        if path.exists():
            st = path.stat()
            info = (f"<br><span style='color:#b9c2d0'>{html.escape(str(path))}<br>last changed "
                    f"{_t.strftime('%Y-%m-%d %H:%M', _t.localtime(st.st_mtime))}, {st.st_size:,} bytes</span>")
            new_name = versioned(path).name
        else:
            new_name = "new numbered copies (_v2…)"
        txt = f"<b>The {html.escape(what)} already exists:</b> {html.escape(path.name)}{info}"
        if warnings:
            txt += "<br><br><b style='color:#ffb454'>" + "<br>".join(html.escape(w) for w in warnings) + "</b>"
        lab = QLabel(txt); lab.setWordWrap(True); lab.setTextFormat(Qt.RichText); l.addWidget(lab)
        remember = QCheckBox("Don't ask again for this file until the studio is closed"); l.addWidget(remember)
        row = QHBoxLayout()
        res = ["cancel"]
        for label, key in ((f"Save as new: {new_name}", "new"), ("Overwrite (backup kept)", "overwrite"), ("Cancel", "cancel")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=key: (res.__setitem__(0, k), dlg.accept()))
            row.addWidget(b)
            if key == "new":
                b.setDefault(True)
        l.addLayout(row)
        dlg.exec()
        dlg.deleteLater()
        box[0] = (res[0], remember.isChecked() if res[0] == "overwrite" else None)


class RouteAsker(QObject):
    ask = Signal(object, object, bool, object)

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.ask.connect(self._ask, Qt.BlockingQueuedConnection)

    def __call__(self, board, res, live):
        box = [None]
        if QThread.currentThread() is QApplication.instance().thread():
            self._ask(board, res, live, box)
        else:
            self.ask.emit(board, res, live, box)
        return box[0]

    @Slot(object, object, bool, object)
    def _ask(self, board, res, live, box):
        box[0] = self.main.review_route(board, res, live)


class PlacementAsker(QObject):
    """Agent thread -> UI: show the proposed placement, let the user apply it (Eagle link or .brd) or reject it."""
    ask = Signal(object, object, bool, object)

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.ask.connect(self._ask, Qt.BlockingQueuedConnection)

    def __call__(self, board, res, live):
        box = [None]
        if QThread.currentThread() is QApplication.instance().thread():
            self._ask(board, res, live, box)
        else:
            self.ask.emit(board, res, live, box)
        return box[0]

    @Slot(object, object, bool, object)
    def _ask(self, board, res, live, box):
        box[0] = self.main.review_placement(board, res, live)


# ------------------------------------------------------------------- settings
def _subdirs(p: Path) -> list:
    try:
        return [c for c in p.iterdir() if c.is_dir()]
    except OSError:
        return []


def default_lib_dirs() -> list:
    """Typical Eagle 6/7/8/9 library folders on Windows. Each is scanned with all sub-folders."""
    out = []
    bases = {"C:\\", os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             str(Path.home()), os.environ.get("ProgramData", r"C:\ProgramData")}
    for base in sorted(bases):
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for n in sorted(names):
            if n.upper().startswith(("EAGLE", "CADSOFT", "AUTODESK")):
                top = Path(base) / n
                for cand in [top / "lbr", top / "libraries"] + [c / "lbr" for c in _subdirs(top) if c.name.upper().startswith("EAGLE")]:
                    if cand.is_dir():
                        out.append(str(cand))
    home = Path.home()
    for d in (home / "Documents" / "EAGLE", home / "Documents" / "EAGLE" / "libraries",
              home / "Documents" / "EagleAI" / "libraries", home / "OneDrive" / "Documents" / "EAGLE"):
        if d.exists():
            out.append(str(d))
    # drop folders already covered by a parent that is in the list
    out = list(dict.fromkeys(out))
    return [d for d in out if not any(o != d and d.lower().startswith(o.lower().rstrip("\\/") + os.sep) for o in out)]


class SettingsDialog(QDialog):
    def __init__(self, s: Settings, claude_models: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Settings - {APP} {__version__}")
        self.setMinimumWidth(680)
        self.s = s
        f = QFormLayout(self)
        self.keys = {}
        for brand, (label, _u, env, _b) in BRANDS.items():
            e = QLineEdit(get_key(brand)); e.setEchoMode(QLineEdit.Password)
            e.setPlaceholderText(f"or set {env}")
            self.keys[brand] = e
            f.addRow(f"{label} API key", e)
        self.dmodel = QComboBox(); self.dmodel.addItems(claude_models); self.dmodel.setEditable(True)
        self.dmodel.setCurrentText(s.datasheet_model)
        self.budget = QDoubleSpinBox(); self.budget.setRange(0, 1000); self.budget.setDecimals(2)
        self.budget.setValue(s.budget_usd or 0); self.budget.setSpecialValueText("no limit")
        self.qty = QSpinBox(); self.qty.setRange(1, 100000); self.qty.setValue(s.build_qty)
        self.fee = QDoubleSpinBox(); self.fee.setRange(0, 50); self.fee.setValue(s.extended_fee)
        self.libs = QPlainTextEdit("\n".join(s.lib_dirs)); self.libs.setFixedHeight(80)
        self.csv = QLineEdit(s.jlc_csv)
        self.eagle = QLineEdit(s.eagle_exe or eagle_cli.find_eagle() or "")
        self.conv = QLineEdit(s.converted_dir or str(eagle_cli.default_converted_dir()))
        self.style = QComboBox(); self.style.addItems(["US", "EU"]); self.style.setCurrentText(s.passive_style)
        self.lbr_dir = QLineEdit(s.lbr_dir); self.lbr_dir.setPlaceholderText("<project>\\libs")
        self.lbr_ask = QCheckBox("Ask where to save every new library part"); self.lbr_ask.setChecked(s.lbr_ask)
        self.native = QComboBox()
        for k, v in DIALOG_MODES.items():
            self.native.addItem(v, k)
        self.native.setCurrentIndex(list(DIALOG_MODES).index(DIALOG_MODE))
        self.pages = QCheckBox("Send only pinout/package pages of datasheets (full PDF if that fails)")
        self.pages.setChecked(s.datasheet_pages)
        f.addRow("Datasheet model (Claude reads PDFs)", self.dmodel)
        f.addRow("Session budget (USD, all AIs)", self.budget)
        f.addRow("Boards per build (JLC pricing)", self.qty)
        f.addRow("Extended-part fee (USD)", self.fee)
        lib_row = QVBoxLayout(); lib_row.addWidget(self.libs)
        lb = QHBoxLayout()
        add = QPushButton("Add folder…"); add.clicked.connect(self._add_lib_dir)
        det = QPushButton("Auto-detect Eagle folders"); det.clicked.connect(self._detect_lib_dirs)
        lb.addWidget(add); lb.addWidget(det); lb.addStretch(1); lib_row.addLayout(lb)
        f.addRow("Eagle library folders\n(one per line, sub-folders\nare scanned too)", lib_row)
        f.addRow("JLC parts CSV (optional)", self.csv)
        f.addRow("eagle.exe (7.4)", self.eagle)
        f.addRow("Converted libraries go to", self.conv)
        f.addRow("Resistor/capacitor symbols", self.style)
        lr = QHBoxLayout(); lr.addWidget(self.lbr_dir, 1)
        lbb = QPushButton("Browse…")
        lbb.clicked.connect(lambda: (lambda d: d and self.lbr_dir.setText(d))(
            _dlg_dir(self, "New library parts (datasheet parts, coils) go to")))
        lr.addWidget(lbb)
        f.addRow("New libraries go to", lr)
        f.addRow("", self.lbr_ask)
        f.addRow("File dialogs", self.native)
        f.addRow("Token saving", self.pages)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _add_lib_dir(self):
        d = _dlg_dir(self, "Choose a folder that contains .lbr files (sub-folders are scanned too)")
        if d:
            cur = self.libs.toPlainText().strip()
            self.libs.setPlainText((cur + "\n" if cur else "") + d)

    def _detect_lib_dirs(self):
        have = [l.strip() for l in self.libs.toPlainText().splitlines() if l.strip()]
        found = [d for d in default_lib_dirs() if d not in have]
        self.libs.setPlainText("\n".join(have + found))
        QMessageBox.information(self, APP, f"Added {len(found)} folder(s)." if found else
                                "No standard Eagle library folders found - use Add folder… to pick yours.")

    def apply(self):
        s = self.s
        for brand, e in self.keys.items():
            if e.text().strip() != get_key(brand):
                try:
                    set_key(brand, e.text().strip())
                except Exception as exc:
                    QMessageBox.warning(self, APP, f"Could not store the {BRANDS[brand][0]} key securely ({exc}). "
                                                   f"Set {BRANDS[brand][2]} instead.")
        s.datasheet_model = self.dmodel.currentText().strip()
        s.budget_usd = self.budget.value() or None
        s.build_qty, s.extended_fee = self.qty.value(), self.fee.value()
        s.lib_dirs = [l.strip() for l in self.libs.toPlainText().splitlines() if l.strip()]
        s.jlc_csv = self.csv.text().strip()
        s.eagle_exe = self.eagle.text().strip()
        s.converted_dir = self.conv.text().strip()
        s.passive_style = self.style.currentText()
        s.lbr_dir = self.lbr_dir.text().strip()
        s.lbr_ask = self.lbr_ask.isChecked()
        global DIALOG_MODE
        DIALOG_MODE = self.native.currentData()
        s.datasheet_pages = self.pages.isChecked()


class JlcExportDialog(QDialog):
    def __init__(self, brds: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export for JLCPCB assembly")
        f = QFormLayout(self)
        self.brd = QComboBox(); self.brd.addItems([str(b) for b in brds]); self.brd.setEditable(True)
        pick = QPushButton("Browse…"); pick.clicked.connect(self._browse)
        row = QHBoxLayout(); row.addWidget(self.brd, 1); row.addWidget(pick)
        f.addRow("Board file (.brd)", row)
        self.side = QButtonGroup(self)
        srow = QHBoxLayout()
        for i, (lab, key) in enumerate((("Top", "top"), ("Bottom", "bottom"), ("Both (one combined file)", "both"))):
            rb = QRadioButton(lab); rb.setProperty("key", key); self.side.addButton(rb, i); srow.addWidget(rb)
            if i == 0:
                rb.setChecked(True)
        f.addRow("Assembly side", srow)
        self.rot = QComboBox(); self.rot.addItems(["Bottom rotation as shown in Eagle", "Bottom rotation mirrored (360 - angle)"])
        f.addRow("Bottom parts", self.rot)
        self.fill = QCheckBox("Look up cheapest in-stock LCSC numbers for parts that have none (JLC lookup)")
        self.fill.setChecked(True)
        f.addRow("", self.fill)
        f.addRow(QLabel("Files: BOM_<board>_<side>.xlsx (Comment, Designator, Footprint, LCSC Part #) and\n"
                        "CPL_<board>_<side>.xlsx (Designator, Mid X, Mid Y, Layer, Rotation) in <project>/jlc/.\n"
                        "Check every part in JLC's assembly preview before paying - rotations can need a nudge."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _browse(self):
        p, _ = _dlg_open(self, "Board file", "", "Eagle board (*.brd)")
        if p:
            self.brd.setCurrentText(p)

    def values(self):
        return (Path(self.brd.currentText()), self.side.checkedButton().property("key"),
                "mirror" if self.rot.currentIndex() == 1 else "eagle", self.fill.isChecked())


class DestDialog(QDialog):
    """Where to put an imported library (pre-filled with <library folder>/UltraLibrarian/<part>)."""

    def __init__(self, info: dict, default: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import library zip")
        self.resize(720, 0)
        f = QFormLayout(self)
        f.addRow(QLabel(f"<b>{html.escape(Path(info['zip']).name)}</b><br>Contains: {html.escape(info['kind'])}<br>"
                        f"{len(info['all'])} files"))
        self.part = QLineEdit(info["part"])
        f.addRow("Part / library name", self.part)
        self.dest = QLineEdit(str(default))
        b = QPushButton("Browse…"); b.clicked.connect(self._browse)
        row = QHBoxLayout(); row.addWidget(self.dest, 1); row.addWidget(b)
        f.addRow("Unzip to (created if needed)", row)
        self.part.textChanged.connect(lambda t: self.dest.setText(str(Path(self.dest.text()).parent / t)))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _browse(self):
        d = _dlg_dir(self, "Parent folder for this part", str(Path(self.dest.text()).parent))
        if d:
            self.dest.setText(str(Path(d) / self.part.text()))


def _hscroll(page: QWidget) -> QScrollArea:
    """Wrap a tab page so it can get narrower than its button rows (a sideways scrollbar appears)."""
    sa = QScrollArea()
    sa.setWidget(page)
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)       # the tables inside scroll vertically themselves
    return sa


class ProjectFilesDialog(QDialog):
    """The folder has several schematics or MD files: pick the .sch (the design) and the MD (linked to it)."""

    def __init__(self, folder: Path, schs, sch, mds, md, need_design: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP} - load project files")
        self.setMinimumWidth(560)
        f = QFormLayout(self)
        intro = QLabel("This folder has more than one schematic or Markdown file. Choose what to load - both are "
                       "loaded: the <b>.sch</b> becomes the design (pins exactly as in Eagle), the <b>MD</b> is "
                       "linked to it (its title, summary, citations and sections are kept, a .orig backup is saved "
                       "once, then it is kept up to date from the design).")
        intro.setWordWrap(True)
        f.addRow(intro)
        rel = lambda x: str(x.relative_to(folder)) if x.is_relative_to(folder) else str(x)
        self.sch_box = None
        if need_design:
            self.sch_box = QComboBox()
            for c in schs:
                self.sch_box.addItem(rel(c), c)
            self.sch_box.addItem("(none - build the design from the MD)" if mds else "(start empty)", None)
            self.sch_box.setCurrentIndex(schs.index(sch) if sch in schs else self.sch_box.count() - 1)
            f.addRow("Schematic (.sch)", self.sch_box)
        self.md_box = QComboBox()
        for c in mds:
            self.md_box.addItem(rel(c), c)
        self.md_box.addItem("(none - the studio writes a new MD)", None)
        self.md_box.setCurrentIndex(mds.index(md) if md in mds else self.md_box.count() - 1)
        f.addRow("Schematic MD", self.md_box)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)

    def choice(self):
        return (self.sch_box.currentData() if self.sch_box else None), self.md_box.currentData()


# ----------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP} {__version__}")
        self.resize(1500, 920)
        self.qs = QSettings("EagleAI", "IDE")
        s = self.settings = Settings()
        s.brand = self.qs.value("brand", "claude", type=str)
        if s.brand not in BRANDS:
            s.brand = "claude"
        s.model = self.qs.value(f"model_{s.brand}", DEFAULT_MODEL[s.brand], type=str) or DEFAULT_MODEL[s.brand]
        s.datasheet_model = self.qs.value("dmodel", s.datasheet_model)
        s.budget_usd = self.qs.value("budget", 5.0, type=float) or None
        s.build_qty = self.qs.value("qty", 10, type=int)
        s.extended_fee = self.qs.value("fee", 3.0, type=float)
        s.lib_dirs = [d for d in (self.qs.value("libs", [], type=list) or []) if d] or default_lib_dirs()
        s.jlc_csv = self.qs.value("csv", "", type=str)
        s.eagle_exe = self.qs.value("eagle", "", type=str) or (eagle_cli.find_eagle() or "")
        s.converted_dir = self.qs.value("converted", "", type=str)
        s.passive_style = self.qs.value("style", "US", type=str)
        s.datasheet_pages = self.qs.value("pages", True, type=bool)
        s.lbr_dir = self.qs.value("lbr_dir", "", type=str)
        s.lbr_ask = self.qs.value("lbr_ask", False, type=bool)
        s.rules_preset = self.qs.value("rules_preset", "jlc_standard", type=str)
        global DIALOG_MODE
        DIALOG_MODE = self.qs.value("dialog_mode", "safe", type=str)
        if DIALOG_MODE not in DIALOG_MODES:
            DIALOG_MODE = "safe"
        from . import crashdiag
        crashdiag.session_start(DIALOG_MODE)
        died = crashdiag.last_session_died_in_dialog()
        if died:
            QTimer.singleShot(800, lambda: self._died_in_dialog(died))
        self.models = {b: list(dict.fromkeys(BUILTIN.get(b, []) + (self.qs.value(f"known_models_{b}", [], type=list) or [])))
                       for b in BRANDS}
        self.project: Project | None = None
        self.agent: Agent | None = None
        self.worker = None
        self._bg = []
        self.approver = ScriptApprover(self)
        self.lbr_asker = LbrPathAsker(self)
        self.placement_asker = PlacementAsker(self)
        self.route_asker = RouteAsker(self)
        self.overwrite_asker = OverwriteAsker(self)
        try:
            eagle_link.install()                      # keep studio.ulp current with this version
        except OSError:
            pass
        self.link_watch = QFileSystemWatcher(self)
        self.link_watch.addPath(str(eagle_link.bridge_dir()))
        self.link_watch.directoryChanged.connect(lambda _p: QTimer.singleShot(300, self._eagle_synced))
        self._link_stamp = self._stamp()
        self.signals = ProjectSignals()
        self.signals.changed.connect(self._design_changed)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(lambda _p: self._watch_exports())
        self.watcher.fileChanged.connect(lambda _p: (self._watch_exports(), self.refresh_bom()))
        self.bom_window = None
        self._build_ui()
        last = self.qs.value("project", "", type=str)
        if last and Path(last).is_dir():
            self.open_project(Path(last))
        else:
            self.statusBar().showMessage("New/Open project to begin")
        QTimer.singleShot(1500, self.check_models)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        tb = QToolBar(); tb.setMovable(False); self.addToolBar(tb)
        self.proj_btn = QToolButton(); self.proj_btn.setText("Project ▾"); self.proj_btn.setPopupMode(QToolButton.InstantPopup)
        self.proj_menu = QMenu(self); self.proj_btn.setMenu(self.proj_menu)
        self.proj_menu.aboutToShow.connect(self._fill_project_menu)
        tb.addWidget(self.proj_btn)
        for label, fn, key in [("Import .sch / .md…", self.import_dialog, "Ctrl+I"),
                               ("Import library zip…", self.import_zip, ""),
                               ("Settings", self.show_settings, ""),
                               ("Calculators", self.show_calculators, ""), ("Check", self.run_checks, "F5"),
                               ("BOM (live)", self.show_bom_window, "Ctrl+B"),
                               ("Generate .sch", self.gen_sch, "Ctrl+G"), ("Open in Eagle", self.open_eagle, ""),
                               ("Eagle link…", self.eagle_link_dialog, ""),
                               ("Design rules…", self.rules_dialog, ""),
                               ("Linked designs…", self.show_linked, ""),
                               ("Datasheet → part…", self.add_datasheet, ""), ("Price BOM", self.price, ""),
                               ("Export JLC BOM+CPL…", self.export_jlc, ""), ("About", self.about, "")]:
            a = QAction(label, self); a.triggered.connect(fn)
            if key:
                a.setShortcut(QKeySequence(key))
            tb.addAction(a)
        o = QAction(self); o.setShortcut(QKeySequence("Ctrl+O")); o.triggered.connect(self.pick_project); self.addAction(o)
        u = QAction(self); u.setShortcut(QKeySequence("Ctrl+Z")); u.triggered.connect(self.undo); self.addAction(u)
        self.calc_window = None

        split = self.split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(7)
        split.setChildrenCollapsible(False)
        chat = QWidget(); cl = QVBoxLayout(chat)
        chat.setMinimumWidth(240)
        top = QHBoxLayout()
        top.addWidget(QLabel("AI"))
        self.brand_box = QComboBox()
        for b, (label, *_r) in BRANDS.items():
            self.brand_box.addItem(label, b)
        self.brand_box.setCurrentIndex(list(BRANDS).index(self.settings.brand))
        self.brand_box.currentIndexChanged.connect(self._brand_picked)
        top.addWidget(self.brand_box)
        self.model_box = QComboBox(); self.model_box.setEditable(True)
        self._fill_models()
        self.model_box.currentTextChanged.connect(self._model_picked)
        top.addWidget(self.model_box, 1)
        cl.addLayout(top)
        rv = QHBoxLayout()
        rv.addWidget(QLabel("Double-check with"))
        self.review_box = QComboBox()
        for b, (label, *_r) in BRANDS.items():
            self.review_box.addItem(label, b)
        self.review_box.setCurrentIndex(1 if self.settings.brand == "claude" else 0)
        self.review_btn = QPushButton("Review design")
        self.review_btn.setToolTip("A second AI checks the design read-only (it can't change anything) and lists problems")
        self.review_btn.clicked.connect(self.review)
        rv.addWidget(self.review_box, 1); rv.addWidget(self.review_btn)
        cl.addLayout(rv)
        for box in (self.brand_box, self.model_box, self.review_box):     # may shrink with a narrow chat
            box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(6)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setFont(QFont("Segoe UI", 10))
        self.log.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
                                         | Qt.LinksAccessibleByMouse)
        self.log.setContextMenuPolicy(Qt.CustomContextMenu)
        self.log.customContextMenuRequested.connect(self._chat_menu)
        self.input = ChatInput(); self.input.setFixedHeight(90)
        self.input.setPlaceholderText("Describe what to design or change…  (Ctrl+Enter to send; Ctrl+V pastes "
                                      "screenshots / images)")
        self.input.images.connect(self.add_attachments)
        self.attachments = []
        self.attach_bar = QScrollArea(); self.attach_bar.setFixedHeight(96); self.attach_bar.setWidgetResizable(True)
        self.attach_bar.setFrameShape(QFrame.NoFrame); self.attach_bar.setVisible(False)
        self.attach_bar.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tools_row, row = QHBoxLayout(), QHBoxLayout()          # two short rows keep the chat able to go narrow
        self.img_btn = QPushButton("Image…")
        self.img_btn.setToolTip("Attach a picture for the AI (or paste one with Ctrl+V, or drop it on the message box)")
        self.img_btn.clicked.connect(self.pick_images)
        self.send_btn = QPushButton("Send"); self.stop_btn = QPushButton("Stop"); self.stop_btn.setEnabled(False)
        self.est_lbl = QLabel("")
        self.est_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)     # never holds the chat wide
        self.send_btn.clicked.connect(self.send); self.stop_btn.clicked.connect(self.stop)
        self.new_btn = QPushButton("New chat")
        self.new_btn.setToolTip("Start a fresh conversation (the design and the old chat stay saved)")
        self.new_btn.clicked.connect(self.new_chat)
        self.undo_btn = QPushButton("Undo AI edit")
        self.undo_btn.setToolTip("Put the design back to how it was before the last AI edit, import or BOM edit (Ctrl+Z)")
        self.undo_btn.clicked.connect(self.undo)
        for b in (self.img_btn, self.undo_btn, self.new_btn):
            tools_row.addWidget(b)
        tools_row.addStretch(1)
        row.addWidget(self.est_lbl, 1); row.addWidget(self.stop_btn); row.addWidget(self.send_btn)
        cl.addWidget(self.log, 1); cl.addLayout(tools_row); cl.addWidget(self.attach_bar); cl.addWidget(self.input)
        cl.addLayout(row)
        self.input.textChanged.connect(self.update_estimate)
        sc = QAction(self); sc.setShortcut(QKeySequence("Ctrl+Return")); sc.triggered.connect(self.send)
        self.input.addAction(sc)
        split.addWidget(chat)

        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(True)
        self.svg = SvgPreview()
        self.tabs.addTab(self.svg, "Preview")
        bomw = QWidget(); bl = QVBoxLayout(bomw); bl.setContentsMargins(0, 0, 0, 0)
        self.bom_editor = self._make_bom_editor()
        self.prog = QProgressBar(); self.prog.setVisible(False)
        self.bom_total = QLabel("")
        bl.addWidget(self.bom_editor, 1); bl.addWidget(self.prog); bl.addWidget(self.bom_total)
        # pages with long button rows scroll sideways instead of holding the right side wide
        self.tabs.addTab(_hscroll(bomw), "BOM (live)")
        self.nets = QTreeWidget(); self.nets.setHeaderLabels(["Net / pin", "Max V (worst case)"])
        self.nets.headerItem().setToolTip(1, "Highest voltage the net reaches (to GND), worst case: supply tolerance, "
                                             "surges, resonant peaks. Used for capacitor and resistor ratings. "
                                             "Double-click to set.")
        self.nets.itemClicked.connect(lambda it, _c: self.refresh(highlight=it.text(0) if it.parent() is None else it.parent().text(0)))
        self.nets.itemDoubleClicked.connect(self._edit_net_voltage)
        self.tabs.addTab(self.nets, "Nets")
        self.tabs.addTab(_hscroll(self._libs_tab()), "Libraries")
        self.md_tab = _hscroll(self._md_tab())
        self.tabs.addTab(self.md_tab, "Schematic MD")
        self.spec_view = QPlainTextEdit(); self.spec_view.setReadOnly(True)
        self.tabs.addTab(self.spec_view, "design.json")
        self.tabs.setMinimumWidth(240)
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 3)
        saved = self.qs.value("chat_split")
        if saved is not None:
            split.restoreState(saved)
        split.splitterMoved.connect(lambda *_: self.qs.setValue("chat_split", split.saveState()))

        outer = QSplitter(Qt.Vertical)
        outer.addWidget(split)
        self.problems = QListWidget(); self.problems.setMaximumHeight(170)
        outer.addWidget(self.problems)
        self.setCentralWidget(outer)
        self.cost_lbl = QLabel("no project"); self.statusBar().addPermanentWidget(self.cost_lbl)

    def _libs_tab(self) -> QWidget:
        w = QWidget(); l = QVBoxLayout(w)
        self.lib_summary = QLabel("")
        self.lib_summary.setWordWrap(True)
        l.addWidget(self.lib_summary)
        self.lib_table = QTableWidget(0, 4)
        self.lib_table.setHorizontalHeaderLabels(["Library", "Problem", "Status", "File"])
        self.lib_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.lib_table.horizontalHeader().setStretchLastSection(True)
        l.addWidget(self.lib_table, 1)
        row = QHBoxLayout()
        for label, fn in [("Add library folder…", self.add_lib_folder), ("Import library zip…", self.import_zip),
                          ("Test convert one", lambda: self.convert_libs(test=True)),
                          ("Convert all binary with Eagle 7.4", lambda: self.convert_libs(test=False)),
                          ("Repair damaged XML", self.repair_libs), ("Rescan libraries", self.rescan_libs)]:
            b = QPushButton(label); b.clicked.connect(fn); row.addWidget(b)
        row.addStretch(1)
        l.addLayout(row)
        self.lib_prog = QProgressBar(); self.lib_prog.setVisible(False); l.addWidget(self.lib_prog)
        return w

    # ------------------------------------------------------------ Markdown document
    def _md_tab(self) -> QWidget:
        from PySide6.QtWidgets import QTextBrowser
        w = QWidget(); l = QVBoxLayout(w); l.setContentsMargins(0, 0, 0, 0)
        self.md_banner = QLabel(""); self.md_banner.setWordWrap(True); self.md_banner.setTextFormat(Qt.RichText)
        l.addWidget(self.md_banner)
        row = QHBoxLayout()
        self.md_raw = QCheckBox("Show raw Markdown"); self.md_raw.toggled.connect(lambda _b: self.refresh_md())
        row.addWidget(self.md_raw)
        self.md_find = QLineEdit(); self.md_find.setPlaceholderText("Find… (Enter)")
        self.md_find.returnPressed.connect(lambda: (self.md_view.find(self.md_find.text()) or
                                                    (self.md_view.moveCursor(self.md_view.textCursor().MoveOperation.Start),
                                                     self.md_view.find(self.md_find.text()))))
        row.addWidget(self.md_find, 1)
        for label, fn in (("Open MD file…", self.open_md_file),
                          ("Copy", lambda: QApplication.clipboard().setText(self._md_text_now())),
                          ("Open file", self.open_md), ("Import edits from file", self.import_md_edits),
                          ("Overwrite with design", self.overwrite_md)):
            b = QPushButton(label); b.clicked.connect(fn); row.addWidget(b)
            if label.startswith("Import"):
                self.md_import_btn = b
            if label.startswith("Overwrite"):
                self.md_over_btn = b
        l.addLayout(row)
        self.md_view = QTextBrowser(); self.md_view.setOpenExternalLinks(True)
        self.md_view.setStyleSheet("QTextBrowser{font-size:10.5pt;}")
        l.addWidget(self.md_view, 1)
        return w

    def _md_text_now(self) -> str:
        p = self.project
        if not p:
            return ""
        f = p.md_path()
        return f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""

    def refresh_md(self):
        p = self.project
        if not p or not hasattr(self, "md_view"):
            return
        from .md_writer import digest
        f = p.md_path()
        rec = p.state().get("md", {})
        text = self._md_text_now()
        outside = bool(text and rec.get("digest") and digest(text) != rec["digest"])
        p.md_conflict = outside and text != (p.md_text() if p.design.parts else text)
        if p.md_needs_choice:
            self.md_banner.setText("No Schematic MD linked yet - use <b>Open MD file…</b> to link your existing one, "
                                   "or <b>Overwrite with design</b> to create a new one.")
            self.md_banner.setStyleSheet("padding:6px; border-radius:4px; background:#2b3138; color:#f2f4f8;")
        elif not f.exists():
            self.md_banner.setText("No Markdown document yet - it is written as soon as the design has parts.")
            self.md_banner.setStyleSheet("padding:6px; border-radius:4px; background:#2b3138; color:#f2f4f8;")
        elif p.md_conflict:
            self.md_banner.setText(f"<b>{html.escape(f.name)} was edited outside the studio.</b> The design is not "
                                   "changed and the file is not overwritten until you choose: <b>Import edits from "
                                   "file</b> (updates the design) or <b>Overwrite with design</b>.")
            self.md_banner.setStyleSheet("padding:6px; border-radius:4px; background:#7a2e2e; color:#ffffff;")
        else:
            import time as _t
            self.md_banner.setText(f"{html.escape(f.name)} - matches the design (BOM, parts, nets). Rewritten "
                                   f"automatically after every change. Last written "
                                   f"{_t.strftime('%H:%M:%S', _t.localtime(f.stat().st_mtime))}.")
            self.md_banner.setStyleSheet("padding:6px; border-radius:4px; background:#1f5e32; color:#ffffff;")
        self.md_import_btn.setEnabled(f.exists()); self.md_over_btn.setEnabled(bool(p.design.parts))
        bar = self.md_view.verticalScrollBar().value()
        if self.md_raw.isChecked():
            self.md_view.setPlainText(text)
        else:
            self.md_view.setMarkdown(text)
        self.md_view.verticalScrollBar().setValue(bar)

    def _pick_and_adopt_md(self) -> bool:
        p = self.project
        f, _ = _dlg_open(self, "Open an MD file for this project", str(p.folder), "Markdown (*.md)")
        return bool(f) and self._adopt_md(Path(f))

    def _adopt_md(self, f: Path) -> bool:
        p = self.project
        try:
            msg = p.adopt_md(f)
        except Exception as exc:
            QMessageBox.warning(self, APP, f"Could not use {f.name}: {exc}")
            return False
        self.agent.note(f"Schematic MD linked: {f.name} (kept its prose, backup {f.name}.orig)")
        self.log.append(f"<b>Schematic MD linked</b> {html.escape(f.name)} - kept its title, summary, citations and "
                        f"sections; rewritten from the design and kept up to date (backup {html.escape(f.name)}.orig)")
        self.statusBar().showMessage(msg, 8000)
        self._watch_exports()
        return True

    def load_project_files(self, need_design: bool):
        """Find the .sch and the Schematic MD in the project folder and load both: the .sch becomes the design
        (pins exactly as in Eagle), the MD is linked to it (its prose kept, kept up to date). No .sch: the MD
        builds the design. Asks only when there is more than one candidate."""
        p = self.project
        need_md = p.md_needs_choice or need_design
        schs = p.sch_candidates() if need_design else []
        mds = p.md_candidates() if need_md else []
        if not schs and not mds:
            if p.md_needs_choice:
                self.statusBar().showMessage(p.create_md(), 8000)
                self._watch_exports()
            return
        sch = p.best_pick(schs)
        md = p.best_pick(mds, sch)
        if len(schs) > 1 or len(mds) > 1:
            dlg = ProjectFilesDialog(p.folder, schs, sch, mds, md, need_design, self)
            if not dlg.exec():
                return
            sch, md = dlg.choice()
        if sch is not None:
            if md is not None:
                p.md_needs_choice = True          # hold the MD back until it is linked below (no _studio.md copy)
            self.do_import(sch)
        if md is None:
            if p.md_needs_choice and p.design.parts:
                self.statusBar().showMessage(p.create_md(), 8000)
                self._watch_exports()
        elif p.design.parts:
            self._adopt_md(md)
        else:
            self.do_import(md)                    # no .sch (or it failed): the MD builds the design
        self.refresh()

    def open_md_file(self):
        """Open an older MD: link it as this project's Schematic MD, or import it into the design."""
        p = self.project
        if not p:
            return
        if not p.design.parts:
            f, _ = _dlg_open(self, "Open an MD schematic", str(p.folder), "Markdown (*.md)")
            if f:
                self.do_import(Path(f))       # builds the design from it; the file stays the project's MD
                self._watch_exports()
            return
        box = QMessageBox(self)
        box.setWindowTitle(APP)
        box.setText("What should the MD file be used for?\n\n• Link: it becomes this project's Schematic MD. The "
                    "design stays as it is; the file is rewritten from the design (its title, summary, citations and "
                    "sections are kept, a .orig backup is saved) and kept up to date.\n\n• Import: the parts and nets "
                    "in the file replace the design (undo available).")
        link = box.addButton("Link to this project", QMessageBox.AcceptRole)
        imp = box.addButton("Import into the design", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is link:
            self._pick_and_adopt_md()
        elif box.clickedButton() is imp:
            f, _ = _dlg_open(self, "Import an MD schematic", str(p.folder), "Markdown (*.md)")
            if f:
                self.agent.snapshot("before_md_import")
                self.do_import(Path(f))
                self._watch_exports()

    def open_md(self):
        f = self.project.md_path() if self.project else None
        if f and f.exists() and hasattr(os, "startfile"):
            os.startfile(str(f))

    def overwrite_md(self):
        if not self.project:
            return
        if self.project.md_conflict and QMessageBox.question(
                self, APP, "Replace the edited file with the document generated from the design? "
                           "Your edits in the file are lost.") != QMessageBox.Yes:
            return
        if self.project.md_needs_choice:
            self.statusBar().showMessage(self.project.create_md(), 6000)
        else:
            self.statusBar().showMessage(self.project.sync_md(force=True), 6000)
        self._watch_exports()

    def import_md_edits(self):
        if not self.project:
            return
        f = self.project.md_path()
        if QMessageBox.question(self, APP, f"Import {f.name} into the design? The parts and nets in the file replace "
                                           "the design's (undo is available).") == QMessageBox.Yes:
            from .md_writer import digest
            self.agent.snapshot("before_md_import")
            # the file's content is now the accepted baseline; the next design change rewrites it in studio format
            self.project.save_state(md={"path": f.name, "digest": digest(self._md_text_now())})
            self.project.md_conflict = False
            self.do_import(f)

    # ------------------------------------------------------------ live BOM
    def _make_bom_editor(self, parent=None):
        return BomEditor(lambda: self.project, self._bom_edit, lambda: self.worker is not None,
                         {"price": self.price, "export": self.export_jlc, "ai_find": self.ai_find_lcsc,
                          "paste": self.paste_lcsc, "memory": self.show_jlc_memory}, parent)

    def _edit_net_voltage(self, it, _col=0):
        if it.parent() is not None or not self.project or self.worker is not None:
            return
        net = it.text(0)
        cur = self.project.design.net_volts.get(net)
        txt, ok = QInputDialog.getText(self, "Net voltage", f"Highest voltage net {net} reaches (to GND), worst "
                                       f"case - e.g. 14.4 for a car's 12 V, the peak of a resonant tank.\n"
                                       f"Empty = guess from the name.", text="" if cur is None else f"{cur:g}")
        if not ok:
            return
        txt = txt.strip().upper().rstrip("V").strip()
        try:
            val = float(txt) if txt else None
        except ValueError:
            QMessageBox.warning(self, APP, f"'{txt}' is not a number of volts.")
            return
        self._bom_edit(f"Nets tab: {net} max voltage = {'(from the name)' if val is None else f'{val:g} V'}")
        dispatch(self.project, "set_net_voltage", {"nets": {net: val}})
        self.refresh()

    def _bom_edit(self, text: str):
        if self.agent:
            self.agent.snapshot("bom_edit")
            self.agent.note(text)

    def show_bom_window(self):
        if self.bom_window is None:
            self.bom_window = QDialog(self, Qt.Window)
            self.bom_window.setWindowTitle(f"BOM (live) - {APP}")
            self.bom_window.resize(1200, 600)
            l = QVBoxLayout(self.bom_window)
            self.bom_window.editor = self._make_bom_editor(self.bom_window)
            l.addWidget(self.bom_window.editor)
        if self.project:
            self.bom_window.setWindowTitle(f"BOM (live) - {self.project.folder.name}")
        self.bom_window.editor.refresh()
        self.bom_window.show(); self.bom_window.raise_(); self.bom_window.activateWindow()

    def refresh_bom(self):
        self.bom_editor.refresh()
        if self.bom_window is not None:
            self.bom_window.editor.refresh()

    def _design_changed(self):
        self.refresh()

    def _watch_exports(self):
        """Watch <project>/jlc and the exported files so outside edits (Excel) show up immediately."""
        if not self.project:
            return
        old = self.watcher.files() + self.watcher.directories()
        if old:
            self.watcher.removePaths(old)
        jd = self.project.folder / "jlc"
        paths = [str(self.project.design_path)] if self.project.design_path.exists() else []
        if jd.is_dir():
            paths.append(str(jd))
            paths += [str(f) for f in jd.glob("*.xlsx")]
        for brd in self.project.folder.glob("*.brd"):
            paths.append(str(brd))
        md = self.project.md_path()
        if md.exists():
            paths.append(str(md))
        if paths:
            self.watcher.addPaths(paths)
        self.refresh_bom()
        self.refresh_md()

    # ------------------------------------------------------------- Eagle link
    def _stamp(self):
        try:
            return (eagle_link.bridge_dir() / "stamp.txt").read_text().strip()
        except OSError:
            return ""

    def _eagle_synced(self):
        s = self._stamp()
        if not s or s == self._link_stamp:
            return
        self._link_stamp = s
        kind, st, _age = eagle_link.state()
        if kind == "board":
            from .board import read_state
            try:
                b = read_state(st)
                msg = f"Eagle link: board {Path(b.path).name} synced - {len(b.elements)} parts"
            except Exception as exc:
                msg = f"Eagle link: board synced (could not read: {exc})"
        elif kind == "sch":
            msg = "Eagle link: schematic synced"
        else:
            return
        if eagle_link.pending():
            msg += " - commands are queued for the other editor" if kind else ""
        self.statusBar().showMessage(msg, 10000)
        if self.agent:
            self.agent.note(msg)

    def eagle_link_dialog(self):
        ulp = eagle_link.install()
        kind, st, age = eagle_link.state()
        dlg = QDialog(self); dlg.setWindowTitle("Eagle link"); dlg.resize(760, 360)
        l = QVBoxLayout(dlg)
        l.addWidget(QLabel(
            "<b>One key in Eagle 7.4 keeps the studio in sync.</b><br>"
            "Paste this once into Eagle's command line (board or schematic editor) and press Enter. "
            "From then on, F12 sends what's open in Eagle to the studio and runs anything the studio queued "
            "(e.g. a placement). Everything it does can be undone in Eagle."))
        cmd = QLineEdit(eagle_link.assign_command()); cmd.setReadOnly(True); cmd.setFont(QFont("Consolas", 10))
        row = QHBoxLayout(); row.addWidget(cmd, 1)
        cp = QPushButton("Copy"); cp.clicked.connect(lambda: (QApplication.clipboard().setText(cmd.text()),
                                                              self.statusBar().showMessage("Copied - paste it in Eagle", 5000)))
        row.addWidget(cp); l.addLayout(row)
        last = "never" if kind is None else f"{'board' if kind == 'board' else 'schematic'}, {int(age // 60)} min ago"
        pend = eagle_link.pending()
        info = QLabel(f"Link script: {ulp}<br>Last sync from Eagle: {last}<br>"
                      + ("<b>Commands are queued</b> - press F12 in Eagle to run them." if pend else "Nothing queued."))
        info.setWordWrap(True); l.addWidget(info)
        row2 = QHBoxLayout()
        cb = QPushButton("Cancel queued commands"); cb.setEnabled(bool(pend))
        cb.clicked.connect(lambda: (eagle_link.cancel(), cb.setEnabled(False), info.setText(info.text().replace(
            "<b>Commands are queued</b> - press F12 in Eagle to run them.", "Nothing queued."))))
        of = QPushButton("Open link folder")
        of.clicked.connect(lambda: os.startfile(str(eagle_link.bridge_dir())) if hasattr(os, "startfile") else None)
        close = QPushButton("Close"); close.clicked.connect(dlg.accept)
        row2.addWidget(cb); row2.addWidget(of); row2.addStretch(1); row2.addWidget(close)
        l.addLayout(row2)
        dlg.exec()

    def review_placement(self, board, res, live):
        s = res.summary()
        txt = (f"Moves {s['moved']} parts, keeps {s['fixed']}. Ratsnest {s['ratsnest_mm'][0]} → {s['ratsnest_mm'][1]} mm. "
               "Red = top, blue = bottom, dashed = not moved, yellow = connections.")
        if s["warnings"] != "none":
            txt += "<br><b style='color:#ffb454'>" + "<br>".join(html.escape(w) for w in s["warnings"]) + "</b>"
        return self._review("Proposed placement", placement.preview_svg(board, res), txt, board, live)

    def review_route(self, board, res, live):
        s = res.summary()
        rl = self.project.state().get("design_rules", {}).get("name", "") if self.project else ""
        txt = (f"Routed {s['routed']} nets, {s['vias']} vias, {s['trace_length_mm']} mm of trace "
               f"({s['seconds']} s, grid {s['grid_mm']} mm). Rules: {html.escape(rl or 'JLC standard')}. "
               "Red = top copper, blue = bottom, gold = vias.")
        if s["failed"] != "none":
            txt += "<br><b style='color:#ffb454'>Not routed: " + html.escape(", ".join(f"{k} ({v})" for k, v in s["failed"].items())) + "</b>"
        txt += ("<br><b style='color:#7ee787'>DRC: clean</b>" if s["drc"] == "clean" else
                "<br><b style='color:#ff8a80'>DRC: " + "<br>".join(html.escape(x) for x in s["drc"][:8]) + "</b>")
        if res.pour:
            txt += f"<br>Copper pour: {html.escape(res.pour['net'])} on " + ", ".join(
                "bottom" if l == 16 else "top" for l in res.pour["layers"])
        return self._review("Proposed routing", router.preview_svg(board, res), txt, board, live)

    def _review(self, title, svg_bytes, txt, board, live):
        dlg = QDialog(self); dlg.setWindowTitle(title); dlg.resize(1100, 760)
        l = QVBoxLayout(dlg)
        svg = SvgPreview()
        svg.load(svg_bytes); l.addWidget(svg, 1)
        lab = QLabel(txt); lab.setWordWrap(True); l.addWidget(lab)
        choice = [None]
        row = QHBoxLayout()
        be = QPushButton("Send to Eagle (then press F12 in the board)")
        be.clicked.connect(lambda: (choice.__setitem__(0, "eagle"), dlg.accept()))
        bb = QPushButton(f"Overwrite {Path(board.path).name if board.path else 'the .brd'} (Eagle closed, backup kept)")
        bb.setEnabled(bool(board.path) and Path(board.path).exists())
        def write():
            if live and QMessageBox.question(dlg, APP, "This board was synced from an open Eagle. Close it in Eagle "
                                             "first (without saving) or your edit there will overwrite this. "
                                             "Write the file now?") != QMessageBox.Yes:
                return
            choice[0] = "brd"; dlg.accept()
        bb.clicked.connect(write)
        cn = QPushButton("Don't apply"); cn.clicked.connect(dlg.reject)
        row.addWidget(be); row.addWidget(bb); row.addStretch(1); row.addWidget(cn)
        l.addLayout(row)
        dlg.exec()
        dlg.deleteLater()
        return choice[0]

    def rules_dialog(self):
        if not self.project:
            return
        from dataclasses import asdict, fields as dfields
        cur = dispatch(self.project, "design_rules", {})["rules"]
        dlg = QDialog(self); dlg.setWindowTitle("Design rules (placement, routing, checks)"); dlg.resize(560, 720)
        f = QFormLayout(dlg)
        preset = QComboBox()
        names = {k: v.name for k, v in rules_mod.PRESETS.items()}
        preset.addItem("(current)", "")
        for k, n in names.items():
            preset.addItem(n, k)
        f.addRow("Preset", preset)
        edits = {}
        labels = {"trace": "Default trace width (mm)", "clearance": "Clearance (mm)", "min_trace": "Min trace (mm)",
                  "min_clearance": "Min clearance (mm)", "smd_clearance": "SMD pad to pad (mm)",
                  "via_drill": "Via drill (mm)", "via_dia": "Via diameter (mm)", "min_drill": "Min drill (mm)",
                  "min_annular": "Min annular ring (mm)", "hole_to_hole": "Hole to hole (mm)",
                  "edge_clearance": "Copper to board edge (mm)", "copper_oz": "Copper (oz)", "layers": "Layers"}
        for k, lab in labels.items():
            e = QDoubleSpinBox(); e.setDecimals(3); e.setRange(0, 50); e.setSingleStep(0.01)
            e.setValue(float(cur[k])); edits[k] = e; f.addRow(lab, e)
        name = QLineEdit(cur["name"]); f.addRow("Name", name)
        prob = QLabel(""); prob.setWordWrap(True); f.addRow(prob)

        def load(d):
            for k, e in edits.items():
                e.setValue(float(d[k]))
            name.setText(d["name"])
        def check():
            d = dict(cur, **{k: e.value() for k, e in edits.items()}, name=name.text())
            d["layers"] = int(d["layers"])
            pr = rules_mod.DesignRules(**{k: v for k, v in d.items() if k in {x.name for x in dfields(rules_mod.DesignRules)}}).check()
            prob.setText("<b style='color:#ff8a80'>" + "<br>".join(pr) + "</b>" if pr else
                         "<span style='color:#7ee787'>Within the fab's limits.</span>")
            return d
        for e in edits.values():
            e.valueChanged.connect(check)
        preset.currentIndexChanged.connect(lambda _i: preset.currentData() and (load(asdict(rules_mod.PRESETS[preset.currentData()])), check()))
        row = QHBoxLayout()
        ld = QPushButton("Load Eagle .dru…")
        def load_dru():
            fn, _ = _dlg_open(dlg, "Eagle design rules", str(self.project.folder), "Design rules (*.dru)")
            if fn:
                try:
                    load(asdict(rules_mod.read_dru(Path(fn)))); check()
                except (OSError, ValueError) as exc:
                    QMessageBox.warning(dlg, APP, str(exc))
        ld.clicked.connect(load_dru)
        ex = QPushButton("Export .dru for Eagle…")
        def export():
            fn, _ = _dlg_save(dlg, "Save Eagle design rules", str(self.project.folder / "design_rules.dru"),
                                                "Design rules (*.dru)")
            if fn:
                d = check()
                rules_mod.write_dru(rules_mod.DesignRules(**{k: v for k, v in d.items() if k in {x.name for x in dfields(rules_mod.DesignRules)}}), Path(fn))
                self.statusBar().showMessage(f"Saved {fn} - in Eagle: DRC → Load", 8000)
        ex.clicked.connect(export)
        row.addWidget(ld); row.addWidget(ex); f.addRow(row)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); f.addRow(bb)
        check()
        if dlg.exec() == QDialog.Accepted:
            d = check()
            ov = {k: v for k, v in d.items() if k in labels or k == "name"}
            ov["layers"] = int(ov["layers"])
            res = dispatch(self.project, "design_rules", {"overrides": ov})
            if "error" in res:
                QMessageBox.warning(self, APP, res["error"])
            else:
                self.agent.note(f"Design rules set: {d['name']} (trace {d['trace']} / clearance {d['clearance']} mm, "
                                f"via {d['via_drill']}/{d['via_dia']} mm, edge {d['edge_clearance']} mm)")
                self.statusBar().showMessage("Design rules saved for this project (also design_rules.dru)", 8000)

    def about(self):
        box = QMessageBox(self); box.setWindowTitle(APP)
        box.setText(f"<b>{APP} {__version__}</b><br>AI-driven Eagle 7.4 schematic design.<br><br>"
                    f"Python {sys.version.split()[0]}<br>See CHANGELOG.md for what changed.")
        cr = box.addButton("Crash report…", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        box.exec()
        if box.clickedButton() is cr:
            self.crash_report()

    def _died_in_dialog(self, line: str):
        global DIALOG_MODE
        box = QMessageBox(self); box.setWindowTitle(APP); box.setIcon(QMessageBox.Warning)
        box.setText("Last time the studio closed while a file dialog was open - it most likely crashed or froze "
                    "inside it.\n\n" + line.split(" OPEN ", 1)[-1]
                    + "\n\nThe crash report reads Windows' own records: a crash names the DLL that crashed, a "
                      "freeze ('stopped interacting with Windows') means an Explorer add-on hung the dialog. "
                      "If other programs' Open dialogs freeze too, the add-on is the cause, not the studio.")
        rep = box.addButton("Show crash report", QMessageBox.AcceptRole)
        safe = box.addButton("Use crash-safe Windows dialogs", QMessageBox.ActionRole)
        builtin = box.addButton("Use built-in dialogs", QMessageBox.ActionRole)
        box.addButton("Keep as is", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() in (safe, builtin):
            DIALOG_MODE = "safe" if box.clickedButton() is safe else "qt"
            self.qs.setValue("dialog_mode", DIALOG_MODE)
        elif box.clickedButton() is rep:
            self.crash_report()

    def crash_report(self):
        from . import crashdiag
        dlg = QDialog(self); dlg.setWindowTitle("Crash report"); dlg.resize(980, 640)
        l = QVBoxLayout(dlg)
        l.addWidget(QLabel("Reading Windows' crash records (Event Viewer -> Application, event 1000) and the studio's "
                           "logs. Nothing is sent anywhere - copy it to me if you want help."))
        txt = QPlainTextEdit(crashdiag.local_report(DIALOG_MODE) + "\n\n== Windows crash records ==\nreading (max ~30 s)…")
        txt.setReadOnly(True); txt.setFont(QFont("Consolas", 9)); l.addWidget(txt, 1)
        row = QHBoxLayout()
        cp = QPushButton("Copy all"); cp.clicked.connect(lambda: QApplication.clipboard().setText(txt.toPlainText()))
        ev = QPushButton("Open Event Viewer")
        ev.clicked.connect(lambda: subprocess.Popen(["eventvwr.msc"], shell=True) if os.name == "nt" else None)
        fl = QPushButton("Open log folder")
        fl.clicked.connect(lambda: os.startfile(str(crashdiag.ROOT)) if hasattr(os, "startfile") else None)
        close = QPushButton("Close"); close.clicked.connect(dlg.accept)
        for b in (cp, ev, fl):
            row.addWidget(b)
        row.addStretch(1); row.addWidget(close); l.addLayout(row)
        w = FuncWorker(lambda _prog: crashdiag.windows_report())
        w.result.connect(lambda t: txt.setPlainText(crashdiag.local_report(DIALOG_MODE) + "\n\n" + t))
        w.failed.connect(lambda e: txt.setPlainText(f"could not build the report: {e}"))
        self._keep(w); w.start()
        dlg.exec()

    # ------------------------------------------------------------ recent projects
    def recent(self) -> list:
        return [p for p in (self.qs.value("recent", [], type=list) or []) if p]

    def _push_recent(self, folder: Path):
        lst = [str(folder)] + [p for p in self.recent() if os.path.normcase(p) != os.path.normcase(str(folder))]
        self.qs.setValue("recent", lst[:MAX_RECENT])

    def _fill_project_menu(self):
        m = self.proj_menu
        m.clear()
        m.addAction("New / Open project folder…", self.pick_project)
        m.addSeparator()
        rec = self.recent()
        if not rec:
            a = m.addAction("(no recent projects)"); a.setEnabled(False)
        for i, p in enumerate(rec):
            exists = Path(p).is_dir()
            label = f"{i + 1}  {Path(p).name}   —   {p}" + ("" if exists else "   (missing)")
            a = m.addAction(label, lambda p=p: self.open_project(Path(p)))
            a.setEnabled(exists)
            if self.project and os.path.normcase(str(self.project.folder)) == os.path.normcase(p):
                a.setCheckable(True); a.setChecked(True)
        if rec:
            m.addSeparator()
            m.addAction("Clear recent list", lambda: self.qs.setValue("recent", []))

    # ------------------------------------------------------------ projects
    def pick_project(self):
        d = _dlg_dir(self, "Project folder (created if needed)")
        if d:
            self.open_project(Path(d))

    def open_project(self, folder: Path):
        if self.worker is not None:
            QMessageBox.information(self, APP, "Wait for the AI to finish (or press Stop) before switching projects.")
            return
        self.project = Project(folder, self.settings, anthropic_client())
        self.project.ask_script = self.approver
        self.project.ask_lbr_path = self.lbr_asker
        self.project.ask_placement = self.placement_asker
        self.project.ask_route = self.route_asker
        self.project.guard.ask = self.overwrite_asker
        (folder / "datasheets").mkdir(exist_ok=True)
        self.agent = Agent(self.project)
        self.qs.setValue("project", str(folder))
        self._push_recent(folder)
        self.project.costs.listeners.append(lambda _t: self.signals.changed.emit())
        self.project.listeners.append(self.signals.changed.emit)
        self.setWindowTitle(f"{APP} {__version__} - {folder.name}")
        self.render_chat()
        self._linked_notices()
        if not any(get_key(b) for b in BRANDS):
            self.log.append("<i>No AI key yet - open Settings.</i>")
        self.refresh()
        self._watch_exports()
        if self.bom_window is not None:
            self.bom_window.setWindowTitle(f"BOM (live) - {folder.name}")
        self.scan_libs(after=self.after_libraries)

    def render_chat(self):
        """Show the saved chat of this project (since the last New chat)."""
        self.log.clear()
        entries = self.agent.visible_log() if self.agent else []
        older = len(self.agent.log) - len(entries) if self.agent else 0
        if older:
            self.log.append(f"<i>{older} earlier chat entries saved in .eagleai/chat.jsonl</i>")
        for e in entries:
            t = e.get("t")
            if t == "user":
                pics = "".join(self._thumb_html(path=Path(x) if Path(x).is_absolute() else self.project.folder / x)
                               for x in e.get("images") or [])
                self.log.append(f"<p><b>You → {html.escape(e.get('to', ''))}:</b> {html.escape(e['text'][:2000])}"
                                f"{('<br>' + pics) if pics else ''}</p>")
            elif t == "assistant":
                self.log.append(f"<p><b>{html.escape(e.get('by', 'AI'))}:</b> {html.escape(e.get('text', ''))}</p>")
            elif t == "review":
                self.log.append(f"<p style='background:#2a2f3a'><b>REVIEW — {html.escape(e.get('by', ''))}:</b><br>"
                                f"{html.escape(e.get('text', '')).replace(chr(10), '<br>')}</p>")
            elif t == "tool":
                self.log.append(f"<span style='color:#b9c2d0'>⚙ {html.escape(e['name'])} → {html.escape(str(e.get('result', '')))}</span>")
            elif t == "note":
                self.log.append(f"<i>{html.escape(e['text'][:500])}</i>")

    def scan_libs(self, after=None, rescan=False):
        p = self.project
        self.lib_prog.setVisible(True)
        self.statusBar().showMessage("Scanning libraries (all sub-folders)…")
        w = FuncWorker(lambda prog: p.rescan(prog) if rescan else p.load_libraries(prog))
        w.progress.connect(lambda i, n, s: (self.lib_prog.setMaximum(n), self.lib_prog.setValue(i),
                                            self.statusBar().showMessage(f"Scanning {i}/{n}: {s}")))
        w.result.connect(lambda msg: self._scanned(p, msg, after))
        w.failed.connect(lambda e: (self.lib_prog.setVisible(False), self.statusBar().showMessage(f"library scan failed: {e}")))
        self._keep(w); w.start()

    def _scanned(self, p, msg, after):
        if p is not self.project:          # a newer project was opened meanwhile
            return
        self.lib_prog.setVisible(False)
        self.statusBar().showMessage(msg, 10000)
        self.refresh_libs(); self.refresh()
        if not p.index._files:
            self.log.append("<p style='color:#ffb454'><b>No libraries found.</b> Open the Libraries tab and use "
                            "<b>Add library folder…</b> to pick the folder that holds your .lbr files "
                            "(sub-folders are scanned too).</p>")
        if after:
            after()

    def after_libraries(self):
        """Load what is already in the folder: design.json, else the .sch and the Schematic MD found there."""
        p = self.project
        bad = [s for s in p.index.problem_libs() if not s.get("covered_by")]
        if bad:
            self.log.append(f"<i>{len(bad)} libraries could not be read - see the Libraries tab to convert them.</i>")
        changed = p.externally_changed_sch()
        if p.design.parts:
            if p.md_needs_choice:
                self.load_project_files(need_design=False)
            if changed:
                names = ", ".join(f.name for f in changed)
                if QMessageBox.question(self, APP, f"{names} changed in Eagle since the app last wrote it.\n\n"
                                        f"Re-import it? (Replaces the app's copy of the design; undo is available.)") == QMessageBox.Yes:
                    self.agent.snapshot("before_reimport")
                    self.do_import(changed[0])
            return
        if not p.design_candidates():
            if not self.agent.visible_log():
                self.log.append("<i>Empty project. Describe what you want to build, or use Import.</i>")
            return
        self.load_project_files(need_design=True)

    def import_dialog(self):
        if not self.project:
            return
        f, _ = _dlg_open(self, "Import design", str(self.project.folder),
                                           "Eagle schematic or Markdown (*.sch *.md)")
        if f:
            if self.project.design.parts and QMessageBox.question(
                    self, APP, "Replace the current design? (Undo is available.)") != QMessageBox.Yes:
                return
            self.agent.snapshot("before_import")
            self.do_import(Path(f))

    def do_import(self, path: Path):
        p = self.project
        try:
            if path.suffix.lower() == ".sch":
                report = p.import_sch(path)
                self.log.append("<b>Imported</b> " + "<br>".join(html.escape(r) for r in report))
                self.agent.note(f"Design imported from {path.name}: " + "; ".join(report))
                self.refresh(); self.refresh_libs()
                return
            parsed = p.import_md(path)
        except Exception as exc:
            traceback.print_exc()
            QMessageBox.warning(self, APP, f"Import failed: {exc}")
            return
        n_nets = len(parsed["nets"])
        done, left = p.auto_place_bom(parsed["bom"])
        msg = (f"Read {path.name}: {len(parsed['bom'])} BOM rows, {n_nets} nets (connections parsed in code).\n"
               f"Placed locally, no AI cost: {len(done)} rows.")
        if done:
            msg += "\n  " + "\n  ".join(done[:30])
        if parsed["conflicts"]:
            msg += "\n\nWARNING - the markdown joins differently named nets:\n" + "\n".join(parsed["conflicts"][:5])
        self.log.append("<b>Markdown import</b><br>" + html.escape(msg).replace("\n", "<br>"))
        self.agent.note(msg)
        if not left:
            from .tools import tool_import_apply_nets
            res = tool_import_apply_nets(p)
            self.log.append(f"Connected {res.get('connected_pins', 0)} pins locally."
                            + (f"<br><b>{len(res['errors'])} pins didn't match the library parts</b> - send "
                               f"“fix the import pin errors” to let the AI sort them out." if res.get("errors") else ""))
            if res.get("errors"):
                p.pending_import = p.pending_import or parsed
            self.refresh()
            return
        self.refresh()
        rows = [{k: v for k, v in r.items() if k != "description" or len(v) < 80} for r in left]
        if not get_key(self.settings.brand):
            self.log.append(f"<i>{len(left)} rows need a judgement call. Add an AI key in Settings, then send: "
                            f"“continue the markdown import”.</i>")
            return
        prompt = ("Markdown import. These BOM rows could not be matched automatically (reason in 'why'). For each: "
                  "search_library (part number, then value+package, then description), then add them with add_parts "
                  "using EXACTLY the given refs and value/mpn. If nothing installed fits, tell me which rows need a "
                  "datasheet. Then call import_apply_nets and fix the reported pin mismatches.\n\nRows:\n"
                  + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
                  + ("\n\nRefs used in connections but missing from the BOM: " + ", ".join(parsed["refs_not_in_bom"])
                     if parsed["refs_not_in_bom"] else "")
                  + ("\n\nConflicts in the markdown (tell me, don't guess): " + "; ".join(parsed["conflicts"])
                     if parsed["conflicts"] else ""))
        est = self.agent.estimate_next(prompt, model=self.settings.model) * max(2, len(left) // 3)
        if QMessageBox.question(self, APP, f"{len(done)} rows were placed locally. {len(left)} need the AI to choose "
                                f"the library part.\n\nRough cost: ${est:.2f} with {self.settings.model}. "
                                f"Go ahead?") == QMessageBox.Yes:
            self.input.setPlainText(prompt)
            self.send()

    def _keep(self, w):
        self._bg.append(w)
        w.finished.connect(lambda: self._bg.remove(w) if w in self._bg else None)

    def show_settings(self):
        dlg = SettingsDialog(self.settings, self.models["claude"], self)
        if dlg.exec():
            dlg.apply()
            s = self.settings
            for k, v in (("dmodel", s.datasheet_model), ("budget", s.budget_usd or 0),
                         ("qty", s.build_qty), ("fee", s.extended_fee), ("libs", s.lib_dirs), ("csv", s.jlc_csv),
                         ("eagle", s.eagle_exe), ("converted", s.converted_dir), ("style", s.passive_style),
                         ("pages", s.datasheet_pages), ("lbr_dir", s.lbr_dir), ("lbr_ask", s.lbr_ask),
                         ("dialog_mode", DIALOG_MODE)):
                self.qs.setValue(k, v)
            if self.project:
                self.open_project(self.project.folder)     # rescans libraries with the new folders
            self.check_models()

    # -------------------------------------------------------------- AI brand / models
    def _fill_models(self):
        b = self.settings.brand
        self.model_box.blockSignals(True)
        self.model_box.clear()
        self.model_box.addItems(self.models.get(b, []))
        self.model_box.setCurrentText(self.settings.model)
        self.model_box.blockSignals(False)

    def _brand_picked(self, _i):
        b = self.brand_box.currentData()
        self.settings.brand = b
        self.qs.setValue("brand", b)
        self.settings.model = self.qs.value(f"model_{b}", "", type=str) or DEFAULT_MODEL[b] or \
            (self.models.get(b) or [""])[0]
        self._fill_models()
        if not get_key(b):
            self.statusBar().showMessage(f"No {BRANDS[b][0]} API key - add it in Settings", 8000)
        self.update_estimate()

    def _model_picked(self, m: str):
        if m:
            self.settings.model = m.strip()
            self.qs.setValue(f"model_{self.settings.brand}", self.settings.model)
            self.update_estimate()

    def check_models(self):
        if not any(get_key(b) for b in BRANDS):
            return
        w = FuncWorker(lambda _p: fetch_all())
        w.result.connect(self._models_found)
        w.failed.connect(lambda e: self.statusBar().showMessage(f"model check skipped: {e}", 6000))
        self._keep(w); w.start()

    def _models_found(self, found: dict):
        lines, errors = [], []
        priced = self.project.costs if self.project else None
        for brand, lst in found.items():
            if lst and "error" in lst[0]:
                errors.append(f"{BRANDS[brand][0]}: {lst[0]['error'][:120]}")
                continue
            known = self.qs.value(f"known_models_{brand}", [], type=list) or []
            ids = [m["id"] for m in lst]
            first_time = not known
            new = diff_new(lst, known, brand)
            self.models[brand] = list(dict.fromkeys(ids + self.models.get(brand, [])))
            self.qs.setValue(f"known_models_{brand}", list(dict.fromkeys(known + ids)))
            if first_time:
                continue              # first check for this brand: just fill the list, no popup
            for m in new:
                tag = "" if (priced and priced.price_for(m["id"], 1)) else \
                    "  - no price on file: costs show $0 until you add it to pricing.json"
                lines.append(f"• {BRANDS[brand][0]}: {m['name']}  ({m['id']}){tag}")
        self._fill_models()
        if errors:
            self.statusBar().showMessage("Model check: " + " | ".join(errors), 12000)
        if lines:
            QMessageBox.information(self, "New AI models found",
                                    "These models are now available and were added to the model lists:\n\n"
                                    + "\n".join(lines))

    # ---------------------------------------------------------------- refresh
    def refresh(self, highlight: str | None = None):
        p = self.project
        if not p:
            return
        self.spec_view.setPlainText(p.design.to_json())
        try:
            if p.design.parts:
                self.svg.load(render_svg(p.design, p.index, highlight_net=highlight).encode("utf-8"))
            else:
                self.svg.load(b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 4'><rect width='10' height='4' fill='#1b1d21'/></svg>")
        except Exception as exc:
            self.statusBar().showMessage(f"preview error: {exc}")
        from .ratings import net_voltage
        self.nets.clear()
        for net, pins in sorted(p.design.nets.items()):
            v, src = net_voltage(p.design, net)
            vt = "" if v is None else f"{v:g} V" + ("" if src == "set" else " (from the name)")
            it = QTreeWidgetItem([net, vt]); self.nets.addTopLevelItem(it)
            if src == "name":
                it.setForeground(1, QBrush(QColor("#aab3c0")))
            for pin in pins:
                it.addChild(QTreeWidgetItem([pin]))
        self.nets.resizeColumnToContents(0)
        self.refresh_bom()
        self.refresh_md()
        self.run_checks()
        self.update_cost()

    def refresh_libs(self):
        p = self.project
        if not p:
            return
        idx = p.index
        probs = idx.problem_libs()
        counts = {}
        for f in idx._files:
            for d in idx.dirs:
                try:
                    Path(f).relative_to(d)
                    counts[d] = counts.get(d, 0) + 1
                    break
                except ValueError:
                    pass
        dirs = "<br>".join(html.escape(str(d)) + ("  <b>(missing)</b>" if not d.is_dir() else f"  - {counts.get(d, 0)} .lbr files")
                           for d in idx.dirs)
        self.lib_summary.setText(f"<b>{idx.stats()}</b><br>Scanned (with all sub-folders):<br>{dirs}")
        self.lib_table.setRowCount(len(probs))
        kinds = {"binary": "old binary format (Eagle 5 or older)", "xml-damaged": "damaged XML",
                 "empty": "empty file", "unreadable": "can't open"}
        for i, s in enumerate(probs):
            status = (f"OK - XML copy used: {s['covered_by']}" if s.get("covered_by")
                      else "needs converting" if s["kind"] == "binary"
                      else "try Repair" if s["kind"] == "xml-damaged" else "unusable")
            for j, v in enumerate([s["name"], kinds.get(s["kind"], s["kind"]), status, s["path"]]):
                self.lib_table.setItem(i, j, QTableWidgetItem(v))
        self.lib_table.resizeColumnsToContents()

    def run_checks(self):
        p = self.project
        if not p:
            return
        from .ratings import problems as rating_problems
        self.problems.clear()
        for pr in p.design.validate(p.index):
            self.problems.addItem(f"{'✖' if pr.level == 'error' else '⚠'} {pr.where}: {pr.msg}")
        for lvl, where, msg in rating_problems(p.design, p.index):
            self.problems.addItem(f"{'⚠' if lvl == 'warn' else 'ℹ'} {where}: {msg}")
        if self.problems.count() == 0:
            self.problems.addItem("✔ no problems found")

    def update_cost(self):
        if self.project:
            self.cost_lbl.setText(self.project.costs.summary())

    def update_estimate(self):
        if self.agent and (self.input.toPlainText().strip() or self.attachments):
            try:
                est = self.agent.estimate_next(self.input.toPlainText(), model=self.settings.model,
                                               images=self.attachments)
                n = len(self.attachments)
                self.est_lbl.setText(f"≈ ${est:.4f} with {self.settings.model}"
                                     + (f" (incl. {n} image{'s' if n > 1 else ''})" if n else ""))
            except Exception:
                self.est_lbl.setText("")
        else:
            self.est_lbl.setText("")

    # ------------------------------------------------------- library repair / import
    def _bad_libs(self, kind: str):
        if not self.project:
            return []
        rows = {i.row() for i in self.lib_table.selectedIndexes()}
        probs = self.project.index.problem_libs()
        pick = [probs[r] for r in sorted(rows)] if rows else probs
        return [s for s in pick if s["kind"] == kind and not s.get("covered_by")]

    def convert_libs(self, test: bool):
        libs = self._bad_libs("binary")
        if not libs:
            QMessageBox.information(self, APP, "No binary libraries need converting (already-covered ones are skipped).")
            return
        exe = self.settings.eagle_exe
        if not exe or not Path(exe).exists():
            QMessageBox.warning(self, APP, "Set the path to eagle.exe (Eagle 7.4) in Settings first.")
            return
        if test:
            libs = libs[:1]
        out = self.project.converted_dir
        if not test and QMessageBox.question(
                self, APP, f"Convert {len(libs)} libraries with Eagle 7.4?\n\nEagle opens and closes once per file. "
                f"Your originals are not changed; XML copies go to:\n{out}\n\nIf Eagle shows an 'update' dialog, "
                f"click OK/Yes.") != QMessageBox.Yes:
            return
        self.lib_prog.setVisible(True)
        w = FuncWorker(lambda prog: eagle_cli.convert_many(exe, [Path(s["path"]) for s in libs], out, prog, guard=self.project.guard))
        w.progress.connect(lambda i, n, s: (self.lib_prog.setMaximum(n), self.lib_prog.setValue(i),
                                            self.statusBar().showMessage(f"Converting {i}/{n}: {s}")))
        w.result.connect(self._converted)
        w.failed.connect(lambda e: (self.lib_prog.setVisible(False), QMessageBox.warning(self, APP, e)))
        self._keep(w); w.start()

    def _converted(self, res: list):
        self.lib_prog.setVisible(False)
        ok = [r for r in res if r["ok"]]
        bad = [r for r in res if not r["ok"]]
        text = f"Converted {len(ok)} of {len(res)}."
        if bad:
            text += "\n\nFailed:\n" + "\n".join(f"{Path(r['src']).name}: {r['msg']}" for r in bad[:15])
            text += ("\n\nManual fallback: in Eagle 7.4 Control Panel open the library (it converts it), then "
                     "File > Save as… into the converted-libraries folder.")
        QMessageBox.information(self, APP, text)
        if ok:
            self.rescan_libs()

    def repair_libs(self):
        libs = self._bad_libs("xml-damaged")
        if not libs:
            QMessageBox.information(self, APP, "No damaged-XML libraries to repair.")
            return
        fixed, failed = [], []
        for s in libs:
            r = repair_xml_lbr(Path(s["path"]), self.project.converted_dir, guard=self.project.guard)
            (fixed if r else failed).append(s["name"])
        QMessageBox.information(self, APP, f"Repaired {len(fixed)}: {', '.join(fixed) or '-'}\n\n"
                                f"Not repairable in code (truncated or not Eagle XML): {', '.join(failed) or '-'}")
        if fixed:
            self.rescan_libs()

    def add_lib_folder(self):
        d = _dlg_dir(self, "Choose a folder that contains .lbr files (sub-folders are scanned too)")
        if not d:
            return
        if d not in self.settings.lib_dirs:
            self.settings.lib_dirs.append(d)
            self.qs.setValue("libs", self.settings.lib_dirs)
        if self.project:
            self.project.settings.lib_dirs = self.settings.lib_dirs
            self.project.index.dirs = [Path(x) for x in [self.project.folder, self.project.converted_dir] + self.settings.lib_dirs]
            self.scan_libs(rescan=True)

    def choose_lbr_path(self, name: str, default) -> "Path | None":
        """Save dialog for a new library part. Picking an existing .lbr adds the part to it."""
        f, _ = _dlg_save(
            self, f"Save library part '{name}' - pick a new file, or an existing .lbr to add it to",
            str(default), "Eagle library (*.lbr)", options=QFileDialog.DontConfirmOverwrite)
        if not f:
            return None
        path = Path(f) if f.lower().endswith(".lbr") else Path(f + ".lbr")
        self.ensure_lib_dir(path.parent)
        return path

    def ensure_lib_dir(self, folder: Path):
        """Make sure a folder holding new libraries is scanned in future sessions."""
        folder = Path(folder)
        dirs = [self.project.folder] if self.project else []
        dirs += [Path(d) for d in self.settings.lib_dirs] + ([Path(self.settings.lbr_dir)] if self.settings.lbr_dir else [])
        if any(str(folder).lower().startswith(str(d).lower()) for d in dirs):
            return
        self.settings.lib_dirs.append(str(folder))
        self.qs.setValue("libs", self.settings.lib_dirs)
        if self.project:
            self.project.index.dirs.append(folder)

    def rescan_libs(self):
        if self.project:
            self.scan_libs(rescan=True)

    def import_zip(self):
        if not self.project:
            QMessageBox.information(self, APP, "Open a project first.")
            return
        f, _ = _dlg_open(self, "Library zip (Ultra Librarian, SnapEDA, …)",
                                           str(Path.home() / "Downloads"), "Zip files (*.zip)")
        if not f:
            return
        try:
            info = ul_import.inspect_zip(Path(f))
        except Exception as exc:
            QMessageBox.warning(self, APP, f"Can't read the zip: {exc}")
            return
        base = Path(self.settings.lib_dirs[0]) if self.settings.lib_dirs else Path.home() / "Documents" / "EagleAI" / "libraries"
        dlg = DestDialog(info, base / "UltraLibrarian" / info["part"], self)
        if not dlg.exec():
            return
        dest = Path(dlg.dest.text())
        if (dest / f"{dlg.part.text()}.lbr").exists() and QMessageBox.question(
                self, APP, f"{dest} already has {dlg.part.text()}.lbr - replace it?") != QMessageBox.Yes:
            return
        if not any(str(dest).lower().startswith(str(Path(d)).lower()) for d in self.settings.lib_dirs + [str(self.project.folder)]):
            if QMessageBox.question(self, APP, "That folder is not inside one of your library folders, so the app won't "
                                    "find the part.\n\nAdd its parent folder to the library folders?") == QMessageBox.Yes:
                self.settings.lib_dirs.append(str(dest.parent))
                self.qs.setValue("libs", self.settings.lib_dirs)
                self.project.index.dirs.append(dest.parent)
        try:
            res = ul_import.import_zip(Path(f), dest, self.settings.eagle_exe, dlg.part.text(),
                                        guard=self.project.guard if self.project else None)
        except Exception as exc:
            traceback.print_exc()
            QMessageBox.warning(self, APP, f"Import failed: {exc}")
            return
        msg = f"Imported {res['part']} into {res['dest']}\n\n" + "\n".join("• " + r for r in res["report"])
        QMessageBox.information(self, APP, msg)
        self.agent.note(msg)
        self.log.append("<b>Library imported</b><br>" + html.escape(msg).replace("\n", "<br>"))
        self.rescan_libs()

    # -------------------------------------------------------------- chatting
    # ------------------------------------------------------------ image attachments
    def add_attachments(self, atts: list):
        from .images import MAX_IMAGES
        room = MAX_IMAGES - len(self.attachments)
        if len(atts) > room:
            self.statusBar().showMessage(f"At most {MAX_IMAGES} images per message - "
                                         f"{len(atts) - max(room, 0)} not attached", 8000)
        self.attachments += atts[:max(room, 0)]
        self._show_attachments()

    def pick_images(self):
        from . import images as im
        f, _ = _dlg_open(self, "Attach an image for the AI", str(self.project.folder) if self.project else "",
                         "Images (" + " ".join("*" + e for e in im.IMAGE_EXT) + ")")
        if f:
            a = im.from_file(f)
            if a is None:
                QMessageBox.warning(self, APP, f"Could not read {Path(f).name} as an image.")
                return
            self.add_attachments([a])

    def _remove_attachment(self, i: int):
        if 0 <= i < len(self.attachments):
            del self.attachments[i]
        self._show_attachments()

    def _show_attachments(self):
        from PySide6.QtGui import QPixmap
        from .images import tokens
        strip = QWidget(); h = QHBoxLayout(strip); h.setContentsMargins(0, 0, 0, 0)
        for i, a in enumerate(self.attachments):
            cell = QWidget(); v = QVBoxLayout(cell); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(1)
            pm = QPixmap(); pm.loadFromData(a["data"])
            thumb = QLabel(); thumb.setPixmap(pm.scaled(120, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            thumb.setToolTip(f"{a['name']} - {a['w']}x{a['h']} px, about {tokens(a):,} tokens")
            x = QToolButton(); x.setText("✕ remove"); x.clicked.connect(lambda _c=False, n=i: self._remove_attachment(n))
            v.addWidget(thumb); v.addWidget(x)
            h.addWidget(cell)
        h.addStretch(1)
        self.attach_bar.setWidget(strip)
        self.attach_bar.setVisible(bool(self.attachments))
        self.update_estimate()

    def _thumb_html(self, att=None, path=None) -> str:
        """An attached picture as a small inline image in the chat (from memory, or from the saved file)."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QImage, QTextDocument
        img = QImage.fromData(att["data"]) if att is not None else QImage(str(path))
        if img.isNull():
            return f"<i>[image {html.escape(Path(str(path)).name) if path else ''} not found]</i> "
        self._thumb_n = getattr(self, "_thumb_n", 0) + 1
        url = QUrl(f"chatimg://{self._thumb_n}")
        self.log.document().addResource(QTextDocument.ImageResource, url, img)
        hgt = min(110, img.height())
        wid = max(1, min(260, round(img.width() * hgt / max(img.height(), 1))))
        hgt = round(img.height() * wid / max(img.width(), 1))
        return f"<img src='{url.toString()}' width='{wid}' height='{hgt}'> "

    def _start_worker(self, text: str, brand: str, model: str, review: bool, images=None):
        if not get_key(brand):
            QMessageBox.information(self, APP, f"Add your {BRANDS[brand][0]} API key in Settings first.")
            return False
        self._linked_notices()
        label = "REVIEW request to" if review else "You →"
        pics = "".join(self._thumb_html(a) for a in images or [])
        self.log.append(f"<p><b>{label} {html.escape(BRANDS[brand][0])} {html.escape(model)}:</b> "
                        f"{html.escape(text[:2000])}{('<br>' + pics) if pics else ''}</p>"
                        f"<b>{html.escape(BRANDS[brand][0])}:</b> ")
        self.send_btn.setEnabled(False); self.review_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        w = AgentWorker(self.agent, text, brand, model, review, images)
        w.text.connect(self._append_text)
        w.tool.connect(lambda n, d: self.log.append(f"<br><span style='color:#b9c2d0'>⚙ {html.escape(n)}  {html.escape(d)}</span><br>"))
        w.design_changed.connect(self.refresh)
        w.done.connect(self._finished); w.failed.connect(self._finished_err)
        self.worker = w; w.start()
        self.refresh_bom()          # locks BOM editing while the AI works
        return True

    def send(self):
        if not self.agent or self.worker is not None:
            return
        text = self.input.toPlainText().strip()
        if not text and not self.attachments:
            return
        text = text or "Look at the attached image(s)."
        if self._start_worker(text, self.settings.brand, self.settings.model, False, list(self.attachments)):
            self.input.clear()
            self.attachments = []
            self._show_attachments()

    def review(self):
        if not self.agent or self.worker is not None:
            return
        brand = self.review_box.currentData()
        model = self.qs.value(f"model_{brand}", "", type=str) or DEFAULT_MODEL[brand] or (self.models.get(brand) or [""])[0]
        if not model:
            QMessageBox.information(self, APP, f"Pick a {BRANDS[brand][0]} model first (choose it in the AI box once).")
            return
        extra = self.input.toPlainText().strip()
        text = "Double-check the current design and the work done so far." + (f" Focus: {extra}" if extra else "")
        if self._start_worker(text, brand, model, True, list(self.attachments)):
            self.input.clear()
            self.attachments = []
            self._show_attachments()

    def _append_text(self, s: str):
        # a separate cursor: moving the view's own cursor would drop whatever you have selected to copy
        sb = self.log.verticalScrollBar()
        follow = sb.value() >= sb.maximum() - 4
        c = QTextCursor(self.log.document())
        c.movePosition(QTextCursor.End)
        c.insertText(s, QTextCharFormat())
        if follow:
            sb.setValue(sb.maximum())

    def _chat_menu(self, pos):
        m = self.log.createStandardContextMenu(pos)
        m.addSeparator()
        m.addAction("Copy all chat", lambda: QApplication.clipboard().setText(self.log.toPlainText()))
        m.exec(self.log.viewport().mapToGlobal(pos))
        m.deleteLater()

    def _finished(self, res):
        self.worker = None
        self.refresh_bom()
        self.send_btn.setEnabled(True); self.review_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        if res and "[" in res[-80:]:
            self.log.append(f"<i>{html.escape(res[res.rfind('['):])}</i>")
        self.refresh()

    def _finished_err(self, err: str):
        self.log.append(f"<p style='color:#ff8a80'>Error: {html.escape(err)}</p>")
        self._finished("")

    def new_chat(self):
        if self.agent and self.worker is None:
            self.agent.new_chat()
            self.render_chat()
            self.log.append("<i>New chat - the design is kept; the old chat stays in .eagleai/chat.jsonl.</i>")
            self.update_estimate()

    def stop(self):
        if self.agent:
            self.agent.cancel = True

    def show_linked(self):
        if not self.project:
            return
        from .linked_view import LinkedDesignsDialog
        dlg = LinkedDesignsDialog(self.project, _dlg_dir, _dlg_open, _dlg_save, self)
        dlg.exec()
        for m in dlg.messages:
            self.agent.note(m)
            self.log.append(f"<i>{html.escape(m)}</i>")
        self.update_estimate()

    def _linked_notices(self):
        """Tell the user (and, through the chat log, every AI) what changed on the linked boards."""
        from . import linked
        try:
            msgs = linked.changes(self.project)
        except Exception as exc:
            msgs = [f"Linked designs could not be checked: {exc}"]
        for m in msgs:
            self.agent.note(m)
            self.log.append(f"<p style='color:#ffb454'><i>{html.escape(m)}</i></p>")

    def show_calculators(self):
        from .calc_view import CalculatorsDialog
        if self.calc_window is None:
            self.calc_window = CalculatorsDialog(self)
        self.calc_window.show(); self.calc_window.raise_(); self.calc_window.activateWindow()

    def undo(self):
        if self.worker is not None:
            self.statusBar().showMessage("Wait for the AI to finish (or press Stop) before undoing", 5000)
            return
        if self.agent:
            n = self.agent.undo_last()
            self.statusBar().showMessage(f"restored {n}" if n else "nothing to undo", 4000)
            if n:
                self.agent.note(f"User pressed Undo: design restored to {n}")
            self.refresh()

    # -------------------------------------------------------------- actions
    def gen_sch(self):
        if not self.project:
            return
        from .tools import tool_generate_sch
        res = tool_generate_sch(self.project)
        if "error" in res:
            QMessageBox.warning(self, APP, res["error"] + "\n" + "\n".join(res.get("problems", [])))
            return
        self.last_sch = res["path"]
        self.statusBar().showMessage(f"wrote {res['path']} ({res['parts']} parts, {res['nets']} nets)", 8000)
        msgs = res["errors"] + ([res["note"]] if res.get("note") else [])
        if msgs:
            QMessageBox.information(self, APP, "\n".join(msgs))

    def open_eagle(self):
        path = getattr(self, "last_sch", None)
        if not path or not Path(path).exists():
            self.gen_sch()
            path = getattr(self, "last_sch", None)
        if not path:
            return
        exe = self.settings.eagle_exe
        try:
            if exe and Path(exe).exists():
                subprocess.Popen([exe, path])
            else:
                os.startfile(path)          # whatever Windows has registered for .sch
        except AttributeError:
            subprocess.Popen(["xdg-open", path])

    def add_datasheet(self):
        if not self.project:
            return
        f, _ = _dlg_open(self, "Datasheet PDF", "", "PDF (*.pdf)")
        if not f:
            return
        dest = self.project.folder / "datasheets" / Path(f).name
        if Path(f) != dest:
            shutil.copy(f, dest)
        out = self.choose_lbr_path(dest.stem, self.project.lbr_out(dest.stem) if not self.settings.lbr_ask
                                   else (Path(self.settings.lbr_dir) if self.settings.lbr_dir else self.project.generated_libs)
                                   / f"{dest.stem}.lbr")
        if out is None:
            return
        self.input.setPlainText(f"Build a library part from datasheets/{dest.name} (save_to: {out}) and add it to the "
                                f"design. Tell me everything I must verify about the footprint.")
        self.send()

    def price(self):
        if not self.project or self.worker is not None:
            return
        self.tabs.setCurrentIndex(1)
        self.prog.setVisible(True); self.bom_total.setText("looking up JLC…")
        p = self.project
        w = FuncWorker(lambda prog: price_bom(p, assign=True, progress=prog))
        w.progress.connect(lambda i, n, s: (self.prog.setMaximum(n), self.prog.setValue(i)))
        w.result.connect(self._priced)
        w.failed.connect(lambda e: (self.prog.setVisible(False), self.bom_total.setText(f"JLC lookup failed: {e}")))
        self._keep(w); w.start()

    # ------------------------------------------------------- JLC part numbers with AI web search
    def _missing_lines(self):
        p = self.project
        from .ratings import analyze, group_text
        ratings = analyze(p.design, p.index)
        out = []
        for g in p.design.bom():
            if g["dnp"] or g["lcsc"]:
                continue
            ref0 = g["refs"].split(",")[0].strip()
            dev, _ = p.design.device_for(p.index, ref0)
            pkg = dev.package if dev else ""
            rt = group_text(ratings, [r.strip() for r in g["refs"].split(",")], True)
            out.append({"refs": g["refs"], "qty": g["qty"] * p.settings.build_qty, "value": g["value"],
                        "library_part": f"{g['library']}/{g['deviceset']}/{g['device']}", "package": pkg,
                        "size": jlc.footprint_size(pkg) or "", "mpn": g["mpn"], "note": g["note"],
                        "min_rating": "" if rt == "?" else rt})
        return out

    def ai_find_lcsc(self):
        p = self.project
        if not p or self.worker is not None:
            return
        key = get_key("claude")
        if not key:
            QMessageBox.information(self, APP, "This uses Claude with web search - add a Claude API key in Settings.")
            return
        lines = self._missing_lines()
        if not lines:
            QMessageBox.information(self, APP, "Every line already has an LCSC number (or is DNP).")
            return
        model = self.settings.model if self.settings.brand == "claude" else DEFAULT_MODEL["claude"]
        est = p.costs.estimate(model, 15000 + 9000 * len(lines), 3000) + 3 * len(lines) * jlc_ai.WEB_SEARCH_USD
        if QMessageBox.question(self, APP, f"Claude ({model}) will search jlcpcb.com / lcsc.com for {len(lines)} BOM "
                                f"line(s) without an LCSC number:\n\n" + "\n".join(f"  {l['refs']}: {l['value']}" for l in lines[:15])
                                + f"\n\nEstimated cost ≈ ${est:.2f}. Every number it finds is re-checked at JLC before "
                                f"you apply it. Continue?") != QMessageBox.Yes:
            return
        by_refs = {l["refs"]: l for l in lines}
        need = {l["refs"]: l["qty"] for l in lines}
        self.prog.setVisible(True); self.prog.setMaximum(0)
        self.bom_total.setText("Claude is searching JLC/LCSC…")

        def job(prog):
            parts, usage, searches = jlc_ai.ask_claude(lines, key, model, p.settings.build_qty, p.settings.extended_fee,
                                                       on_status=lambda s: prog(0, 0, s))
            p.costs.record(model, usage, label="jlc web search", extra_usd=searches * jlc_ai.WEB_SEARCH_USD)
            prog(0, 0, "checking the numbers at JLC…")
            return jlc_ai.verify(parts, p.jlc, by_refs, need), searches

        w = FuncWorker(job)
        w.progress.connect(lambda i, n, s: self.bom_total.setText("Claude: " + s))
        w.result.connect(lambda r: self._ai_found(p, *r))
        w.failed.connect(lambda e: (self.prog.setVisible(False), self.bom_total.setText(""),
                                    QMessageBox.warning(self, APP, f"AI part search failed: {e}")))
        self._keep(w); w.start()

    def _ai_found(self, p, rows, searches):
        self.prog.setVisible(False); self.prog.setMaximum(100); self.bom_total.setText("")
        if p is not self.project:
            return
        dlg = QDialog(self); dlg.setWindowTitle(f"JLC part numbers found by Claude ({searches} web searches)")
        dlg.resize(1250, 520)
        l = QVBoxLayout(dlg)
        l.addWidget(QLabel("Ticked rows are applied. Rows with a problem are unticked - read the Check column first."))
        cols = ["Use", "Refs", "LCSC", "Part", "Package", "Stock", "Unit $", "Basic", "Check", "Why"]
        t = QTableWidget(len(rows), len(cols)); t.setHorizontalHeaderLabels(cols)
        t.horizontalHeader().setStretchLastSection(True)
        for i, r in enumerate(rows):
            use = QTableWidgetItem()
            ok = bool(r.get("verified") and r.get("lcsc") and not r.get("problem"))
            use.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            use.setCheckState(Qt.Checked if ok else Qt.Unchecked)
            if not r.get("lcsc") or not r.get("verified"):
                use.setFlags(Qt.ItemIsEnabled)
            t.setItem(i, 0, use)
            check = ("OK - checked at JLC" if ok else r.get("problem") or "") if r.get("lcsc") else (r.get("problem") or "")
            vals = [r.get("refs", ""), r.get("lcsc", ""), f"{r.get('manufacturer', '')} {r.get('mfr_part', '')}".strip(),
                    r.get("package", ""), str(r.get("stock", "") or ""),
                    f"{r['unit_price']:.4f}" if isinstance(r.get("unit_price"), (int, float)) else "",
                    "Basic" if r.get("basic") else ("Extended" if r.get("verified") else ""), check, r.get("why", "")]
            for j, v in enumerate(vals, 1):
                it = QTableWidgetItem(str(v)); it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                t.setItem(i, j, it)
        t.resizeColumnsToContents()
        l.addWidget(t, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Apply ticked")
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        chosen = {rows[i]["refs"]: rows[i]["lcsc"] for i in range(len(rows)) if t.item(i, 0).checkState() == Qt.Checked}
        self._apply_lcsc({r.strip().upper(): lc for refs, lc in chosen.items() for r in str(refs).split(",") if r.strip()},
                         "Claude web search")

    def _apply_lcsc(self, mapping: dict, source: str):
        p = self.project
        mapping = {r: lc for r, lc in mapping.items() if r in p.design.parts}
        if not mapping:
            return
        self.agent.snapshot("lcsc_" + source[:10])
        for r, lc in mapping.items():
            p.design.parts[r].lcsc = lc
        p.changed()
        self.agent.note(f"LCSC numbers set from {source}: " + ", ".join(f"{r}={lc}" for r, lc in sorted(mapping.items())))
        self.statusBar().showMessage(f"{len(mapping)} LCSC numbers applied - getting prices…", 8000)
        self.price()

    def show_jlc_memory(self):
        if not self.project:
            return
        mem = self.project.jlc_mem
        by = {}
        for k, v in mem.data.items():
            e = by.setdefault(v["lcsc"], dict(v, keys=[]))
            e["keys"].append(k.split(":", 1)[0] + " " + k.split(":", 1)[1].replace("|", " "))
            e["ts"] = max(e.get("ts", 0), v.get("ts", 0))
        rows = sorted(by.values(), key=lambda e: -e.get("ts", 0))
        dlg = QDialog(self); dlg.setWindowTitle(f"My JLC parts - remembered across projects ({len(rows)})")
        dlg.resize(1100, 500)
        l = QVBoxLayout(dlg)
        l.addWidget(QLabel(f"Used first by Fill missing and by the AI, after a fresh stock check at JLC.\nFile: {mem.path}"))
        cols = ["LCSC", "Part", "Package", "Basic", "Used for", "Last project", "Last used"]
        t = QTableWidget(len(rows), len(cols)); t.setHorizontalHeaderLabels(cols)
        t.setSelectionBehavior(QAbstractItemView.SelectRows); t.horizontalHeader().setStretchLastSection(True)
        import time as _t
        for i, e in enumerate(rows):
            for j, v in enumerate([e["lcsc"], e.get("mfr", ""), e.get("package", ""), "Basic" if e.get("basic") else "",
                                   "; ".join(e["keys"]), e.get("project", ""),
                                   _t.strftime("%Y-%m-%d", _t.localtime(e.get("ts", 0)))]):
                it = QTableWidgetItem(str(v)); it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable); t.setItem(i, j, it)
        t.resizeColumnsToContents()
        l.addWidget(t, 1)
        row = QHBoxLayout()
        fb = QPushButton("Forget selected")
        def forget():
            sel = sorted({ix.row() for ix in t.selectedIndexes()}, reverse=True)
            for r in sel:
                mem.forget(t.item(r, 0).text()); t.removeRow(r)
        fb.clicked.connect(forget)
        cb = QPushButton("Close"); cb.clicked.connect(dlg.accept)
        row.addWidget(fb); row.addStretch(1); row.addWidget(cb); l.addLayout(row)
        dlg.exec()

    def paste_lcsc(self):
        p = self.project
        if not p:
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "Paste LCSC numbers",
            "Paste Eagle ATTRIBUTE lines, a table, or lines like 'R1, R2: C22548' (e.g. from a Claude chat):")
        if not ok or not text.strip():
            return
        found = jlc_ai.parse_lcsc_list(text, p.design.parts.keys())
        if not found:
            QMessageBox.information(self, APP, "No reference + LCSC number pairs for parts in this design were found.")
            return
        changes = [f"{r}: {p.design.parts[r].lcsc or '(none)'} → {lc}" for r, lc in sorted(found.items())
                   if p.design.parts[r].lcsc != lc]
        if not changes:
            QMessageBox.information(self, APP, "All of those numbers are already in the BOM.")
            return
        if QMessageBox.question(self, APP, "Apply these LCSC numbers?\n\n" + "\n".join(changes[:40])
                                + "\n\nPrices are looked up afterwards; unknown numbers show 'not listed at JLC'.") == QMessageBox.Yes:
            self._apply_lcsc(found, "pasted list")

    def _priced(self, res: dict):
        self.prog.setVisible(False)
        self.bom_total.setText("")
        self.refresh()
        self.statusBar().showMessage(f"JLC prices updated: ≈ ${res['total_per_board']:.2f}/board at {res['build_qty']} "
                                     f"boards ({res['unpriced_lines']} lines unpriced"
                                     + (f", {res['from_memory']} from your remembered parts" if res.get('from_memory') else "")
                                     + ") - snapshot, not a quote", 12000)

    def export_jlc(self):
        if not self.project or self.worker is not None:
            return
        brds = sorted(self.project.folder.glob("*.brd"))
        dlg = JlcExportDialog(brds, self)
        if not brds:
            dlg.setWindowTitle("Export for JLCPCB assembly - no .brd in the project yet (place the parts in Eagle first)")
        if not dlg.exec():
            return
        brd, side, rot_mode, fill = dlg.values()
        if not brd.exists():
            QMessageBox.warning(self, APP, f"Board file not found: {brd}")
            return
        p = self.project
        offsets = Path.home() / "Documents" / "EagleAI" / "jlc_rotations.json"

        def job(prog):
            filled = None
            if fill and any(not s.lcsc for s in p.design.parts.values()):
                filled = price_bom(p, assign=True, progress=prog)
            return filled, bom_sync.export_jlc(p, brd, side, rot_mode, offsets)

        self.prog.setVisible(True); self.bom_total.setText("exporting for JLC…")
        w = FuncWorker(job)
        w.progress.connect(lambda i, n, s: (self.prog.setMaximum(n), self.prog.setValue(i)))
        w.result.connect(lambda r: self._jlc_done(*r))
        w.failed.connect(lambda e: (self.prog.setVisible(False), QMessageBox.warning(self, APP, f"Export failed: {e}")))
        self._keep(w); w.start()

    def _jlc_done(self, filled, res):
        self.prog.setVisible(False)
        self.bom_total.setText("")
        self._watch_exports()
        self.refresh()
        if filled:
            self._priced(filled)
        lines = [f"BOM: {res['bom_path']}", f"CPL: {res['cpl_path']}",
                 f"{len(res['cpl'])} parts placed, {len(res['bom'])} BOM lines. Board sides with parts: "
                 f"{', '.join(res['sides_present']) or '-'}"]
        if res["missing_lcsc"]:
            lines.append(f"\nNO LCSC NUMBER (red rows in the BOM): {', '.join(res['missing_lcsc'])}")
        if res["dnp"]:
            lines.append(f"Not placed (DNP): {', '.join(res['dnp'])}")
        if res["no_pads"]:
            lines.append(f"Skipped, no pads (logos, fiducials, holes): {', '.join(res['no_pads'])}")
        if res["not_on_board"]:
            lines.append(f"In the schematic but not on the board: {', '.join(res['not_on_board'])}")
        if res["warnings"]:
            lines.append("\n" + "\n".join(res["warnings"][:10]))
        lines.append("\nUpload BOM and CPL on the JLC assembly page and check the placement preview.")
        msg = "\n".join(lines)
        self.agent.note("JLC export: " + msg)
        QMessageBox.information(self, "JLC export", msg)


def apply_theme(app):
    """One consistent high-contrast dark theme (independent of the Windows light/dark setting)."""
    from PySide6.QtGui import QColor, QPalette
    app.setStyle("Fusion")
    p = QPalette()
    roles = {QPalette.Window: "#1e2127", QPalette.WindowText: "#f2f4f8", QPalette.Base: "#15171c",
             QPalette.AlternateBase: "#1e2229", QPalette.Text: "#f2f4f8", QPalette.Button: "#2c323c",
             QPalette.ButtonText: "#f2f4f8", QPalette.BrightText: "#ffffff", QPalette.ToolTipBase: "#2c323c",
             QPalette.ToolTipText: "#f2f4f8", QPalette.Highlight: "#2f6fdb", QPalette.HighlightedText: "#ffffff",
             QPalette.Link: "#7fbcff", QPalette.PlaceholderText: "#9aa4b2"}
    for r, c in roles.items():
        p.setColor(r, QColor(c))
    for r in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, r, QColor("#8a93a1"))
    app.setPalette(p)
    app.setStyleSheet("QToolTip{color:#f2f4f8;background:#2c323c;border:1px solid #5b6577;}"
                      "QPushButton,QToolButton{padding:4px 10px;border:1px solid #4a5363;border-radius:4px;}"
                      "QPushButton:hover,QToolButton:hover{background:#38404d;}"
                      "QHeaderView::section{background:#2c323c;color:#f2f4f8;padding:4px;border:1px solid #3a4150;}"
                      "QTabBar::tab{padding:6px 12px;} QTabBar::tab:selected{background:#2f6fdb;color:#ffffff;}"
                      "QStatusBar{color:#f2f4f8;}"
                      "QSplitter::handle{background:#2c323c;} QSplitter::handle:hover{background:#2f6fdb;}")


def main():
    app = QApplication(sys.argv)
    app.setApplicationVersion(__version__)
    apply_theme(app)
    w = MainWindow(); w.show()
    code = app.exec()
    try:
        from . import crashdiag
        crashdiag.session_end()
    except Exception:
        pass
    # Leave without Python tearing down Qt objects one by one (PySide can crash on that at exit). On Windows,
    # TerminateProcess skips the DLL unload notifications too - with a background job still running (library
    # scan, crash report, AI call) those made Qt crash *while closing*, which looked like a crash in faulthandler.log.
    try:
        w.qs.sync()
        sys.stdout.flush(); sys.stderr.flush()
    finally:
        if os.name == "nt":
            try:
                terminate_self(code)
            except Exception:
                pass
        os._exit(code)


def terminate_self(code: int):
    """TerminateProcess on this process. The prototypes must be declared: without them ctypes passes the
    pseudo-handle (-1) as a 32-bit int, Windows rejects it, and the process carries on into a normal exit."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)      # private instance: don't alter ctypes.windll
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetCurrentProcess.argtypes = ()
    k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.TerminateProcess(k32.GetCurrentProcess(), int(code) & 0xFFFFFFFF)


if __name__ == "__main__":
    main()
