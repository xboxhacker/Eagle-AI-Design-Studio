"""v0.4.5: smarter free JLC search, AI web-search verification, pasted LCSC lists."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eagleai import jlc, jlc_ai

C = jlc.Candidate


class FakeJlc:
    def __init__(self):
        self.calls = []
        self.parts = {
            "C15849": C("C15849", "CL10A105KB8NNNC", "0603", "1uF 50V X5R ±10% 0603 MLCC", 5979916, [(1, 0.0297)], True),
            "C97906": C("C97906", "GRM2165C1H103JA01D", "0805", "10nF 50V C0G 0805", 11089, [(1, 0.0611)]),
            "C42412353": C("C42412353", "IRLML2502TRPBF(ES)", "SOT-23", "20V 4.2A N-MOSFET", 1350, [(1, 0.0415)]),
        }

    def lookup(self, lcsc):
        self.calls.append(("lookup", lcsc))
        return self.parts.get(lcsc.upper())

    def by_mpn(self, q):
        self.calls.append(("mpn", q))
        return [c for c in self.parts.values() if jlc._norm(q) in jlc._norm(c.mfr)]

    def capacitors(self, farads, package):
        self.calls.append(("cap", farads, package))
        return [C("C1", "X6V", "0603", "1uF 6.3V X5R 0603", 10 ** 6, [(1, 0.001)], True),
                C("C15849", "CL10A105KB8NNNC", "0603", "1uF 50V X5R 0603", 10 ** 6, [(1, 0.0297)], True)]

    def resistors(self, ohms, package):
        return []

    def find(self, q):
        return []


def test_capacitor_voltage_filter():
    f = FakeJlc()
    ranked, how = jlc.best_for_part(f, "C", "1µF", "", "C0603", 3)
    assert ranked and ranked[0].lcsc == "C15849", (ranked, how)       # 6.3 V part rejected (< 16 V default)
    assert ("cap", 1e-06, "0603") in f.calls
    ranked, _ = jlc.best_for_part(f, "C", "1µF 6.3V", "", "C0603", 3)
    assert ranked[0].lcsc == "C1"                                       # explicit 6.3 V allows it


def test_mpn_variants_and_names():
    f = FakeJlc()
    ranked, how = jlc.best_for_part(f, "Q", "IRLML2502TRPbF", "", "SOT23", 6, names=("IRLML2502", ""))
    assert ranked and ranked[0].lcsc == "C42412353", how
    ranked, how = jlc.best_for_part(f, "X", "WIREPADSMD2,54-5,08", "", "SMD2,54-5,08", 1)
    assert not ranked and "AI" in how


def test_verify():
    f = FakeJlc()
    lines = {"C1": {"refs": "C1", "size": "0603"}, "C2": {"refs": "C2", "size": "0603"},
             "Q1, Q2": {"refs": "Q1, Q2", "size": ""}, "GND": {"refs": "GND"}, "U1": {"refs": "U1"}}
    props = [{"refs": "C1", "lcsc": "C15849"},
             {"refs": "C2", "lcsc": "C97906", "package_change": "0805"},
             {"refs": "Q1, Q2", "lcsc": "c42412353"},
             {"refs": "GND", "skip": "wire pad"},
             {"refs": "U1", "lcsc": "C999999999"}]
    out = {r["refs"]: r for r in jlc_ai.verify(props, f, lines, {"Q1, Q2": 6, "C1": 3})}
    assert out["C1"]["verified"] and not out["C1"]["problem"]
    assert out["C2"]["verified"] and "0805" in out["C2"]["problem"]
    assert out["Q1, Q2"]["lcsc"] == "C42412353" and not out["Q1, Q2"]["problem"]
    assert out["GND"]["problem"] == "wire pad"
    assert not out["U1"]["verified"] and "not list" in out["U1"]["problem"]


def test_parse_pasted_chat():
    text = """| C1 | 3 | **C15849** | Samsung CL10A105KB8NNNC, 1µF 50V X5R 0603 | Basic* |
| R1, R2 | 6 | **C22548** | Yageo RC0603FR-071KL, 1kΩ 1% 0603 | Extended |
- **U1:** UMW 78M05 at $0.118, against $0.43 for the ST L78M05ABDT-TR (C157412).
ATTRIBUTE U1 LCSC 'C347281';
ATTRIBUTE Q1 LCSC 'C42412353';"""
    got = jlc_ai.parse_lcsc_list(text, ["C1", "R1", "R2", "U1", "Q1", "Q2"])
    assert got == {"C1": "C15849", "R1": "C22548", "R2": "C22548", "U1": "C347281", "Q1": "C42412353"}, got


def test_assign_refuses_unknown():
    import tempfile
    from eagleai.model import PartSpec
    from eagleai.tools import Project, Settings, dispatch
    d = Path(tempfile.mkdtemp())
    p = Project(d, Settings(lib_dirs=[]))
    p.jlc = FakeJlc()
    p.design.parts["C1"] = PartSpec("C1", "rcl", "C-EU", "C0603", "1uF")
    p.design.parts["C2"] = PartSpec("C2", "rcl", "C-EU", "C0603", "1uF")
    assert "error" in dispatch(p, "jlc_assign", {"ref": "C1", "lcsc": "C123"})
    r = dispatch(p, "jlc_assign", {"ref": "C1,C2", "lcsc": "c15849"})
    assert r.get("ok") and p.design.parts["C2"].lcsc == "C15849", r
    assert dispatch(p, "jlc_lookup", {"lcsc": "C97906"})["package"] == "0805"



def test_memory_across_projects():
    import tempfile
    from eagleai.model import PartSpec
    from eagleai.tools import Project, Settings, price_bom, dispatch
    mem = Path(tempfile.mkdtemp()) / "jlc_memory.json"
    st = Settings(lib_dirs=[], jlc_memory_path=str(mem), build_qty=3)
    # project A: user's C1 has C15849, priced -> remembered
    a = Project(Path(tempfile.mkdtemp()), st); a.jlc = FakeJlc()
    a.design.parts["C1"] = PartSpec("C1", "rcl", "C-EU", "C0603", "1µF", lcsc="C15849")
    a.design.parts["Q1"] = PartSpec("Q1", "irlml", "IRLML2502", "", "IRLML2502TRPBF", lcsc="C42412353")
    price_bom(a)
    assert mem.exists() and len(a.jlc_mem) == 2
    # project B: same parts, no LCSC -> filled from memory, no search
    b = Project(Path(tempfile.mkdtemp()), st); b.jlc = FakeJlc()
    b.design.parts["C7"] = PartSpec("C7", "rcl", "C-EU", "C0603", "1µF")
    b.design.parts["Q3"] = PartSpec("Q3", "irlml", "IRLML2502", "", "IRLML2502TRPBF")
    res = price_bom(b, assign=True)
    assert b.design.parts["C7"].lcsc == "C15849" and b.design.parts["Q3"].lcsc == "C42412353", res
    assert res["from_memory"] == 2
    assert not any(c[0] in ("cap", "mpn") for c in b.jlc.calls), b.jlc.calls     # no search needed
    assert "remembered" in dispatch(b, "jlc_find", {"ref": "C7"})["searched"]
    # out of stock -> falls back to search
    c = Project(Path(tempfile.mkdtemp()), st); c.jlc = FakeJlc(); c.jlc.parts["C15849"].stock = 0
    c.design.parts["C9"] = PartSpec("C9", "rcl", "C-EU", "C0603", "1uF")
    price_bom(c, assign=True)
    assert any(x[0] == "cap" for x in c.jlc.calls)
    # higher voltage asked than remembered -> not recalled
    assert c.jlc_mem.recall(c.jlc_keys("C9"), need_volts=100) is None


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn()
            print("ok", n)
