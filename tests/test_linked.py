"""Linked designs: a controller board and its display board, only one open at a time. The open one sees a summary of the other
in the AI's system prompt, can read its detail with linked_design (read only), is told when it changes, and both
share one system requirements file."""
import os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

tmp = Path(tempfile.mkdtemp())
QSettings.setDefaultFormat(QSettings.IniFormat)                       # never touch the user's real settings
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp / "qs"))
for n in ("question", "information", "warning"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.Yes))
app = QApplication.instance() or QApplication([])

from eagleai import linked
from eagleai.agent import Agent
from eagleai.model import Design, PartSpec
from eagleai.tools import Project, Settings, dispatch
from fakes import FakeProvider


class SysProvider(FakeProvider):
    def step(self, system, tools, native, on_text):
        self.seen_system = system
        return super().step(system, tools, native, on_text)


def make_project(folder: Path, name, parts, nets, summary):
    p = Project(folder, Settings(lib_dirs=[]))
    d = Design(name)
    for ref, (ds, val) in parts.items():
        d.parts[ref] = PartSpec(ref, "lib", ds, "", val)
    d.nets = nets
    d.doc = {"title": name, "summary": summary}
    p.design = d
    d.save(p.design_path)
    return p


root = tmp / "system"
MAIN = make_project(root / "Controller", "Controller board", {"U2": ("ATMEGA328P", "ATmega328P"), "C2": ("C-EU", "100n"),
                                                            "U1": ("LM7805", "LM7805"), "J1": ("JST_GH", "")},
                  {"+5V": ["U1.OUT", "U2.VCC"], "GND": ["U1.GND", "J1.2"], "+12V": ["U1.IN", "J1.1"],
                   "SDA": ["U2.SDA", "C2.1"]}, ["Reads the sensors and drives the display, I2C at 400 kHz"])
DISP = make_project(root / "Display Board" / "rev2", "Display board", {"L1": ("BLM18", "600R"), "C1": ("C-US", "22n")},
                  {"AC1": ["L1.A", "C1.1"], "AC2": ["L1.B", "C1.2"]},
                  ["Backlight filter: C1 sized for the MEASURED controller PWM frequency"])


def test_read_folder_sch_md():
    c = linked.load_companion(DISP.folder)
    assert c.source == "design.json" and c.name == "rev2" and c.parts["C1"]["value"] == "22n" and not c.error
    sch = tmp / "board.sch"
    sch.write_text("""<?xml version="1.0"?><eagle><drawing><schematic><parts>
<part name="R1" library="rcl" deviceset="R-US_" device="R0603" value="10k"><attribute name="MPN" value="RC0603"/></part>
<part name="U1" library="x" deviceset="NE555" device="D"/></parts><sheets><sheet><nets>
<net name="VCC"><segment><pinref part="R1" gate="G$1" pin="1"/><pinref part="U1" gate="G$1" pin="VCC"/></segment></net>
</nets></sheet></sheets></schematic></drawing></eagle>""", encoding="utf-8")
    c = linked.load_companion(sch)
    assert c.source == ".sch" and c.parts["R1"]["value"] == "10k" and c.parts["R1"]["mpn"] == "RC0603"
    assert c.nets["VCC"] == ["R1.1", "U1.VCC"]
    md = tmp / "display.md"
    md.write_text("# Display v4\n\nBacklight board\n\n- Ferrite approx. 600R\n\n## COMPLETE BILL OF MATERIALS\n\n"
                  "| Ref | Part Number | Value | Package |\n|---|---|---|---|\n| C1 | Generic | 22nF | 0805 |\n"
                  "| L1 | Generic | 6uH | coil |\n\n## Net summary\n\n- Net AC1: L1.1, C1.1\n", encoding="utf-8")
    c = linked.load_companion(md)
    assert c.source == ".md" and c.title == "Display v4" and c.summary == ["Ferrite approx. 600R"]
    assert c.parts["C1"]["value"] == "22nF" and "AC1" in c.nets
    assert "not found" in linked.load_companion(tmp / "gone").error


def test_link_both_ways():
    assert linked.add_link(MAIN, MAIN.folder).startswith("That is this project")
    msg = linked.add_link(MAIN, DISP.folder, "display for this controller")
    assert "Linked rev2" in msg and "linked back" in msg
    assert "already linked" in linked.add_link(MAIN, DISP.folder)
    assert MAIN.state()["linked"][0]["path"] == os.path.join("..", "Display Board", "rev2")   # relative: moves with the folder
    back = linked.links(DISP)
    assert len(back) == 1 and linked._same(back[0]["path"], MAIN.folder)


def test_ai_sees_summary_and_reads_detail():
    ctx = linked.context(MAIN)
    assert "LINKED DESIGNS" in ctx and "display for this controller" in ctx and "C1=C-US 22n" in ctx \
        and "MEASURED controller" in ctx
    fp = SysProvider([("", [("linked_design", {"what": "part", "ref": "c1"})]), ("C1 is 22n", [])])
    Agent(MAIN).run("what filters the backlight?", provider=fp)
    assert "LINKED DESIGNS" in fp.seen_system and "linked_design" in fp.seen_tools
    assert "update_system_doc" not in fp.seen_tools                 # no shared file yet
    out = dispatch(MAIN, "linked_design", {"what": "part", "ref": "C1"})
    assert out["value"] == "22n" and out["pins"] == {"1": "AC1", "2": "AC2"}
    assert "Controller board" in dispatch(DISP, "linked_design", {"name": "controller", "what": "summary"})["card"]
    assert "no linked design" in dispatch(DISP, "linked_design", {"name": "nope"})["error"]
    assert "J1 pins: 1=+12V 2=GND" in linked.context(DISP)            # connector pinout is in the card


def test_change_notice():
    assert linked.changes(MAIN) == []
    d = Design.load(DISP.design_path)
    d.parts["C1"].value = "18n"
    d.parts["R1"] = PartSpec("R1", "lib", "R-US_", "", "10")
    d.save(DISP.design_path)
    msgs = linked.changes(MAIN)
    assert len(msgs) == 1 and "C1: C-US 22n -> C-US 18n" in msgs[0] and "added R1" in msgs[0]
    assert linked.changes(MAIN) == []                                  # seen now
    d.parts["C1"].value = "15n"; d.save(DISP.design_path)
    fp = FakeProvider([("noted", [])])
    Agent(MAIN).run("hi", provider=fp)                                 # the AI is told too
    assert any("18n -> C-US 15n" in t["text"] for t in fp.seen_history)


def test_shared_system_requirements():
    sd = root / "SYSTEM.md"
    msg = linked.set_system_doc(MAIN, sd, create=True)
    assert sd.exists() and "also used by 1 linked project" in msg
    assert linked.system_doc_path(DISP) == sd.resolve()
    assert "SYSTEM REQUIREMENTS" in linked.context(MAIN) and "Shared values" in linked.context(DISP)
    linked.changes(DISP)
    out = dispatch(MAIN, "update_system_doc", {"text": "- I2C: 400 kHz, 3.3 V pull-ups on the display board"})
    assert out["ok"] and (root / "SYSTEM.md.bak").exists() and "400 kHz" in sd.read_text(encoding="utf-8")
    assert linked.changes(MAIN) == []                                  # its own edit is not news
    assert any("SYSTEM.md was changed" in m for m in linked.changes(DISP))
    fp = SysProvider([("ok", [])])
    Agent(MAIN).run("check", provider=fp, review=True)
    assert "3.3 V pull-ups" in fp.seen_system and "linked_design" in fp.seen_tools and "update_system_doc" not in fp.seen_tools


def test_no_links_no_extra_tools():
    p = Project(tmp / "alone", Settings(lib_dirs=[]))
    assert linked.context(p) == "" and linked.changes(p) == []
    fp = FakeProvider([("ok", [])])
    Agent(p).run("hi", provider=fp)
    assert "linked_design" not in fp.seen_tools and "update_system_doc" not in fp.seen_tools


def test_dialog_and_window():
    from eagleai.app import MainWindow
    from eagleai.linked_view import LinkedDesignsDialog
    p = make_project(tmp / "third", "Third", {"R1": ("R-US_", "1k")}, {}, [])
    QInputDialog.getText = staticmethod(lambda *a, **k: ("daughter board", True))
    dlg = LinkedDesignsDialog(p, lambda *a: str(MAIN.folder), lambda *a: (str(tmp / "display.md"), ""),
                              lambda *a: ("", ""))
    assert dlg.table.rowCount() == 0 and "Nothing linked" in dlg.preview_lbl.text()
    dlg.add_folder(); dlg.add_file()
    assert dlg.table.rowCount() == 2 and dlg.table.item(0, 1).text() == "daughter board"
    assert "LINKED DESIGNS" in dlg.preview.toPlainText() and "tokens" in dlg.preview_lbl.text()
    assert linked.system_doc_path(p) is not None                    # picked up the controller's shared file
    dlg.table.selectRow(1); dlg.remove()
    assert dlg.table.rowCount() == 1 and len(dlg.messages) == 3
    w = MainWindow()
    w.open_project(MAIN.folder)
    d = Design.load(DISP.design_path); d.parts["C1"].value = "12n"; d.save(DISP.design_path)
    w._linked_notices()
    for _ in range(20):
        app.processEvents(); time.sleep(0.01)
    assert "15n -&gt; C-US 12n" in w.log.toHtml() or "15n -> C-US 12n" in w.log.toPlainText()
    from PySide6.QtGui import QAction
    assert any(a.text() == "Linked designs…" for a in w.findChildren(QAction))


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
        from eagleai.app import terminate_self
        terminate_self(rc)
    os._exit(rc)
