"""Placement engine + Eagle link files, on a synthetic Eagle 7 board."""
import math, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eagleai import board as bm, placement, eagle_link

BRD = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE eagle SYSTEM "eagle.dtd">
<eagle version="7.4.0"><drawing><board>
<plain>
<wire x1="0" y1="0" x2="40" y2="0" width="0" layer="20"/><wire x1="40" y1="0" x2="40" y2="30" width="0" layer="20"/>
<wire x1="40" y1="30" x2="0" y2="30" width="0" layer="20"/><wire x1="0" y1="30" x2="0" y2="0" width="0" layer="20"/>
</plain>
<libraries><library name="lib"><packages>
<package name="SOT23-5"><smd name="1" x="-0.95" y="-1.3" dx="0.6" dy="1.0" layer="1"/><smd name="2" x="0" y="-1.3" dx="0.6" dy="1.0" layer="1"/>
<smd name="3" x="0.95" y="-1.3" dx="0.6" dy="1.0" layer="1"/><smd name="4" x="0.95" y="1.3" dx="0.6" dy="1.0" layer="1"/>
<smd name="5" x="-0.95" y="1.3" dx="0.6" dy="1.0" layer="1"/></package>
<package name="0603"><smd name="1" x="-0.8" y="0" dx="0.9" dy="1.0" layer="1"/><smd name="2" x="0.8" y="0" dx="0.9" dy="1.0" layer="1"/></package>
<package name="HDR2"><pad name="1" x="-1.27" y="0" drill="1" diameter="1.8"/><pad name="2" x="1.27" y="0" drill="1" diameter="1.8"/>
<wire x1="-2.5" y1="-1.2" x2="2.5" y2="-1.2" width="0.2" layer="21"/><wire x1="-2.5" y1="1.2" x2="2.5" y2="1.2" width="0.2" layer="21"/></package>
</packages></library></libraries>
<elements>
<element name="U1" library="lib" package="SOT23-5" value="LDO" x="5" y="5"/>
<element name="C1" library="lib" package="0603" value="1uF" x="35" y="25"/>
<element name="C2" library="lib" package="0603" value="1uF" x="34" y="5" rot="R90"/>
<element name="R1" library="lib" package="0603" value="10k" x="20" y="27"/>
<element name="J1" library="lib" package="HDR2" value="" x="30" y="15"/>
</elements>
<signals>
<signal name="VIN"><contactref element="U1" pad="1"/><contactref element="C1" pad="1"/><contactref element="J1" pad="1"/></signal>
<signal name="GND"><contactref element="U1" pad="2"/><contactref element="C1" pad="2"/><contactref element="C2" pad="2"/><contactref element="J1" pad="2"/></signal>
<signal name="VOUT"><contactref element="U1" pad="5"/><contactref element="C2" pad="1"/><contactref element="R1" pad="1"/></signal>
<signal name="EN"><contactref element="U1" pad="3"/><contactref element="R1" pad="2"/><wire x1="5" y1="5" x2="20" y2="27" width="0.25" layer="1"/></signal>
</signals></board></drawing></eagle>
"""


def mk():
    d = Path(tempfile.mkdtemp())
    f = d / "ldo.brd"
    f.write_text(BRD)
    return f


def test_place_rules_and_constraints():
    b = bm.read_brd(mk())
    assert b.has_outline and b.outline == (0, 0, 40, 30) and len(b.elements) == 5
    rules = {"rules": [{"ref": "C1", "near": "U1", "pin": "VIN", "max_dist": 2.0},
                       {"ref": "C2", "near": "U1", "pin": "VOUT", "max_dist": 2.0},
                       {"ref": "J1", "edge": "left"}]}
    res = placement.place(b, rules, lambda ref, pin: {"VIN": ["1"], "VOUT": ["5"]}.get(pin, []))
    assert not res.unplaced, res.warnings
    pos = {k: (p.x, p.y, p.angle, p.mirror) for k, p in res.placed.items()}
    boxes = {k: b.elements[k].box_at(*v) for k, v in pos.items()}
    for k, bx in boxes.items():
        assert bx[0] >= 1 - 1e-6 and bx[1] >= 1 - 1e-6 and bx[2] <= 39 + 1e-6 and bx[3] <= 29 + 1e-6, (k, bx)
    ks = list(boxes)
    for i in range(len(ks)):
        for j in range(i + 1, len(ks)):
            a, c = boxes[ks[i]], boxes[ks[j]]
            assert not (a[0] < c[2] - 1e-6 and c[0] < a[2] - 1e-6 and a[1] < c[3] - 1e-6 and c[1] < a[3] - 1e-6), (ks[i], ks[j])
    u = b.elements["U1"]; vin = u.pad_at("1", *pos["U1"])
    c1 = b.elements["C1"].pad_at("1", *pos["C1"])
    assert math.hypot(vin[0] - c1[0], vin[1] - c1[1]) <= 2.0 + 1e-6, (vin, c1)
    assert boxes["J1"][0] <= 1.0 + 0.5 + 0.05               # on the left edge (edge + half the part spacing)
    assert res.ratsnest_after < res.ratsnest_before
    s = res.summary()
    assert s["moved"] >= 4 and s["warnings"] == "none", s
    return b, res


def test_script_and_brd_write():
    f = mk()
    b = bm.read_brd(f)
    res = placement.place(b, {"rules": [{"ref": "C1", "near": "U1", "pin": "1", "max_dist": 2}], "keep": ["R1"]})
    assert not res.placed["R1"].moved
    scr = placement.eagle_script(b, res)
    assert scr.startswith("GRID MM;") and "MOVE 'C1' (" in scr and "GRID LAST;" in scr and "'R1'" not in scr
    backup, warns = placement.write_brd(f, b, res)
    assert backup.exists()
    b2 = bm.read_brd(f)
    assert abs(b2.elements["C1"].x - res.placed["C1"].x) < 1e-3
    assert any("U1 had routed traces" in w for w in warns), warns       # EN is routed
    assert f.read_text().startswith('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle')


def test_bottom_side_and_locked_and_coil():
    b = bm.read_brd(mk())
    b.elements["U1"].locked = True
    res = placement.place(b, {"side": "bottom"})
    assert not res.placed["U1"].moved
    assert all(p.mirror for p in res.placed.values() if p.moved)
    scr = placement.eagle_script(b, res)
    assert "MIRROR 'C1';" in scr and "ROTATE =MR" in scr


def test_state_dump_roundtrip():
    d = Path(tempfile.mkdtemp())
    # what studio.ulp would write for U1 rotated 90: absolute pad positions
    st = d / "state.txt"
    st.write_text("BOARD\tC:/x/ldo.brd\nO\t0\t0\t40\t0\nO\t40\t0\t40\t30\n"
                  "E\tU1\tLDO\tSOT23-5\tlib\t10.0000\t10.0000\t90.00\t0\n"
                  "P\tU1\t1\t11.3000\t9.0500\t1.0000\t0.6000\n"
                  "E\tC1\t1uF\t0603\tlib\t20\t20\t0.00\t1\n"
                  "P\tC1\t1\t20.8000\t20.0000\t0.9000\t1.0000\n"
                  "S\tVIN\tU1\t1\nS\tVIN\tC1\t1\nEND\n")
    b = bm.read_state(st)
    u = b.elements["U1"]
    p1 = next(p for p in u.pads if p.name == "1")
    assert abs(p1.x - (-0.95)) < 1e-6 and abs(p1.y - (-1.3)) < 1e-6 and (p1.w, p1.h) == (0.6, 1.0), p1
    c = b.elements["C1"]; q = c.pads[0]
    assert c.mirror and abs(q.x - (-0.8)) < 1e-6                  # mirrored: x flipped back
    assert b.signals["VIN"] == [("U1", "1"), ("C1", "1")]
    eagle_link.install(d)
    assert (d / "studio.ulp").exists()


def test_tool_flow():
    import tempfile as tf
    from eagleai.tools import Project, Settings, dispatch
    f = mk()
    p = Project(f.parent, Settings(lib_dirs=[]))
    p.bridge_dir = Path(tf.mkdtemp())
    st = dispatch(p, "board_state", {})
    assert "U1(SOT23-5)" in st["elements"] and st["source"].startswith("saved"), st
    seen = {}
    def ask(board, res, live):
        seen["live"] = live
        return "eagle"
    p.ask_placement = ask
    r = dispatch(p, "place_parts", {"rules": [{"ref": "C1", "near": "U1", "pin": "1", "max_dist": 2}]})
    assert "queued" in r["result"] and seen["live"] is False, r
    assert eagle_link.pending(p.bridge_dir) and "MOVE 'C1'" in (p.bridge_dir / "pending.scr").read_text()
    p.ask_placement = lambda *a: None
    assert "did not apply" in dispatch(p, "place_parts", {})["result"]


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn(); print("ok", n)
