"""Router + design rules: routes the sample board DRC-clean, honours widths/clearance, no vias in SMD pads,
reads/writes Eagle .dru, writes traces into the .brd."""
import math, sys, tempfile, time
import xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_placement import mk
from eagleai import board as bm, placement, router, rules as R


def placed_board():
    f = mk()
    b = bm.read_brd(f)
    res = placement.place(b, {"rules": [{"ref": "C1", "near": "U1", "pin": "1", "max_dist": 2},
                                        {"ref": "J1", "edge": "left"}]})
    placement.write_brd(f, b, res)
    # drop the pre-routed EN trace
    import re
    t = re.sub(r'<wire x1="5" y1="5" x2="20" y2="27"[^>]*/>', "", f.read_text())
    f.write_text(t)
    return f, bm.read_brd(f)


def test_route_clean_and_rules():
    f, b = placed_board()
    rl = R.PRESETS["jlc_standard"]
    res = router.route(b, rl, widths={"VIN": 0.5, "GND": 0.5})
    assert not res.failed and res.drc == [], (res.failed, res.drc)
    assert set(res.routed) == {"VIN", "GND", "VOUT", "EN"}
    assert all(s.width == 0.5 for s in res.segs if s.net in ("VIN", "GND"))
    assert all(s.width == 0.2 for s in res.segs if s.net in ("VOUT", "EN"))
    # no via touches an SMD pad
    smd = [s for s in router.board_shapes(b, rl) if s.kind == "rect" and len(s.layers) == 1]
    for v in res.vias:
        c = router.Shape(v.net, (1, 16), "circ", (v.x, v.y, v.dia / 2))
        assert all(c.dist(p) > 0 for p in smd), v
    # every net is really connected: union of copper per net
    shapes = router.board_shapes(b, rl) + [router.Shape(s.net, (s.layer,), "seg", (s.x1, s.y1, s.x2, s.y2, s.width)) for s in res.segs] \
        + [router.Shape(v.net, (1, 16), "circ", (v.x, v.y, v.dia / 2)) for v in res.vias]
    for net in res.routed:
        objs = [s for s in shapes if s.net == net]
        parent = list(range(len(objs)))
        def find(i):
            while parent[i] != i:
                i = parent[i]
            return i
        for i in range(len(objs)):
            for j in range(i + 1, len(objs)):
                if set(objs[i].layers) & set(objs[j].layers) and objs[i].dist(objs[j]) <= 1e-6:
                    parent[find(i)] = find(j)
        pads = [i for i, o in enumerate(objs) if o.kind == "rect"]
        assert len({find(i) for i in pads}) == 1, f"{net} not connected"
    # every trace that meets a pad ends exactly at its centre; every joint is endpoint-to-endpoint
    assert router.check_terminations(b, res) == []
    for net in res.routed:
        for e, pn in b.signals[net]:
            el = b.elements[e]; pad = next(q for q in el.pads if q.name == pn)
            cx, cy = bm.transform(pad.x, pad.y, el.x, el.y, el.angle, el.mirror)
            assert any(abs(s.x1 - cx) < 1e-4 and abs(s.y1 - cy) < 1e-4 or abs(s.x2 - cx) < 1e-4 and abs(s.y2 - cy) < 1e-4
                       for s in res.segs if s.net == net), f"{e}.{pn} not hit at its centre"
    # wider clearance class is honoured
    res2 = router.route(b, rl, clearances={"VIN": 0.4})
    assert res2.drc == [], res2.drc


def test_tee_split_and_dangling():
    segs = [router.Seg("A", 1, 0, 0, 10, 0, 0.2), router.Seg("A", 1, 5, 0, 5, 5, 0.2)]
    out = router.split_tees(segs)
    ends = sorted((s.x1, s.y1, s.x2, s.y2) for s in out)
    assert ends == [(0, 0, 5, 0), (5, 0, 5, 5), (5, 0, 10, 0)], ends


def test_drc_catches_violation():
    rl = R.PRESETS["jlc_standard"]
    a = router.Shape("A", (1,), "seg", (0, 0, 10, 0, 0.2), "A trace")
    c = router.Shape("B", (1,), "seg", (0, 0.3, 10, 0.3, 0.2), "B trace")
    out = router.check_drc([a, c], rl, (-5, -5, 15, 5))
    assert out and "needs 0.200" in out[0], out


def test_script_and_brd():
    f, b = placed_board()
    rl = R.PRESETS["jlc_standard"]
    res = router.route(b, rl)
    res.pour = {"net": "GND", "layers": [16], "box": (1, 1, 39, 29), "isolate": 0.3}
    scr = router.eagle_script(res, rl)
    assert "LAYER 1;" in scr and "WIRE 'VIN' 0.2 (" in scr and "CHANGE DRILL 0.3;" in scr and "POLYGON 'GND'" in scr
    backup = router.write_brd(f, res, rl)
    assert backup.exists()
    root = ET.parse(f).getroot()
    sig = {s.get("name"): s for s in root.iter("signal")}
    assert sig["VIN"].findall("wire") and sig["GND"].find("polygon") is not None
    dr = root.find("drawing/board/designrules")
    assert dr is not None and any(p.get("name") == "mdWireWire" and p.get("value") == "0.2mm" for p in dr)
    b2 = bm.read_brd(f)
    assert len(b2.wires) == len(res.segs)
    # routing again now: everything is already connected -> skipped
    again = router.route(b2, rl)
    assert set(again.skipped) >= {"VIN", "VOUT", "EN"}, again.summary()


def test_dru_roundtrip_and_current():
    d = Path(tempfile.mkdtemp())
    p = R.write_dru(R.PRESETS["jlc_minimum_1oz"], d / "jlc.dru")
    r = R.read_dru(p)
    assert abs(r.min_clearance - 0.10) < 1e-6 and abs(r.min_trace - 0.10) < 1e-6 and abs(r.min_drill - 0.15) < 1e-6
    assert r.trace >= 0.2 and r.layers == 2
    (d / "eagle.dru").write_text("mdWireWire = 8mil\nmdWirePad = 8mil\nmsWidth = 6mil\nmsDrill = 0.35mm\nmdCopperDimension = 40mil\n")
    r2 = R.read_dru(d / "eagle.dru")
    assert abs(r2.clearance - 8 * 0.0254) < 1e-6 and abs(r2.edge_clearance - 1.016) < 1e-6
    assert R.trace_width_for_current(1.0) == 0.35 and R.trace_width_for_current(2.0) == 0.8
    assert R.PRESETS["jlc_standard_2oz"].check() == []


def test_bigger_board_speed():
    # 16 resistor pairs in a row on a 60x40 board, each pair joined + a shared bus
    parts, sigs = [], []
    for i in range(16):
        x = 6 + (i % 8) * 6.5; y = 10 if i < 8 else 28
        parts.append(f'<element name="R{i}" library="lib" package="0603" value="1k" x="{x}" y="{y}"/>')
        sigs.append(f'<signal name="N{i}"><contactref element="R{i}" pad="2"/><contactref element="R{(i + 5) % 16}" pad="1"/></signal>')
    brd = mk().read_text()
    import re
    brd = re.sub(r"<elements>.*</elements>", "<elements>" + "".join(parts) + "</elements>", brd, flags=re.S)
    brd = re.sub(r"<signals>.*</signals>", "<signals>" + "".join(sigs) + "</signals>", brd, flags=re.S)
    brd = brd.replace('x2="40"', 'x2="60"').replace('x1="40"', 'x1="60"').replace('y2="30"', 'y2="40"').replace('y1="30"', 'y1="40"')
    f = Path(tempfile.mkdtemp()) / "big.brd"; f.write_text(brd)
    b = bm.read_brd(f)
    t = time.time()
    res = router.route(b, R.PRESETS["jlc_standard"], time_limit=60)
    assert not res.failed and res.drc == [], res.summary()
    assert time.time() - t < 30, time.time() - t
    print("big board", res.summary())


def test_tools():
    from eagleai.tools import Project, Settings, dispatch
    from eagleai import eagle_link
    f, b = placed_board()
    p = Project(f.parent, Settings(lib_dirs=[]))
    p.bridge_dir = Path(tempfile.mkdtemp())
    r = dispatch(p, "design_rules", {})
    assert r["rules"]["name"].startswith("JLC standard") and r["problems"] == "none"
    assert "error" in dispatch(p, "design_rules", {"overrides": {"bogus": 1}})
    r = dispatch(p, "design_rules", {"overrides": {"clearance": 0.05}})
    assert r["problems"] != "none"
    assert "error" in dispatch(p, "route_board", {})               # rules below fab limits refused
    dispatch(p, "design_rules", {"preset": "jlc_standard"})
    assert (f.parent / "design_rules.dru").exists()
    p.ask_route = lambda board, res, live: "eagle"
    r = dispatch(p, "route_board", {"net_classes": [{"name": "power", "nets": ["VIN", "GND"], "current_a": 2}],
                                    "ground_pour": "GND"})
    assert r["drc"] == "clean" and "queued" in r["result"] and "power: 0.8 mm (2 A)" in r["classes"], r
    scr = (p.bridge_dir / "pending.scr").read_text()
    assert "WIRE 'VIN' 0.8" in scr and "POLYGON 'GND'" in scr
    p.ask_route = lambda *a: "brd"
    r = dispatch(p, "route_board", {})
    assert "written to" in r["result"], r


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn(); print("ok", n)
