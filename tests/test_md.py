"""Markdown document: generated from the design, round-trips through the MD importer, stays in sync,
detects outside edits."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eagleai.importer import parse_md
from eagleai.tools import Project, Settings, dispatch


def make_project():
    import test_engine as te
    from eagleai.lbr_builder import build_lbr
    d = Path(tempfile.mkdtemp())
    lib = d / "lib"
    cap = dict(te.RES, name="C", prefix="C", package=dict(te.RES["package"], name="C0603"))
    for s in (te.SOT, te.RES, cap):
        build_lbr(s, lib)
    p = Project(d / "proj", Settings(lib_dirs=[str(lib)])) if (d / "proj").mkdir() is None else None
    p.load_libraries()
    return p


def build(p):
    r = dispatch(p, "add_parts", {"parts": [
        {"library": "LDO5", "deviceset": "LDO5", "ref": "U1", "mpn": "AP2112K-3.3"},
        {"library": "C", "deviceset": "C", "ref": "C1", "value": "1u"},
        {"library": "C", "deviceset": "C", "ref": "C2", "value": "1u"},
        {"library": "R", "deviceset": "R", "ref": "R1", "value": "10k"}]})
    assert "error" not in str(r)[:200].lower() or "design_errors" in r, r
    for net, pins in {"VIN": ["U1.IN", "C1.1", "R1.1"], "GND": ["U1.GND", "C1.2", "C2.2"],
                      "+3V3": ["U1.OUT", "C2.1"], "EN": ["U1.EN", "R1.2"]}.items():
        r = dispatch(p, "connect", {"net": net, "pins": pins})
        assert "error" not in r, r


def check_prompt_format(text):
    """The rules of the Schematic MD format that code can check."""
    import re
    lines = text.splitlines()
    assert lines[0].startswith("# ")
    heads = [l for l in lines if l.startswith("## ")]
    assert heads[0] == "## Datasheet Citations", heads           # immediately after the summary
    assert "## COMPLETE BILL OF MATERIALS" in heads
    assert all(h in ("## Datasheet Citations", "## COMPLETE BILL OF MATERIALS") or re.match(r"## SECTION \d+: ", h)
               for h in heads), heads
    bom_head = next(l for l in lines if l.startswith("| Ref |"))
    for col in ("Ref", "Qty", "Part Number", "Value", "Description", "Package"):
        assert f"| {col} |" in bom_head
    # every char ASCII except ─ Ω µ
    bad = {c for c in text if ord(c) > 127 and c not in "─Ωµ"}
    assert not bad, bad
    # ### REF heading followed immediately by a fence
    for i, l in enumerate(lines):
        if l.startswith("### "):
            assert lines[i + 1] == "```", (l, lines[i + 1])
    # pin lines: non-passives use Pin N (name); at most one target token; connections agree both ways
    tok = re.compile(r"\b([A-Z]{1,4}\d{1,4}[A-Z]?)\.Pin(\d+)")
    cur, links = None, set()
    in_block = False
    for l in lines:
        if l.startswith("### "):
            cur = l.split()[1]; continue
        if l == "```":
            in_block = not in_block; continue
        if in_block and cur and l.startswith("Pin "):
            if not re.match(r"[RCL]\d", cur):
                assert re.match(r"^Pin \d+ \([^)]+\): ", l), l
            ts = tok.findall(l)
            assert len(ts) <= 1, l
            src = re.match(r"^Pin (\d+)", l).group(1)
            for r, pn in ts:
                links.add(tuple(sorted([(cur, src), (r, pn)])))
    return links


def test_md_roundtrip_and_sync():
    p = make_project()
    build(p)
    md = p.md_path()
    assert md.exists(), p.md_status
    text = md.read_text(encoding="utf-8")
    assert "## COMPLETE BILL OF MATERIALS" in text and "### U1 - AP2112K-3.3" in text and "### C1 - 1µF" in text
    assert "\nNet GND: " in text and "Net VIN ─── " in text and "> Created with Eagle AI Design Studio v" in text
    check_prompt_format(text)
    parsed = parse_md(md)
    got = {n: sorted(m) for n, m in parsed["nets"].items()}
    # every design net exists in the MD with the same pads
    from eagleai.md_writer import _members
    for net in p.design.nets:
        want = sorted(f"{r}.{pad}" for r, pad, _n in _members(p.design, p.index, net))
        assert got.get(net) == want, (net, got.get(net), want)
    refs = {r for row in parsed["bom"] for r in row["refs"]}
    assert refs == set(p.design.parts)
    # BOM edits keep it in sync
    dispatch(p, "set_value", {"ref": "R1", "value": "47k", "lcsc": "C25819"})
    t2 = md.read_text(encoding="utf-8")
    assert "47kΩ" in t2 and "C25819" in t2 and "10kΩ" not in t2
    dispatch(p, "set_doc", {"title": "LDO board", "citations": {"U1": "Diodes AP2112 datasheet rev 2 - pin 1 VIN"},
                            "sections": {"U1": "Power", "C1": "Power"}})
    t3 = md.read_text(encoding="utf-8")
    assert t3.startswith("# LDO board") and "Diodes AP2112 datasheet" in t3 and "## SECTION 1: Power" in t3
    # outside edit -> not overwritten, flagged
    md.write_text(t3 + "\nmy own note\n", encoding="utf-8")
    dispatch(p, "set_value", {"ref": "R1", "value": "22k"})
    assert p.md_conflict and "my own note" in md.read_text(encoding="utf-8")
    p.sync_md(force=True)
    assert not p.md_conflict and "22kΩ" in md.read_text(encoding="utf-8")


def test_extract_doc_from_old_md():
    from eagleai.md_writer import extract_doc
    old = """# Fan Controller
PWM driver for a 12 V fan

- 12 V input, 1 A peak
- One fan per board

## Datasheet Citations
- U1: ST L7805 datasheet rev 34 - pin 1 IN, 2 GND, 3 OUT
- Q1 IRLML2502TRPBF: Infineon IRLML2502 datasheet - 1 G, 2 S, 3 D

## COMPLETE BILL OF MATERIALS
| Ref | Qty | Part Number | Value | Description | Package |
|-----|-----|-------------|-------|-------------|---------|
| U1 | 1 | L7805ABD2T-TR | L7805 | 5V regulator | D2PAK |

## SECTION 1: Power Input
### U1 - L7805 regulator
```
Pin 1 (IN): Net VIN ─── C1.Pin1
```
## SECTION 2: Fan Driver
### Q1 - IRLML2502
```
Pin 3 (D): Net DRAIN ─── J1.Pin1
```
Net VIN: U1.Pin1 (IN), C1.Pin1
"""
    d = extract_doc(old)
    assert d["title"] == "Fan Controller" and d["subtitle"].startswith("PWM driver")
    assert d["summary"] == ["12 V input, 1 A peak", "One fan per board"]
    assert d["citations"]["U1"].startswith("ST L7805") and "Infineon" in d["citations"]["Q1"]
    assert d["sections"] == {"U1": "Power Input", "Q1": "Fan Driver"}


def test_adopt_old_md_and_choice():
    p = make_project()
    build(p)
    old = p.folder / "old_design.md"
    old.write_text("# My Old LDO\nFrom the old workflow\n\n- Battery powered\n\n## Datasheet Citations\n"
                   "- U1: Diodes AP2112 datasheet rev 2\n\n## SECTION 1: Regulator\n### U1 - AP2112\n```\nPin 1 (IN): Net VIN\n```\n",
                   encoding="utf-8")
    p.adopt_md(old)
    t = old.read_text(encoding="utf-8")
    assert t.startswith("# My Old LDO") and "Battery powered" in t and "Diodes AP2112 datasheet rev 2" in t
    assert "## SECTION 1: Regulator" in t and "## COMPLETE BILL OF MATERIALS" in t
    assert (p.folder / "old_design.md.orig").exists()
    dispatch(p, "set_value", {"ref": "R1", "value": "33k"})
    assert "33kΩ" in old.read_text(encoding="utf-8") and p.md_path() == old
    # an old project (design with parts, no MD linked) waits for the user's choice
    from eagleai.tools import Project, Settings
    q = Project(p.folder, p.settings)
    q.save_state(md={})
    q2 = Project(p.folder, p.settings)
    assert q2.md_needs_choice
    q2.load_libraries()
    assert "waiting" in q2.sync_md()


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn(); print("ok", n)
