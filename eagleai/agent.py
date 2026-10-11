"""Tool-calling loop shared by every AI brand, with one persistent chat log per project.

Chat log (.eagleai/chat.jsonl) is provider-neutral. When a request starts, past turns
are sent as a COMPACT summary (user text, assistant text, one line per tool call), not
the raw tool results - so switching from Claude to ChatGPT to Grok costs little, and
long projects don't snowball. The design itself always lives in design.json and every
brand can read it with get_design.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .providers import BRANDS, make_provider
from .tools import READ_ONLY_TOOLS, TOOL_DEFS, Project, dispatch

SYSTEM = """You are the design engine inside Eagle AI Design Studio, which builds Autodesk Eagle 7.4 schematics.

How you work:
- You never write Eagle XML or pin names from memory. You call tools; the tools check everything against the real .lbr libraries.
- Workflow: search_library -> get_pinout (if unsure) -> add_parts -> connect_nets -> generate_sch.
- Save round trips: use add_parts / connect_nets to do many edits in ONE call, add_passive for R/C/L (no search
  needed), and issue independent tool calls together in one response. Every edit result already includes
  design_errors, so only call run_checks when you need the warnings too.
- Calculations, file parsing or repetitive work: write a small Python helper with write_script and run it with
  run_script instead of doing it in your head (list_scripts first - a helper may already exist). The user approves
  new scripts before they run.
- If search_library finds no suitable part, make it - don't stop. In order: build_lbr_from_datasheet if a PDF is in
  the datasheets folder; otherwise build_lbr yourself (template type for standard packages using the manufacturer's
  recommended land pattern, 'custom' with pads + graphics for anything else). Read the geometry report, fix every
  problem, rebuild. With web_search you may look up the manufacturer's land pattern / package drawing.
  Tell the user what you assumed (dimensions you weren't given) - parts you create are UNVERIFIED
  until they check them in Eagle.
- Passives need values (R 10k, C 100n). Prefer standard E-series values.
- Ratings (the BOM and MD show the recommended minimum for every part): once the nets exist, set_net_voltage with
  the WORST-CASE max of each supply and high-voltage node (tolerance, car 14.4 V / surge clamp, resonant peaks).
  Then get_ratings; for parts it can't work out (LED / series / sense resistors, tank caps, inductor current) work
  out the real V/W/A and set_stress. Pick packages and voltages that meet the recommendation and write the chosen
  rating into the value or note (C "100n 50V X7R", R "0.25W" or a bigger package). Fix every UNDER-RATED part.
- Every power pin must be on a net. Name nets in CAPS (VIN, GND, 3V3, SDA). Use ONE ground net name.
- Same net name = connected (the schematic uses net labels, not drawn wires).
- Put decoupling capacitors on every IC supply pin.
- When a tool returns an error, fix it using the information in the error (it lists the valid pins). Do not repeat a failing call unchanged.
- Fix design_errors before generate_sch. Before finishing a design, call run_checks once and report warnings you can't fix.
- PCB-trace coils (spiral / racetrack inductors, wireless-power coils): use build_coil - only choose the numbers,
  never draw copper yourself. If it rejects the numbers, its message says what fits.
- Board placement: board_state first. For each IC with a datasheet, layout_rules_from_datasheet, map the roles to
  refs, then place_parts with those rules (+ connectors on edges, keep what the user placed by hand). Explain what
  you asked for and report rules that could not be met. You never write coordinates for MOVE yourself.
- Routing: check design_rules (JLC standard unless the user loaded their own). Set net classes from the real
  currents (power, coil drive, LED strings), route short critical loops first (switch node, gate drive, decoupling),
  consider a ground pour. Report failed nets and the DRC result honestly; suggest moving parts if nets fail.
- The project's Markdown schematic document is written by the studio from the design after every change. When you
  design a schematic, also call set_doc once: title, subtitle, summary bullets, a datasheet citation for every IC and
  active part (manufacturer, document, the pins you relied on) and sections that group the parts by function.
- For purchasing: try jlc_find first (free, local; it puts parts the user already chose in earlier projects first). It only matches exact part numbers and R/C values, so when it
  finds nothing and you have web_search, search jlcpcb.com / lcsc.com yourself. Rules: must fit the footprint
  (say so if a package change is needed), in stock, cheapest at the quantity actually bought (smallest price break,
  NOT reel pricing), Basic preferred when the saving is less than the Extended fee, pin-compatible second sources
  in the same package are fine. Check a number with jlc_lookup, then jlc_assign (it refuses unknown numbers).
  Never assign a number you have not seen on a page or in a tool result. Wire pads, test points and PCB-trace coils
  are not purchasable - suggest marking them DNP. Prices and stock are a snapshot, not a quote.
- Earlier turns are shown as a summary; other AI assistants may have made some of the changes. Trust the tools
  (get_design, run_checks) over the summary.
- If LINKED DESIGNS are listed below, this board works together with them (e.g. a transmitter and its receiver).
  Use their values when you calculate or choose parts here (linked_design for detail), never assume them, and flag
  anything on this board that no longer matches them or the system requirements.
- Be brief. Say what you changed and anything the user must verify."""

REVIEW_SYSTEM = """You are an independent reviewer double-checking an Eagle 7.4 schematic that another AI assistant built.
You can only READ the design (get_design, get_pinout, search_library, run_checks, list_scripts); you cannot change it.
Look for: pins on the wrong net, swapped or misnumbered pins, missing decoupling, missing pull-ups/pull-downs,
floating enable/reset pins, wrong passive values for the stated goal, power rails shorted together, parts that
don't match what the user asked for, and anything the previous assistant claimed but didn't actually do.
Check claims against the tools, not against the conversation summary.
If LINKED DESIGNS or SYSTEM REQUIREMENTS are listed below, also check this board against them (shared values,
interfaces, cross-board checks) - read the linked boards with linked_design.
Answer with a short list: CONFIRMED OK items, then PROBLEMS (each with the exact REF.PIN / net and the fix),
then QUESTIONS for the user. Say clearly if you found nothing wrong."""


def _short(obj, n=160) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) if not isinstance(obj, str) else obj
    return s if len(s) <= n else s[:n] + "…"


class Agent:
    def __init__(self, project: Project):
        self.p = project
        self.cancel = False
        self.log_path = project.meta_dir / "chat.jsonl"
        self.log: List[Dict] = self._load_log()

    # ------------------------------------------------------------- chat log
    def _load_log(self) -> List[Dict]:
        out = []
        if self.log_path.exists():
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out

    def _append(self, entry: Dict):
        entry.setdefault("ts", time.time())
        self.log.append(entry)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def visible_log(self) -> List[Dict]:
        """Entries since the last New chat (for the UI and for the context)."""
        idx = max((i for i, e in enumerate(self.log) if e.get("t") == "reset"), default=-1)
        return self.log[idx + 1:]

    def compact_history(self, max_turns: int = 30, max_chars: int = 2000) -> List[Dict]:
        """Past turns as plain user/assistant text with one line per tool call."""
        turns: List[Dict] = []
        tools_line: List[str] = []
        for e in self.visible_log():
            t = e.get("t")
            if t == "user":
                n = len(e.get("images") or [])
                turns.append({"role": "user", "text": e["text"][:max_chars]
                              + (f"\n[{n} image(s) were attached to this message - not sent again]" if n else "")})
                tools_line = []
            elif t == "tool":
                tools_line.append(f"{e['name']}({_short(e.get('args', {}), 80)}) -> {e.get('result', '')}")
            elif t in ("assistant", "review"):
                who = e.get("by", "")
                head = f"[{'REVIEW by ' if t == 'review' else ''}{who}]"
                body = ""
                if tools_line:
                    body += "Tools used:\n" + "\n".join("- " + x for x in tools_line[-25:]) + "\n"
                body += e.get("text", "")
                turns.append({"role": "assistant", "text": f"{head}\n{body}"[:max_chars * 2]})
                tools_line = []
            elif t == "note":
                turns.append({"role": "user", "text": f"[note] {e['text'][:max_chars]}"})
        if len(turns) > max_turns * 2:
            turns = [{"role": "user", "text": "(older conversation omitted)"}] + turns[-max_turns * 2:]
        return turns

    def new_chat(self):
        self._append({"t": "reset"})

    def note(self, text: str):
        """Record app events (imports, exports) so every AI knows about them."""
        self._append({"t": "note", "text": text})

    # ----------------------------------------------------------------- undo
    def snapshot(self, label: str):
        d = self.p.folder / "history"
        d.mkdir(exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in label[:30])
        (d / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}.json").write_text(self.p.design.to_json(), encoding="utf-8")

    def undo_last(self) -> Optional[str]:
        d = self.p.folder / "history"
        snaps = sorted(d.glob("*.json")) if d.exists() else []
        if not snaps:
            return None
        last = snaps[-1]
        from .model import Design
        self.p.design = Design.load(last)
        last.unlink()
        self.p.changed()
        return last.name

    # ------------------------------------------------------------ estimate
    def estimate_next(self, text: str, brand: Optional[str] = None, model: Optional[str] = None,
                      images: Optional[List[Dict]] = None) -> float:
        from .images import tokens
        from . import linked
        hist = json.dumps(self.compact_history())
        try:
            extra = len(linked.context(self.p))
        except Exception:
            extra = 0
        chars = len(SYSTEM) + len(json.dumps(TOOL_DEFS)) + len(text) + len(hist) + extra
        pics = sum(tokens(a) for a in images or [])
        return self.p.costs.estimate(model or self.p.settings.model, int(chars / 3.5) + pics, 1500)

    # ---------------------------------------------------------------- run
    def run(self, user_text: str, on_text: Callable[[str], None] = lambda s: None,
            on_tool: Callable[[str, Dict, Dict], None] = lambda n, a, r: None,
            brand: Optional[str] = None, model: Optional[str] = None, review: bool = False,
            max_steps: int = 25, provider=None, images: Optional[List[Dict]] = None) -> str:
        brand = brand or self.p.settings.brand
        model = model or self.p.settings.model
        prov = provider or make_provider(brand, model)
        if prov is None:
            raise RuntimeError(f"No API key for {BRANDS[brand][0]} - add it in Settings")
        self.cancel = False
        by = f"{BRANDS[brand][0]} {prov.model}"
        if not review:
            self.snapshot("before_" + user_text[:20])
        from . import linked
        try:
            for msg in linked.changes(self.p):
                self.note(msg)
            link_ctx = linked.context(self.p)
        except Exception as exc:          # a broken link must never block the chat
            link_ctx = f"(linked designs could not be read: {exc})"
        entry = {"t": "user", "text": user_text, "to": by, "review": review}
        if images:
            from .images import save
            paths = save(images, self.p.meta_dir / "chat_images")
            entry["images"] = [str(Path(x).relative_to(self.p.folder)) if Path(x).is_relative_to(self.p.folder)
                               else x for x in paths]
            native = prov.start(self.compact_history(), user_text, images)
        else:
            native = prov.start(self.compact_history(), user_text)
        self._append(entry)
        system = (REVIEW_SYSTEM if review else SYSTEM) + f"\n\nProject: {self.p.design.name}" \
            + (f"\n\n{link_ctx}" if link_ctx else "")
        st = self.p.state()
        hide = set() if st.get("linked") else {"linked_design"}
        if not st.get("system_doc"):
            hide.add("update_system_doc")
        tools = [t for t in TOOL_DEFS if (t["name"] in READ_ONLY_TOOLS or not review) and t["name"] not in hide]
        allowed = {t["name"] for t in tools}
        texts: List[str] = []
        stop_note = ""
        for _ in range(max_steps):
            if self.cancel:
                stop_note = "[stopped]"
                break
            if self.p.costs.over_budget():
                stop_note = f"[session budget ${self.p.costs.budget_usd:.2f} reached - raise it in Settings to continue]"
                break
            st = prov.step(system, tools, native, on_text)
            self.p.costs.record(prov.model, st.usage, label=("review " if review else "chat ") + brand,
                                extra_usd=float(st.usage.get("extra_usd", 0) if isinstance(st.usage, dict) else 0))
            if st.text:
                texts.append(st.text)
            if st.done:
                break
            results = []
            for cid, name, args in st.calls:
                if name not in allowed:
                    out = {"error": f"{name} is not available here"}
                else:
                    out = dispatch(self.p, name, args)
                on_tool(name, args, out)
                res_short = ("ERROR " + _short(out.get("error"), 100)) if "error" in out else \
                    (f"{len(out['errors'])} errors: " + _short(out["errors"][0], 80) if out.get("errors") else "ok")
                self._append({"t": "tool", "name": name, "args": args if len(json.dumps(args)) < 400 else
                              {"_": _short(args, 200)}, "result": res_short, "by": by})
                results.append((cid, json.dumps(out, ensure_ascii=False, separators=(",", ":"))[:12000],
                                "error" in out))
            prov.add_results(native, results)
        else:
            stop_note = "[stopped: step limit reached]"
        final = "\n".join(texts).strip()
        self._append({"t": "review" if review else "assistant", "text": final + (("\n" + stop_note) if stop_note else ""),
                      "by": by})
        return final + (("\n" + stop_note) if stop_note else "")
