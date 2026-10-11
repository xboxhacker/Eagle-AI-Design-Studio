"""Find JLC/LCSC part numbers with Claude + web search, then verify every answer locally.

The free jlcsearch API only matches whole MPNs / LCSC numbers, so value-only lines ("12nF", "100uH")
and second-source parts need a real search. Claude searches jlcpcb.com / lcsc.com the way you would by
hand; the app then looks every proposed LCSC number up again (jlc.lookup) so a typo or a made-up number
never reaches the BOM unflagged.

Also: parse_lcsc_list() - turns pasted text (Eagle ATTRIBUTE lines, a table, "R1, R2: C22548") into
{ref: lcsc} so results from a chat can be applied without retyping.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional

from . import jlc

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search",
                   "allowed_domains": ["jlcpcb.com", "lcsc.com", "www.lcsc.com"]}
WEB_SEARCH_USD = 0.01        # $10 per 1,000 searches (Anthropic pricing, checked 2026-10-08)

PROMPT = """You are sourcing parts for a JLCPCB assembly order. For every BOM line below, find the JLC/LCSC part
number (C followed by digits) to use. Search jlcpcb.com and lcsc.com (product pages are
https://www.lcsc.com/product-detail/C<number>.html and https://jlcpcb.com/partdetail/C<number>).

Rules - follow all of them:
1. The part must fit the line's footprint (package / size given). If nothing in stock fits, you may propose a
   different package but set "package_change" to the new package and say why.
2. In stock at JLC/LCSC, with stock >= the quantity needed.
3. Lowest cost at the quantity actually bought ({build_qty} boards) - use the smallest price break you can buy,
   NOT reel / full-roll pricing. A Basic part avoids JLC's per-order Extended-part fee (about ${fee}); prefer
   Basic when the price difference is smaller than that fee.
4. A cheaper pin-compatible second source in the same package is fine (say so in "why").
5. Electrical fit: at least the "min_rating" given (worked out from the circuit, already derated) and at least
   any voltage in the value/note; capacitors 16 V if nothing is given, same dielectric if given;
   resistors same value and at least the watts; inductors same inductance and enough current; semiconductors same function, ratings
   at least as good, same pinout.
6. Lines that are not purchasable parts (wire pads, test points, PCB-trace coils, mounting holes, fiducials,
   logos) - set "skip" to the reason, no LCSC.
7. Never guess a number. If you can't confirm it on a page, leave "lcsc" empty and explain in "why".

BOM lines (JSON):
{lines}

When done, reply with ONLY this JSON (no other text):
{{"parts":[{{"refs":"C1","lcsc":"C15849","mfr_part":"CL10A105KB8NNNC","manufacturer":"Samsung","package":"0603",
"basic":true,"unit_price":0.0145,"stock":73800,"package_change":"","skip":"","why":"1uF 50V X5R, Basic"}}]}}"""


def _lines_json(lines: List[Dict]) -> str:
    keep = ("refs", "qty", "value", "library_part", "package", "size", "mpn", "note", "min_rating")
    return json.dumps([{k: l.get(k) for k in keep if l.get(k)} for l in lines], ensure_ascii=False, indent=0)


def _extract_json(text: str) -> Dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("the AI did not return a parts list")
    return json.loads(m.group(0))


def ask_claude(lines: List[Dict], key: str, model: str, build_qty: int, fee: float,
               on_status: Callable[[str], None] = lambda s: None, max_searches: Optional[int] = None):
    """Returns (parts, usage dict, searches_used). Runs pause_turn continuations."""
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    tool = dict(WEB_SEARCH_TOOL, max_uses=max_searches or min(4 * len(lines) + 2, 40))
    msgs = [{"role": "user", "content": PROMPT.format(build_qty=build_qty, fee=f"{fee:.2f}", lines=_lines_json(lines))}]
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    searches, text = 0, ""
    for _ in range(8):
        resp = client.messages.create(model=model, max_tokens=8000, tools=[tool], messages=msgs)
        u = resp.usage
        for k in usage:
            usage[k] += getattr(u, k, 0) or 0
        stu = getattr(u, "server_tool_use", None)
        searches += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        on_status(f"searched {searches}x…")
        text = "".join(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason == "pause_turn":          # long server-tool turn: send it back to continue
            msgs = msgs + [{"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]}]
            continue
        break
    return _extract_json(text).get("parts", []), usage, searches


def verify(proposals: List[Dict], client, lines_by_refs: Dict[str, Dict], qty_needed: Dict[str, int]) -> List[Dict]:
    """Look every proposed LCSC number up again. Adds verified / problem fields."""
    out = []
    for pr in proposals:
        row = dict(pr)
        refs = str(pr.get("refs", ""))
        line = lines_by_refs.get(refs, {})
        row["verified"], row["problem"] = False, ""
        lc = str(pr.get("lcsc") or "").strip().upper()
        if pr.get("skip") or not lc:
            row["problem"] = pr.get("skip") or "no part found"
            out.append(row)
            continue
        if not re.fullmatch(r"C\d+", lc):
            row["problem"] = f"'{lc}' is not an LCSC number"
            out.append(row)
            continue
        row["lcsc"] = lc
        try:
            c = client.lookup(lc)
        except Exception as exc:
            row["problem"] = f"could not check ({str(exc)[:60]})"
            out.append(row)
            continue
        if c is None:
            row["problem"] = "JLC does not list this number"
            out.append(row)
            continue
        need = qty_needed.get(refs, 1)
        row.update(mfr_part=c.mfr or pr.get("mfr_part", ""), stock=c.stock, package=c.package or pr.get("package", ""),
                   unit_price=c.unit_price(need), basic=c.basic or c.preferred, verified=True)
        probs = []
        if c.stock < need:
            probs.append(f"only {c.stock} in stock")
        size = line.get("size")
        if size and size not in (c.package or "") and size not in (c.description or ""):
            probs.append(f"package {c.package} - footprint is {size}")
        if pr.get("package_change"):
            probs.append(f"needs package change to {pr['package_change']}")
        row["problem"] = "; ".join(probs)
        out.append(row)
    return out


def parse_lcsc_list(text: str, known_refs) -> Dict[str, str]:
    """Pull {ref: 'C12345'} out of pasted text. Understands Eagle ATTRIBUTE lines, markdown/CSV tables and
    'R1, R2: C22548'. Only refs that exist in the design are returned."""
    known = {r.upper() for r in known_refs}
    out: Dict[str, str] = {}
    for m in re.finditer(r"ATTRIBUTE\s+([A-Z]+\d+)\s+(?:LCSC\w*|JLC\w*)\s+'?(C\d+)'?", text, re.I):
        if m.group(1).upper() in known:
            out[m.group(1).upper()] = m.group(2).upper()
    for line in text.splitlines():
        if re.search(r"ATTRIBUTE", line, re.I):
            continue
        nums = [n for n in re.findall(r"\bC\d{3,}\b", line) if n.upper() not in known]
        if len(set(nums)) != 1:
            continue
        refs = [r.upper() for r in re.findall(r"\b([A-Z]{1,3}\d{1,4})\b", line)
                if r.upper() in known and r.upper() != nums[0].upper()]
        for r in refs:
            out.setdefault(r, nums[0].upper())
    return out
