"""AI-designed custom parts: pads with rot/shape, graphics (copper, outline, polygon, holes, text), geometry report."""
import sys, tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eagleai.tools import Project, Settings, dispatch


def proj():
    return Project(Path(tempfile.mkdtemp()), Settings(lib_dirs=[]))


def test_custom_part_with_graphics():
    p = proj()
    spec = {"name": "XT30_PW", "prefix": "J", "description": "XT30 right-angle power connector",
            "package": {"type": "custom", "name": "XT30PW", "silk_box": False,
                        "pads": [{"name": "1", "x": -2.5, "y": 0, "type": "tht", "drill": 1.3, "dia": 2.6, "shape": "long", "rot": 90},
                                 {"name": "2", "x": 2.5, "y": 0, "type": "tht", "drill": 1.3, "dia": 2.6, "shape": "long", "rot": 90},
                                 {"name": "M", "x": 0, "y": -4, "w": 3, "h": 1.5, "type": "smd", "roundness": 50}],
                        "graphics": [{"type": "wire", "x1": -5, "y1": -6, "x2": 5, "y2": -6, "width": 0.2, "layer": 21},
                                     {"type": "polygon", "layer": 1, "width": 0.2,
                                      "vertices": [{"x": -1, "y": -5}, {"x": 1, "y": -5}, {"x": 0, "y": -3, "curve": 30}]},
                                     {"type": "hole", "x": 0, "y": 3, "drill": 1.0},
                                     {"type": "rectangle", "x1": -5, "y1": -6, "x2": 5, "y2": 6, "layer": 51},
                                     {"type": "text", "x": -5, "y": 7, "text": ">NAME", "size": 1, "layer": 25}]},
            "pins": [{"name": "+", "pads": ["1"], "direction": "pwr"}, {"name": "-", "pads": ["2"], "direction": "pwr"},
                     {"name": "MNT", "pads": ["M"], "direction": "pas"}]}
    r = dispatch(p, "build_lbr", {"spec": spec})
    assert "error" not in r, r
    assert r["geometry"]["problems"] == "none", r["geometry"]
    root = ET.parse(r["path"]).getroot()
    pk = root.find(".//package")
    assert pk.find("polygon/vertex[@curve]") is not None and pk.find("hole") is not None
    assert pk.find("pad[@name='1']").get("rot") == "R90" and pk.find("smd").get("roundness") == "50"
    assert "XT30_PW" in r["deviceset"] and "+" in r["pins"]


def test_report_catches_mistakes():
    p = proj()
    spec = {"name": "BAD", "prefix": "U",
            "package": {"type": "custom", "name": "BAD",
                        "pads": [{"name": "1", "x": 0, "y": 0, "w": 1, "h": 1},
                                 {"name": "2", "x": 0.9, "y": 0, "w": 1, "h": 1},
                                 {"name": "3", "x": 5, "y": 0, "type": "tht", "drill": 0.2, "dia": 0.4}]},
            "pins": [{"name": "A", "pads": ["1"], "direction": "io"}, {"name": "B", "pads": ["2"], "direction": "io"},
                     {"name": "C", "pads": ["3"], "direction": "io"}]}
    r = dispatch(p, "build_lbr", {"spec": spec})
    probs = " ".join(r["geometry"]["problems"])
    assert "1 and 2 overlap" in probs and "drill 0.2" in probs and "annular" in probs, probs
    assert any("geometry:" in w for w in r["warnings"])


def test_unknown_graphic_rejected():
    p = proj()
    spec = {"name": "X", "package": {"type": "custom", "name": "X", "pads": [{"name": "1", "x": 0, "y": 0, "w": 1, "h": 1}],
                                     "graphics": [{"type": "via", "x": 0, "y": 0}]},
            "pins": [{"name": "A", "pads": ["1"], "direction": "io"}]}
    r = dispatch(p, "build_lbr", {"spec": spec})
    assert "error" in r and "via" in r["error"] or "unknown graphics" in str(r), r


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn(); print("ok", n)
