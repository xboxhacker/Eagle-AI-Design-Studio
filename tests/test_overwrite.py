"""Overwrite guard: every write of an existing user file asks; overwrite keeps a backup; new = _vN; cancel = untouched."""
import sys, tempfile, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eagleai.safewrite import OverwriteGuard, versioned
from eagleai.tools import dispatch


def test_guard_choices():
    d = Path(tempfile.mkdtemp())
    f = d / "a.sch"; f.write_text("old")
    g = OverwriteGuard(d)
    assert g.target(d / "new.sch") == d / "new.sch"                  # nothing there: no question
    assert g.target(f) == d / "a_v2.sch"                              # no UI: never destroys
    asked = []
    g.ask = lambda p, w, warn: (asked.append(p.name), ("cancel", None))[1]
    assert g.target(f) is None and f.read_text() == "old"
    g.ask = lambda p, w, warn: ("overwrite", True)
    assert g.target(f) == f and list((d / ".eagleai" / "backups").glob("a.sch.*"))
    g.ask = lambda p, w, warn: (_ for _ in ()).throw(AssertionError("asked again"))
    assert g.target(f) == f                                           # remembered for the session
    (d / "a_v2.sch").write_text("x")
    assert versioned(f) == d / "a_v3.sch" and versioned(d / "a_v2.sch") == d / "a_v3.sch"
    # pair: one question, shared version number
    b, c = d / "BOM_x.xlsx", d / "CPL_x.xlsx"
    b.write_text("1"); c.write_text("2")
    g2 = OverwriteGuard(d)
    g2.ask = lambda p, w, warn: ("new", None)
    m = g2.targets([b, c])
    assert m[b].name == "BOM_x_v2.xlsx" and m[c].name == "CPL_x_v2.xlsx"


def test_generate_sch_asks_with_warnings():
    import test_md as t
    p = t.make_project(); t.build(p)
    first = dispatch(p, "generate_sch", {"filename": "board"})
    assert first["path"].endswith("board.sch")
    seen = {}
    def ask(path, what, warnings):
        seen["w"] = warnings; seen["what"] = what
        return ("cancel", None)
    p.guard.ask = ask
    r = dispatch(p, "generate_sch", {"filename": "board"})
    assert "cancelled" in r["error"] and seen["what"] == "schematic"
    (p.folder / "board.brd").write_text("<eagle/>")
    dispatch(p, "generate_sch", {"filename": "board"})
    assert any("board.brd" in w for w in seen["w"]), seen
    p.guard.ask = lambda *a: ("overwrite", False)
    r = dispatch(p, "generate_sch", {"filename": "board"})
    assert r["path"].endswith("board.sch") and "backup" in r.get("note", "")
    p.guard.ask = lambda *a: ("new", None)
    r = dispatch(p, "generate_sch", {"filename": "board"})
    assert r["path"].endswith("board_v2.sch")


def test_lbr_part_replace_asks_and_add_backs_up():
    import test_md as t
    p = t.make_project()
    lib = p.folder / "mylib.lbr"
    spec = {"name": "PAD1", "prefix": "TP", "package": {"type": "custom", "name": "PAD1",
            "pads": [{"name": "1", "x": 0, "y": 0, "w": 1, "h": 1}]}, "pins": [{"name": "P", "pads": ["1"], "direction": "pas"}]}
    assert "error" not in dispatch(p, "build_lbr", {"spec": spec, "save_to": str(lib)})
    asked = []
    p.guard.ask = lambda path, what, w: (asked.append(what), ("cancel", None))[1]
    r = dispatch(p, "build_lbr", {"spec": spec, "save_to": str(lib)})        # same part again -> asks
    assert "cancelled" in r["error"] and "PAD1" in asked[0]
    spec2 = dict(spec, name="PAD2", package=dict(spec["package"], name="PAD2"))
    r = dispatch(p, "build_lbr", {"spec": spec2, "save_to": str(lib)})      # new part -> added, backup, no question
    assert "error" not in r and len(asked) == 1
    assert list((p.folder / ".eagleai" / "backups").glob("mylib.lbr.*"))


def test_owned_file_and_jlc_export_cancel():
    import test_md as t
    from eagleai import bom_sync
    p = t.make_project(); t.build(p)
    (p.folder / "design_rules.dru").write_text("mdWireWire = 6mil\n")             # the user's own file
    asked = []
    p.guard.ask = lambda path, what, w: (asked.append(path.name), ("new", None))[1]
    dispatch(p, "design_rules", {"preset": "jlc_standard"})
    assert asked == ["design_rules.dru"] and (p.folder / "design_rules_v2.dru").exists()
    assert (p.folder / "design_rules.dru").read_text() == "mdWireWire = 6mil\n"
    # JLC export: existing pair -> cancel leaves them
    jd = p.folder / "jlc"; jd.mkdir()
    (jd / "BOM_b_top.xlsx").write_text("x"); (jd / "CPL_b_top.xlsx").write_text("y")
    p.guard.ask = lambda *a: ("cancel", None)
    try:
        bom_sync.export_jlc(p, p.folder / "b.brd", "top")
        assert False
    except bom_sync.Cancelled:
        pass
    assert (jd / "BOM_b_top.xlsx").read_text() == "x"


def test_scripts_keep_history():
    from eagleai.scripts import ScriptStore
    d = Path(tempfile.mkdtemp())
    s = ScriptStore(d)
    s.write("calc", "print(1)")
    r = s.write("calc", "print(2)")
    assert "_history" in r["note"] and list((d / "_history").glob("calc.*.py"))


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn(); print("ok", n)
