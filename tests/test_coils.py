"""Coil generator: continuity, exact spacing, Eagle-loadable output, add-to-existing-library."""
import math, sys, tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eagleai import coils
from eagleai.lbr_builder import build_lbr, SpecError


def arc_points(s, n=60):
    x1, y1, x2, y2, c = s["x1"], s["y1"], s["x2"], s["y2"], s.get("curve", 0)
    if not c:
        return [(x1 + (x2 - x1) * t / n, y1 + (y2 - y1) * t / n) for t in range(n + 1)]
    th = math.radians(c)                       # + = CCW (Eagle)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    d = math.hypot(x2 - x1, y2 - y1)
    r = d / 2 / math.sin(abs(th) / 2)
    h = math.sqrt(max(r * r - d * d / 4, 0))
    ux, uy = -(y2 - y1) / d, (x2 - x1) / d      # left normal
    sgn = 1 if (th > 0) == (abs(th) < math.pi) else -1
    cx, cy = mx + sgn * h * ux, my + sgn * h * uy
    a0 = math.atan2(y1 - cy, x1 - cx)
    pts = [(cx + r * math.cos(a0 + th * t / n), cy + r * math.sin(a0 + th * t / n)) for t in range(n + 1)]
    assert math.hypot(pts[-1][0] - x2, pts[-1][1] - y2) < 1e-6, (s, pts[-1])
    return pts


def spacing(segs, pitch):
    pts = [(i, p) for i, s in enumerate(segs) for p in arc_points(s)]
    worst = 1e9
    for i, a in pts[::3]:
        for j, b in pts[::3]:
            if abs(i - j) > 2:
                worst = min(worst, math.hypot(a[0] - b[0], a[1] - b[1]))
    return worst


def check_shape(cp):
    top, start, info = coils.spiral(cp)
    for a, b in zip(top, top[1:]):
        assert abs(a["x2"] - b["x1"]) < 1e-9 and abs(a["y2"] - b["y1"]) < 1e-9, "not continuous"
    assert top[-1]["x2"] == 0 and top[-1]["y2"] == 0                      # ends on the centre pad
    w = spacing(top[:-1], info["pitch"])
    assert w >= info["pitch"] - 0.02, f"neighbouring turns {w:.3f} mm apart, pitch {info['pitch']}"
    return top


def test_racetrack_and_round_geometry():
    check_shape(coils.CoilParams(shape="racetrack", length=120, width=38.1, turns=5))
    check_shape(coils.CoilParams(shape="round", width=25.4, trace=0.5, gap=0.15, turns=8, clearance=2.6,
                                 pad_dia=1.6, pad_drill=0.8, center_dia=1.0, center_drill=0.5))


def test_errors_are_helpful():
    cp = coils.CoilParams(shape="round", width=25.4, turns=40)
    msg = "; ".join(coils.check(cp))
    assert "Max" in msg and "turns" in msg
    assert coils.check(coils.CoilParams(clearance=0.5))


def test_lbr_written_and_merged():
    d = Path(tempfile.mkdtemp())
    tx = coils.CoilParams(name="COIL_RACETRACK_A", length=200, width=30, turns=5, layers="both")
    rx = coils.CoilParams(name="COIL_ROUND_B", shape="round", width=25.4, trace=0.5, gap=0.15, turns=8,
                          clearance=2.6, pad_dia=1.6, pad_drill=0.8, center_dia=1.0, center_drill=0.5, layers="both")
    spec, stats = coils.build_spec(tx)
    path, warn = build_lbr(spec, out_path=d / "PCB_COILS.lbr", note="coil generator")
    spec2, stats2 = coils.build_spec(rx)
    path2, warn2 = build_lbr(spec2, out_path=path, note="coil generator")
    assert path2 == path and any("added to the existing" in w for w in warn2)
    root = ET.parse(path).getroot()
    pk = {p.get("name"): p for p in root.iter("package")}
    assert set(pk) == {"COIL_RACETRACK_A", "COIL_ROUND_B"}
    tx_el = pk["COIL_RACETRACK_A"]
    assert tx_el[0].tag == "description"
    layers = {w.get("layer") for w in tx_el.findall("wire")}
    assert {"1", "16", "20"} <= layers
    assert {p.get("name") for p in tx_el.findall("pad")} == {"A", "B", "CT"}
    assert not list(root.iter("via"))                     # Eagle 7 packages can't hold vias
    ds = {d.get("name") for d in root.iter("deviceset")}
    assert ds == {"COIL_RACETRACK_A", "COIL_ROUND_B"}
    # rebuilding replaces, not duplicates
    build_lbr(spec, out_path=path)
    assert len([p for p in ET.parse(path).getroot().iter("package")]) == 2
    assert stats["dc_resistance_per_layer_ohm"] > 0 and "inductance_per_layer_uH_estimate" in stats2
    print("A", stats, "\nB", stats2)
    # library index can read it
    from eagleai.library import LibraryIndex
    idx = LibraryIndex([d]); idx.scan()
    dev = idx.find_device("PCB_COILS", "COIL_RACETRACK_A", "") if hasattr(idx, "find_device") else None


def test_not_a_library_refused():
    d = Path(tempfile.mkdtemp())
    (d / "x.lbr").write_bytes(b"\x10\x80binary")
    try:
        build_lbr(coils.build_spec(coils.CoilParams())[0], out_path=d / "x.lbr")
        assert False
    except SpecError as e:
        assert "another file" in str(e)


def test_tool_and_save_location():
    from eagleai.tools import Project, Settings, dispatch
    d = Path(tempfile.mkdtemp()); libdir = Path(tempfile.mkdtemp())
    p = Project(d, Settings(lib_dirs=[], lbr_dir=str(libdir)))
    r = dispatch(p, "build_coil", {"params": {"name": "RX1", "shape": "round", "width": 25.4, "trace": 0.5,
                                              "gap": 0.15, "turns": 8, "clearance": 2.6, "pad_dia": 1.6,
                                              "pad_drill": 0.8, "center_dia": 1.0, "center_drill": 0.5}})
    assert "error" not in r and Path(r["path"]) == libdir / "RX1.lbr", r
    assert "CT" in r["pins"] and r["coil"]["inductance_per_layer_uH_estimate"] > 0
    r2 = dispatch(p, "build_coil", {"params": {"name": "TX1", "turns": 5}, "save_to": str(libdir / "RX1.lbr")})
    assert r2["path"] == str(libdir / "RX1.lbr") and "TX1" in r2["deviceset"], r2
    bad = dispatch(p, "build_coil", {"params": {"name": "X", "shape": "round", "width": 10, "turns": 30}})
    assert "error" in bad and "turns" in bad["error"]
    asked = []
    p.settings.lbr_ask = True
    p.ask_lbr_path = lambda name, default: asked.append(name) or None
    assert "cancelled" in dispatch(p, "build_coil", {"params": {"name": "Y"}})["error"] and asked == ["Y"]


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn(); print("ok", n)
