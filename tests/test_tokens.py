import os, sys, tempfile, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_engine as te
from eagleai.lbr_builder import build_lbr, select_datasheet_pages
from eagleai.tools import Project, Settings, dispatch, tool_import_apply_nets
from eagleai.agent import Agent

tmp = Path(tempfile.mkdtemp())
lib = tmp / "lbr"
for s in (te.BUCK, te.SOT, te.RES): build_lbr(s, lib)
st = Settings(); st.lib_dirs = [str(lib)]; st.converted_dir = str(tmp / "conv"); st.memory_path = str(tmp / "mem.json")

# 1) markdown import fully local
md = tmp / "p" / "x.md"; md.parent.mkdir()
md.write_text("""# T
## COMPLETE BILL OF MATERIALS
| Ref | Qty | Part Number | Value | Description | Package |
|---|---|---|---|---|---|
| U1 | 1 | LDO5-ADJ | LDO5 | regulator | SOT-23-5 |
| R1, R2 | 2 | Generic | 10kΩ | 10kΩ | 0603 |
### U1 - LDO5
```
Pin 1 (IN): Net VIN ─── R1.Pin1
Pin 2 (GND): Net GND ─── R2.Pin2
Pin 5 (OUT): Net VOUT ─── R2.Pin1
```
Net GND: U1.Pin2, R1.Pin2, R2.Pin2
""", encoding="utf-8")
p = Project(md.parent, st); p.load_libraries()
parsed = p.import_md(md)
done, left = p.auto_place_bom(parsed["bom"]); print(done, left)
assert not left and p.design.parts["R1"].value == "10k"
r = tool_import_apply_nets(p); print(r); assert not r["errors"]
print("memory:", json.loads(Path(st.memory_path).read_text()))

# 2) batch tools + add_passive + compact outputs + auto checks
p2 = Project(tmp / "p2", st); p2.load_libraries()
print(dispatch(p2, "add_parts", {"parts": [{"library": "BUCK16", "deviceset": "BUCK16"}, {"library": "NOPE", "deviceset": "X"}]}))
print(dispatch(p2, "add_passive", {"kind": "C", "value": "100nF", "package": "0603", "count": 2}))  # no C lib -> error
print(dispatch(p2, "add_passive", {"kind": "R", "value": "4.7kΩ", "package": "0603", "count": 2}))
print(dispatch(p2, "connect_nets", {"nets": {"FB": ["U1.FB", "R1.1"], "X": ["R1.1"]}}))
print(dispatch(p2, "get_pinout", {"library": "BUCK16", "deviceset": "BUCK16"}))
print(dispatch(p2, "search_library", {"query": "ldo"}))
print(dispatch(p2, "get_design", {}))

# 4) datasheet page selection
from reportlab.pdfgen import canvas
pdf = tmp / "ds.pdf"; c = canvas.Canvas(str(pdf))
for i in range(30):
    words = {3: "Pin Configuration and Functions pin description", 25: "Mechanical data package outline land pattern recommended footprint",
             2: "Ordering information package option"}.get(i, "Electrical characteristics typical performance graph " * 3)
    for k in range(12): c.drawString(40, 800 - 14 * k, f"page {i+1} " + words)
    c.showPage()
c.save()
data, pages = select_datasheet_pages(pdf, "TPS1"); print("pages sent:", pages, len(data), "bytes of", pdf.stat().st_size)
assert {1, 2, 3, 4, 26}.issubset(pages) and len(pages) <= 10
print("ALL OK")
