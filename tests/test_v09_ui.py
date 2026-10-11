"""Opening a folder loads the .sch AND links the MD; chat text stays selectable while the AI streams; preview
zoom / pan / fit; the chat/tabs splitter goes narrow and wide."""
import os, shutil, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

tmp = Path(tempfile.mkdtemp())
QSettings.setDefaultFormat(QSettings.IniFormat)                       # never touch the user's real settings
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp / "qs"))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
app = QApplication.instance() or QApplication([])

import test_engine as te
from eagleai import app as A
from eagleai.lbr_builder import build_lbr
from eagleai.tools import Project, Settings, tool_generate_sch

libdir = tmp / "lbr"
for s in (te.BUCK, te.SOT, te.RES):
    build_lbr(s, libdir)
st = Settings(lib_dirs=[str(libdir)], converted_dir=str(tmp / "converted"))
pa = Project(tmp / "src", st); pa.load_libraries()
d = pa.design
d.add_part(pa.index, "LDO5", "LDO5"); d.add_part(pa.index, "R", "R", None, "R1", "10k")
d.connect(pa.index, "VOUT", ["U1.OUT", "R1.1"]); d.connect(pa.index, "GND", ["U1.GND", "R1.2"])
SCH = Path(tool_generate_sch(pa, "board")["path"])
OLD_MD = ("# My LDO board\nFrom the old workflow\n\n- Battery powered\n\n## Datasheet Citations\n- U1: LDO datasheet\n\n"
          "## COMPLETE BILL OF MATERIALS\n| Ref | Qty | Part Number | Value | Description | Package |\n"
          "|-----|-----|-------------|-------|-------------|---------|\n| U1 | 1 | LDO5 | LDO5 | regulator | SOT-23-5 |\n")

w = A.MainWindow()
w.settings.lib_dirs = st.lib_dirs; w.settings.converted_dir = st.converted_dir


def open_and_wait(folder, until):
    w.open_project(folder)
    for _ in range(300):
        app.processEvents(); time.sleep(0.01)
        if until():
            break
    for _ in range(20):
        app.processEvents()


def test_sch_and_md_both_loaded():
    f = tmp / "one"; f.mkdir()
    shutil.copy(SCH, f / "board.sch")
    (f / "board.md").write_text(OLD_MD, encoding="utf-8")
    open_and_wait(f, lambda: w.project.state().get("md", {}).get("path"))
    p = w.project
    assert set(p.design.parts) == {"U1", "R1"}, p.design.parts                  # design from the .sch
    assert p.md_path() == f / "board.md", p.md_path()                           # the MD is linked ...
    t = (f / "board.md").read_text(encoding="utf-8")
    assert t.startswith("# My LDO board") and "Battery powered" in t and "### R1" in t   # ... prose kept, synced
    assert (f / "board.md.orig").exists() and not list(f.glob("*_studio.md"))


def test_several_files_ask_once_with_paired_defaults():
    f = tmp / "many"; (f / "old").mkdir(parents=True)
    shutil.copy(SCH, f / "v2.sch"); shutil.copy(SCH, f / "old" / "v1.sch")
    (f / "v2.md").write_text(OLD_MD, encoding="utf-8")
    (f / "notes.md").write_text(OLD_MD.replace("My LDO", "Other"), encoding="utf-8")
    os.utime(f / "notes.md", (time.time() + 60, time.time() + 60))       # newest, but not the pair of v2.sch
    seen = []
    orig = A.ProjectFilesDialog.exec
    A.ProjectFilesDialog.exec = lambda self: (seen.append(self.choice()), 1)[1]
    try:
        open_and_wait(f, lambda: w.project.state().get("md", {}).get("path"))
    finally:
        A.ProjectFilesDialog.exec = orig
    assert seen == [(f / "v2.sch", f / "v2.md")], seen
    assert set(w.project.design.parts) == {"U1", "R1"} and w.project.md_path() == f / "v2.md"


def test_existing_design_links_its_md():
    f = tmp / "one"                                    # reopen; forget the link: like an old project
    w.project.save_state(md={})
    open_and_wait(f, lambda: w.project.state().get("md", {}).get("path"))
    assert w.project.md_path() == f / "board.md" and not w.project.md_needs_choice


def test_chat_selection_survives_streaming():
    w.log.clear(); w.log.append("<p><b>Claude:</b> first answer to copy</p>"); w.log.append("<b>Claude:</b> ")
    text = w.log.toPlainText(); i = text.index("first answer")
    c = w.log.textCursor(); c.setPosition(i); c.setPosition(i + len("first answer to copy"), c.MoveMode.KeepAnchor)
    w.log.setTextCursor(c)
    sel = w.log.textCursor().selectedText()
    for chunk in ("more ", "text ", "streaming in"):
        w._append_text(chunk)
    assert w.log.textCursor().selectedText() == sel and sel, "selection lost while the AI was writing"
    w.log.copy()
    assert "first answer to copy" in QApplication.clipboard().text()
    assert w.log.toPlainText().endswith("more text streaming in")


def test_preview_zoom_pan_fit():
    w.resize(1400, 900); w.show(); w.tabs.setCurrentWidget(w.svg); app.processEvents()
    v = w.svg.view
    w.refresh(); app.processEvents()
    z0 = v.zoom()
    v.zoom_by(2.0); assert abs(v.zoom() / z0 - 2.0) < 0.01
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    ev = QWheelEvent(QPointF(100, 100), QPointF(v.viewport().mapToGlobal(QPoint(100, 100))), QPoint(0, 0),
                     QPoint(0, 120), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    z1 = v.zoom(); app.sendEvent(v.viewport(), ev)
    assert v.zoom() > z1, "wheel up should zoom in"
    z2 = v.zoom(); w.refresh(); app.processEvents()
    assert abs(v.zoom() - z2) < 1e-6, "an AI edit (refresh) must keep the zoom"
    v.fit(); assert abs(v.zoom() - z0) < 0.01 * z0


def test_splitter_narrow_and_wide():
    w.resize(1400, 900); app.processEvents()
    total = sum(w.split.sizes())
    w.split.setSizes([260, total - 260]); app.processEvents()
    assert w.split.sizes()[0] <= 300, w.split.sizes()
    w.split.setSizes([total - 260, 260]); app.processEvents()
    assert w.split.sizes()[0] >= total - 300, w.split.sizes()


if __name__ == "__main__":
    import traceback
    rc = 0
    try:
        for n, fn in list(globals().items()):
            if n.startswith("test_"):
                fn(); print("ok", n)
    except Exception:
        traceback.print_exc(); rc = 1
    sys.stdout.flush(); sys.stderr.flush()
    if os.name == "nt":                     # end like the app does: Qt teardown with live workers can crash
        A.terminate_self(rc)
    os._exit(rc)
