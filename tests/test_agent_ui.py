import os, sys, tempfile, types
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_engine as te
from eagleai.lbr_builder import build_lbr
from eagleai.tools import Project, Settings, price_bom
from eagleai.agent import Agent

tmp = Path(tempfile.mkdtemp())
for s in (te.BUCK, te.SOT, te.RES): build_lbr(s, tmp / "proj" / "libs")

from fakes import FakeProvider

class B:  # kept for older tests that import these names
    def __init__(s, **k): s.__dict__.update(k)
    def model_dump(s, exclude_none=True): return dict(s.__dict__)
class Msg:
    def __init__(s, content, stop): s.content, s.stop_reason = content, stop
class Stream: pass
class FakeClient: pass

st = Settings(); st.lib_dirs = []
p = Project(tmp / "proj", st)
print(p.load_libraries())
ag = Agent(p)
seen = []
fp = FakeProvider([("Adding parts. ", [("add_part", {"library": "LDO5", "deviceset": "LDO5"}),
                                         ("connect", {"net": "VIN", "pins": ["U1.IN", "U1.NOPE"]})]),
                    ("Done; U1.NOPE was invalid.", [])])
out = ag.run("add an LDO", on_tool=lambda n, a, r: seen.append((n, r)), provider=fp)
print(out, seen)
assert "U1" in p.design.parts and p.design.nets["VIN"] == ["U1.IN"]
assert "errors" in seen[1][1]
assert p.costs.session_usd == 0 and p.costs.unpriced == {"fake-model"}, p.costs.summary()
print(p.costs.summary())
assert ag.undo_last() and "U1" not in p.design.parts

# JLC pricing with a local CSV
csv = tmp / "parts.csv"
csv.write_text("LCSC Part,MFR.Part,Package,Library Type,Stock,Price,Description\n"
               "C25804,0603WAF1002T5E,0603,Basic Part,50000,\"1-199:0.0021,200-:0.0012\",10k resistor\n"
               "C99999,XYZ,0603,Extended Part,100,1-:0.0005,10k resistor\n")
st.jlc_csv = str(csv); p2 = Project(tmp / "proj", st); p2.load_libraries()
p2.design.add_part(p2.index, "R", "R", None, "R1", "10k", "0603WAF1002T5E")
print(price_bom(p2))

# UI
from PySide6.QtWidgets import QApplication
from eagleai.app import MainWindow
app = QApplication([])
w = MainWindow(); w.open_project(tmp / "proj")
import time
for _ in range(50): app.processEvents(); time.sleep(0.02)
p3 = w.project
p3.design.add_part(p3.index, "BUCK16", "BUCK16"); p3.design.add_part(p3.index, "R", "R", None, "R1", "10k")
p3.design.connect(p3.index, "FB", ["U1.FB", "R1.1"])
w.refresh(); w.show(); app.processEvents()
w.grab().save(str(tmp / "ui.png")); print("shot", tmp / "ui.png")
sys.stdout.flush()
if os.name == "nt":                     # end like the app does: Qt teardown with live workers can crash
    from eagleai.app import terminate_self
    terminate_self(0)
os._exit(0)
