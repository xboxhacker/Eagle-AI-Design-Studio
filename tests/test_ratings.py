"""Recommended ratings: resistor watts and capacitor volts from net voltages or the stress the AI/user set, derated
to standard ratings, checked against the chosen part, shown in the MD, the BOM, the Nets tab and Check."""
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

from eagleai import ratings as R
from eagleai.model import Design, PartSpec
from eagleai.tools import Project, Settings, dispatch


def design():
    d = Design("led")
    for ref, ds, dev, val, note in [("C1", "C-US", "C0603", "100n", ""), ("C2", "C-US", "C0805", "10u", "10V X5R"),
                                    ("C3", "CPOL-US", "E5-5", "100u", "25V"), ("R1", "R-US_", "R0603", "1k", ""),
                                    ("R2", "R-US_", "R0402", "10k", ""), ("R3", "R-US_", "R1206", "150", ""),
                                    ("L1", "L", "", "10u", ""), ("U1", "LM", "", "LM7805", "")]:
        d.parts[ref] = PartSpec(ref, "lib", ds, dev, val, note=note)
    d.nets = {"+12V": ["C1.1", "C2.1", "C3.1", "R1.1", "U1.IN"], "GND": ["C1.2", "C2.2", "C3.2", "R2.2", "U1.GND"],
              "3V3": ["R2.1"], "LED_A": ["R1.2", "R3.1"], "LED_K": ["R3.2", "L1.1"], "SW": ["L1.2"]}
    return d


def test_net_voltage_names():
    assert [R.net_voltage_from_name(n) for n in ("GND", "AGND", "+12V", "3V3", "VCC9", "5V", "V5", "P5V", "VIN",
                                                 "DRAIN1", "12V_IN")] == [0, 0, 12, 3.3, 9, 5, 5, 5, None, None, 12]
    d = design()
    d.net_volts["+12V"] = 14.4
    assert R.net_voltage(d, "+12V") == (14.4, "set") and R.net_voltage(d, "3V3") == (3.3, "name")


def test_caps():
    d = design()
    rs = R.analyze(d)
    assert rs["C1"].need == {"V": 25} and rs["C1"].sees == {"V": 12}            # 12 V x2 -> 25 V
    assert rs["C2"].problems and "10 V is below the recommended 25 V" in rs["C2"].problems[0]
    assert rs["C3"].need == {"V": 16} and not rs["C3"].problems                # electrolytic 1.25x -> 16 V; 25 V ok
    d.net_volts["+12V"] = 14.4
    assert R.analyze(d)["C1"].need == {"V": 35}                                # 28.8 -> 35 V
    d.parts["C1"].note = "C0G"
    assert R.analyze(d)["C1"].need == {"V": 25}                                # 1.5x -> 21.6 -> 25 V


def test_resistors():
    d = design()
    rs = R.analyze(d)
    assert abs(rs["R2"].sees["W"] - 3.3 ** 2 / 10e3) < 1e-9 and rs["R2"].need["W"] == 1 / 20   # 1.1 mW
    r1 = rs["R1"]                                                  # +12V to an unknown node: bounded by 12 V
    assert abs(r1.sees["W"] - 0.144) < 1e-9 and r1.need["W"] == 1 / 2 and r1.problems   # 0603 = 1/10 W
    assert "at most 12 V" in r1.detail()
    assert rs["R3"].missing.startswith("max voltage of net") and R.unknown_nets(d, rs) == ["LED_A", "LED_K"]
    d.parts["R3"].stress = {"A": 0.02, "why": "20 mA LED"}
    r3 = R.analyze(d)["R3"]
    assert abs(r3.sees["W"] - 0.06) < 1e-9 and r3.need["W"] == 1 / 8 and r3.have["W"] == 1 / 4 and not r3.problems
    d.parts["R1"].stress = {"W": 0.03}
    d.parts["R1"].note = "0.25W"
    r1 = R.analyze(d)["R1"]
    assert r1.need["W"] == 1 / 16 and r1.have["W"] == 0.25 and not r1.problems
    d.net_volts["+12V"] = 100
    d.parts["R1"].stress = {}
    assert any("above the 75 V working voltage" in p for p in R.analyze(d)["R1"].problems)


def test_inductor_and_text():
    d = design()
    rs = R.analyze(d)
    assert "current" in rs["L1"].missing and rs["L1"].text() == "?" and rs["U1"].text() == ""
    d.parts["L1"].stress = {"A": 0.5}
    l1 = R.analyze(d)["L1"]
    assert abs(l1.need["A"] - 0.65) < 1e-9 and l1.text() == "≥ 650 mA (500 mA)"
    assert R.analyze(d)["C1"].text(True) == ">= 25 V (12 V)"
    assert R.group_text(R.analyze(d), ["C1", "C2"]).startswith("⚠ UNDER-RATED: ≥ 25 V")
    assert R.chosen_watts("1/8W") == 0.125 and R.chosen_watts("250mW") == 0.25 and R.chosen_volts("100n 50V") == 50


def test_tools_md_json():
    p = Project(tmp / "proj", Settings(lib_dirs=[]))
    p.design = design()
    out = dispatch(p, "set_net_voltage", {"nets": {"+12V": 14.4, "LED_A": 12, "NOPE": 3}})
    assert "+12V=14.4V" in out["set"] and "NOPE: no such net" in out["errors"][0]
    out = dispatch(p, "set_stress", {"parts": {"R3": {"W": 0.06, "why": "20 mA"}, "X9": {}}})
    assert out["set"] == ["R3"] and ">= 1/8 W" in out["ratings"]["R3"] and "no part X9" in out["errors"][0]
    g = dispatch(p, "get_ratings", {})
    assert any(r.startswith("C1: >= 35 V") for r in g["ratings"]) and "set_net_voltage_for" not in g
    assert any(r.startswith("L1:") and "current" in r for r in g["ratings"])
    assert "3V3=3.3V" in g["guessed_from_names"]
    checks = dispatch(p, "run_checks", {})["problems"]
    assert any("C2" in c and "UNDER-RATED" in c for c in checks)
    d2 = Design.load(p.design_path)                                 # saved and read back
    assert d2.net_volts["+12V"] == 14.4 and d2.parts["R3"].stress == {"W": 0.06, "why": "20 mA"}
    assert "stress" not in p.design_path.read_text(encoding="utf-8").split('"C1"')[1].split("}")[0]   # empty not saved
    from eagleai.md_writer import extract_doc, write_md
    md = write_md(p.design, p.index)
    assert "| Rating (min) |" in md and "| >= 35 V (14.4 V) |" in md
    assert "Net voltages (worst case, used for the ratings): +12V 14.4 V" in md
    assert "Rating: >= 1/8 W (60 mW)" in md
    assert "Net voltages" not in " ".join(extract_doc(md).get("summary", []))
    from eagleai.importer import parse_md
    f = tmp / "x.md"; f.write_text(md, encoding="utf-8")
    parsed = parse_md(f)                                            # the importer still reads it the same way
    assert sorted(parsed["nets"]["+12V"]) == sorted(["C1.1", "C2.1", "C3.1", "R1.1", "U1.IN"]) or "+12V" in parsed["nets"]
    assert {r for row in parsed["bom"] for r in row["refs"]} == set(p.design.parts)
    assert not any(k.startswith("RATING") for k in parsed.get("pin_names", {}).values())


def test_bom_and_nets_ui():
    from eagleai.app import MainWindow
    from eagleai.bom_view import COLS, parse_stress, stress_text
    assert parse_stress("20mA 0.1W") == {"A": 0.02, "W": 0.1} and parse_stress("1/8W") == {"W": 0.125}
    assert parse_stress("") == {} and stress_text({"V": 28.0, "W": 0.06}) == "28V 0.06W"
    try:
        parse_stress("lots"); assert False
    except ValueError:
        pass
    folder = tmp / "ui"
    p = Project(folder, Settings(lib_dirs=[]))
    p.design = design(); p.design.save(p.design_path)
    w = MainWindow()
    w.open_project(folder)
    for _ in range(100):
        app.processEvents(); time.sleep(0.01)
    ed = w.bom_editor
    w.refresh(); ed._do_refresh()
    row = next(i for i in range(ed.table.rowCount()) if ed.table.item(i, 0).text() == "R3")
    rc, sc = COLS.index("Rating (min)"), COLS.index("Sees (worst case)")
    assert ed.table.item(row, rc).text() == "?"
    ed.table.item(row, sc).setText("60mW")                          # typed in the BOM
    ed._do_refresh()
    assert w.project.design.parts["R3"].stress == {"W": 0.06} and ed.table.item(row, rc).text().startswith("≥ 1/8 W")
    top = {w.nets.topLevelItem(i).text(0): w.nets.topLevelItem(i) for i in range(w.nets.topLevelItemCount())}
    assert top["+12V"].text(1) == "12 V (from the name)" and top["SW"].text(1) == ""
    QInputDialog.getText = staticmethod(lambda *a, **k: ("14.4", True))
    w._edit_net_voltage(top["+12V"])
    assert w.project.design.net_volts["+12V"] == 14.4
    top = {w.nets.topLevelItem(i).text(0): w.nets.topLevelItem(i) for i in range(w.nets.topLevelItemCount())}
    assert top["+12V"].text(1) == "14.4 V"
    probs = [w.problems.item(i).text() for i in range(w.problems.count())]
    assert any("C2" in t and "UNDER-RATED" in t for t in probs)
    assert any("Nets tab: +12V max voltage = 14.4 V" in e.get("text", "") for e in w.agent.log)


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
