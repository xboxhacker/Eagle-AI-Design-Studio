import os, sys, tempfile, shutil, json
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_engine as te
from eagleai.lbr_builder import build_lbr
from eagleai.library import LibraryIndex, classify_bad_lbr, repair_xml_lbr
from eagleai.tools import Project, Settings, tool_generate_sch, tool_import_apply_nets
from eagleai.importer import parse_md
from eagleai.models import diff_new

tmp = Path(tempfile.mkdtemp())
libdir = tmp / "eagle" / "lbr" / "sub" / "deeper"          # nested: must be found
for s in (te.BUCK, te.SOT, te.RES): build_lbr(s, libdir)
(tmp / "eagle" / "lbr" / "old").mkdir(parents=True)
(tmp / "eagle" / "lbr" / "old" / "transistor.lbr").write_bytes(b"\x10\x80\x00\x00" + b"\x00\x12" * 200)
(tmp / "eagle" / "lbr" / "old" / "bomlib.lbr").write_bytes("﻿  junk".encode() + (libdir / "R.lbr").read_bytes().replace(b"<eagle", b"<eagle", 1)[38:])
st = Settings(); st.lib_dirs = [str(tmp / "eagle" / "lbr")]; st.converted_dir = str(tmp / "converted")

# 1) make a design and an Eagle-style .sch, then reopen as a fresh project and import it
pa = Project(tmp / "projA", st); print(pa.load_libraries())
print([ (s["name"], s["kind"]) for s in pa.index.problem_libs()])
assert any(s["kind"] == "binary" for s in pa.index.problem_libs())
fixed = repair_xml_lbr(tmp / "eagle/lbr/old/bomlib.lbr", tmp / "converted"); print("repair:", fixed)
d = pa.design
d.add_part(pa.index, "BUCK16", "BUCK16"); d.add_part(pa.index, "LDO5", "LDO5"); d.add_part(pa.index, "R", "R", None, "R1", "10k")
d.connect(pa.index, "VIN", ["U1.VIN", "U2.IN"]); d.connect(pa.index, "GND", ["U1.GND", "U2.GND"]); d.connect(pa.index, "FB", ["U1.FB", "R1.1"])
d.connect(pa.index, "VOUT", ["U2.OUT", "R1.2"])
res = tool_generate_sch(pa, "board"); print(res)
projB = tmp / "projB"; projB.mkdir(); shutil.copy(res["path"], projB / "board.sch")
st2 = Settings(); st2.lib_dirs = []; st2.converted_dir = str(tmp / "none")     # NO libraries installed: must use embedded
pb = Project(projB, st2); pb.load_libraries()
print("candidates:", pb.design_candidates())
rep = pb.import_sch(projB / "board.sch"); print("\n".join(rep))
assert set(pb.design.parts) == {"U1", "U2", "R1"}, pb.design.parts
assert sorted(map(sorted, pb.design.nets.values())) == sorted(map(sorted, d.nets.values())), (pb.design.nets, d.nets)
# reopen: embedded libs must persist
pc = Project(projB, st2); pc.load_libraries(); assert not [x for x in pc.design.validate(pc.index) if x.level == "error"]
# protect hand-edited sch
os.utime(projB / "board.sch", (1, 1))
r2 = tool_generate_sch(pc, "board"); print(r2.get("note")); assert r2["path"].endswith("board_v2.sch")   # no UI: never overwrite
print("reimport hint:", pc.externally_changed_sch())

# 2) markdown
md = tmp / "projC" / "led.md"; md.parent.mkdir()
md.write_text("""# LED test
## COMPLETE BILL OF MATERIALS
| Ref | Qty | Part Number | Value | Description | Package |
|-----|-----|-------------|-------|-------------|---------|
| U1 | 1 | LDO5 | LDO5 | regulator | SOT-23-5 |
| R1, R2 | 2 | Generic | 10kΩ | 10kΩ | 0603 |
## SECTION 1: Power
### U1 - LDO5
```
Pin 1 (IN): Net VIN ─── R1.Pin1
Pin 2 (GND): Net GND
Pin 5 (OUT): Net VOUT ─── R2.Pin1
Pin 4 (NC): Not connected
```
### R1 - 10kΩ
```
Pin 2: Net GND
```
Net GND: U1.Pin2 (GND), R1.Pin2, R2.Pin2
""", encoding="utf-8")
pm = parse_md(md); print(json.dumps(pm["nets"]), pm["conflicts"])
assert pm["nets"]["GND"] and set(pm["nets"]["VOUT"]) == {"U1.5", "R2.1"}
pcp = Project(md.parent, st); pcp.load_libraries(); pcp.import_md(md)
for ref, lib in (("U1", "LDO5"), ("R1", "R"), ("R2", "R")):
    pcp.design.add_part(pcp.index, lib, lib, None, ref, "10k" if lib == "R" else "")
print(tool_import_apply_nets(pcp)); print(pcp.design.nets)

print(diff_new([{"id": "claude-sonnet-5-5"}, {"id": "claude-sonnet-6"}], ["claude-x"]))

# 3) UI with a folder containing only a .sch: auto-import
from PySide6.QtWidgets import QApplication, QMessageBox
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
QMessageBox.information = staticmethod(lambda *a, **k: None)
from eagleai.app import MainWindow
import time
app = QApplication([])
projD = tmp / "projD"; projD.mkdir(); shutil.copy(projB / "board.sch", projD / "board.sch")
w = MainWindow(); w.settings.lib_dirs = st.lib_dirs; w.settings.converted_dir = st.converted_dir
w.open_project(projD)
for _ in range(150):
    app.processEvents(); time.sleep(0.02)
assert set(w.project.design.parts) == {"U1", "U2", "R1"}, w.project.design.parts
w.tabs.setCurrentIndex(3); w.show(); app.processEvents()
w.grab().save(str(tmp / "libs_tab.png")); print("shot", tmp / "libs_tab.png")
print("ALL OK")
sys.stdout.flush()
if os.name == "nt":                     # end like the app does: Qt teardown with live workers can crash
    from eagleai.app import terminate_self
    terminate_self(0)
os._exit(0)
