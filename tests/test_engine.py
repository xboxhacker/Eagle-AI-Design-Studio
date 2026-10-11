import sys, tempfile, xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eagleai.lbr_builder import build_lbr, SpecError, validate_spec
from eagleai.library import LibraryIndex
from eagleai.model import Design
from eagleai.sch_writer import write_sch
from eagleai.preview import render_svg
from eagleai.jlc import parse_tiers, parse_value, rank, Candidate
from eagleai.costs import CostTracker

BUCK = {"name": "BUCK16", "prefix": "U", "description": "test buck",
  "package": {"type": "quad_smd", "name": "QFN16", "pins": 16, "pitch": 0.5, "span": 3.7,
              "pad_w": 0.3, "pad_l": 0.9, "body_w": 3, "body_l": 3,
              "exposed_pad": {"name": "EP", "w": 1.6, "h": 1.6}},
  "pins": [{"name": "VIN", "pads": ["1", "2"], "direction": "pwr"},
           {"name": "EN", "pads": ["3"], "direction": "in"},
           {"name": "SW", "pads": ["4", "5", "6"], "direction": "out"},
           {"name": "FB", "pads": ["7"], "direction": "in"},
           {"name": "GND", "pads": ["8", "9", "EP"], "direction": "pwr"}]}
SOT = {"name": "LDO5", "prefix": "U", "package": {"type": "sot23", "name": "SOT23-5", "pins": 5},
  "pins": [{"name": "IN", "pads": ["1"], "direction": "pwr"}, {"name": "GND", "pads": ["2"], "direction": "pwr"},
           {"name": "EN", "pads": ["3"], "direction": "in"}, {"name": "NC", "pads": ["4"], "direction": "nc"},
           {"name": "OUT", "pads": ["5"], "direction": "pwr"}]}
RES = {"name": "R", "prefix": "R", "package": {"type": "custom", "name": "R0603", "pads": [
        {"name": "1", "x": -0.8, "y": 0, "w": 0.8, "h": 0.9}, {"name": "2", "x": 0.8, "y": 0, "w": 0.8, "h": 0.9}]},
  "pins": [{"name": "1", "pads": ["1"], "direction": "pas", "side": "left"}, {"name": "2", "pads": ["2"], "direction": "pas", "side": "right"}]}

def test_all():
    tmp = Path(tempfile.mkdtemp())
    lib = tmp / "libs"
    warns = {}
    for s in (BUCK, SOT, RES):
        p, w = build_lbr(s, lib); ET.parse(p); warns[s["name"]] = w
    print("warnings:", warns)
    # bad spec rejected
    bad = dict(SOT, pins=[{"name": "X", "pads": ["99"], "direction": "io"}])
    try: validate_spec(bad); assert False
    except SpecError as e: print("rejected:", str(e)[:70])
    idx = LibraryIndex([lib]); n = idx.scan(); assert n == 3, n
    idx.unverified.update(["BUCK16", "LDO5"])
    print(idx.search("buck"))
    d = Design("t")
    for args in [("BUCK16", "BUCK16"), ("LDO5", "LDO5"), ("R", "R", None, "R1", "10k"), ("R", "R", None, "R2", "100k")]:
        ps, err = d.add_part(idx, *args); assert ps, err
    print(d.connect(idx, "VIN", ["U1.VIN", "U2.IN", "U2.EN"]))
    print(d.connect(idx, "GND", ["U1.gnd", "U2.GND", "U1.EP"]))
    print(d.connect(idx, "FB", ["U1.FB", "R1.1", "R2.1"]))
    print("bad:", d.connect(idx, "X", ["U1.NOPE"]))
    print("short pad:", d.connect(idx, "GND", ["U1.8"]))
    for p in d.validate(idx): print(p)
    out = write_sch(d, idx, tmp / "t.sch"); print(out)
    root = ET.parse(tmp / "t.sch").getroot()
    assert root.find(".//instances") is not None
    svg = render_svg(d, idx); ET.fromstring(svg); (tmp / "t.svg").write_text(svg)
    print("svg bytes", len(svg), "->", tmp)
    # jlc / costs
    assert parse_tiers("1-499:0.0179,500-:0.0144") == [(1, .0179), (500, .0144)]
    assert abs(parse_value("4k7") - 4700) < 1e-6 and abs(parse_value("100nF") - 1e-7) < 1e-12
    a = Candidate("C1","a","0603","",5000,[(1,.01)],basic=False); b = Candidate("C2","b","0603","",5000,[(1,.02)],basic=True)
    assert rank([a,b], 10)[0].lcsc == "C2"          # ext fee makes basic cheaper at qty 10
    ct = CostTracker(); c = ct.record("claude-sonnet-5-5", {"input_tokens":1000000,"output_tokens":100000}); assert abs(c.usd-3.0)<1e-9
    print("OK")
if __name__ == "__main__":
    test_all()
