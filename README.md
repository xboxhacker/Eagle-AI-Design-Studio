# Eagle AI Design Studio

An AI-assisted design studio for **Autodesk Eagle 7.x** schematics on Windows. Describe the circuit you want in a
chat; the AI (Claude, ChatGPT or Grok) builds it from the parts that really exist in your Eagle libraries, and the
studio writes a valid Eagle `.sch`, a Markdown schematic document, a live BOM with JLCPCB part numbers and prices,
and JLC assembly files.

The AI never writes Eagle XML or pin names from memory: every edit goes through tools that check it against your
real `.lbr` files, so a wrong pin is an error the AI has to fix, not a broken schematic.

> Status: actively developed, used on real boards. Always open the generated files in Eagle and run ERC/DRC before
> ordering anything. Footprints built from datasheets are marked UNVERIFIED until you check them.

---

## Features

**Design**
- Chat-driven schematic design with tool calling: library search, pinouts, batch add/connect, checks after every edit.
- Three AI brands with a shared chat per project - switch between Claude, ChatGPT and Grok at any time.
- **Double-check / Review**: a second AI inspects the design read-only and lists problems.
- Paste screenshots or drop images into the chat.
- Undo for every AI edit (design snapshots).

**Files**
- Writes Eagle 7.4 `.sch` files (net-label style); never overwrites a schematic you edited in Eagle.
- Imports an existing `.sch` (pure code, uses the libraries embedded in the file) or a Markdown schematic.
- Keeps a **Schematic MD** document in sync with the design: summary, datasheet citations, BOM, one pin block per
  part, net summary.
- Opening a project folder finds and loads its `.sch` and Schematic MD together.

**Parts and libraries**
- Scans your Eagle library folders (with sub-folders); converts old binary libraries with Eagle 7.4.
- **Datasheet → part**: builds an Eagle library part from a PDF (only the pinout/package pages are sent).
- Library zip import (Ultra Librarian etc.), keeping the Fusion 360 and STEP files alongside.
- PCB-trace coil generator (racetrack or round spirals) as library parts.

**BOM, ratings and purchasing**
- Live BOM, editable in place, always rebuilt from the design; checks exported files for drift.
- **Recommended ratings**: resistor watts, capacitor minimum voltage and inductor current, worked out from net
  voltages and part stress, derated to standard ratings. Under-rated parts are flagged.
- JLCPCB/LCSC part numbers: free lookup for exact part numbers and R/C values, optional AI web search, and a memory
  of parts you used before. Prices and stock shown per board.
- JLC assembly export: BOM + CPL (top, bottom or both).

**Board**
- Eagle link (an Eagle ULP): send the open board to the studio with F12 and apply placement back.
- Rule-based placement from datasheet layout guidance, a simple router for small 2-layer boards, design rules
  (JLC presets or your own `.dru`).

**Multi-board projects**
- **Linked designs**: boards that work together stay separate projects, but the AI sees a summary of the linked
  boards, can read their details on demand, and is told when they change. An optional shared system-requirements
  file holds the values both boards must agree on.

**Tools**
- Calculators: R/C series-parallel (with best standard pairs), LED resistor, capacitor units and codes, Ohm's law,
  dividers and regulator feedback, RC/LC, SMD resistor codes, trace width (IPC-2221).
- The AI can write small local Python helper scripts for calculations; you approve each one before it runs.
- Cost tracking per message, per-message estimate, session budget cap and prompt caching.

---

## Requirements

- Windows 10 or 11
- Python 3.12 or 3.13 (64-bit) from [python.org](https://www.python.org/downloads/windows/)
- Autodesk Eagle 7.x (to open the files; the studio itself only needs your `.lbr` library folders)
- An API key for at least one of: [Anthropic (Claude)](https://console.anthropic.com/),
  [OpenAI (ChatGPT)](https://platform.openai.com/), [xAI (Grok)](https://console.x.ai/)

## Install and run

```bat
git clone https://github.com/xboxhacker/Eagle-AI-Design-Studio.git
cd Eagle-AI-Design-Studio
run.bat
```

`run.bat` finds Python, creates a `.venv`, installs `requirements.txt` (again only when it changes) and starts the
app. To run it by hand:

```bat
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python run.py
```

## First steps

1. **Settings** → paste your API key(s), check the Eagle library folders (auto-detected from the usual Eagle
   install and Documents folders), and set a session budget.
2. **Project ▾ → New / Open project folder** - an empty folder for a new design, or a folder that already has a
   `.sch` and/or Schematic MD.
3. Describe what you want in the chat, e.g. *"5 V 1 A buck converter from 12 V input with an LED power indicator"*.
4. Review the preview, the **Problems** list and the **BOM** tab; ask for changes.
5. **Generate .sch** and open it in Eagle (**Open in Eagle**).

Useful toolbar buttons: **Check** (F5), **BOM (live)** (Ctrl+B), **Calculators**, **Linked designs…**,
**Design rules…**, **Datasheet → part…**, **Price BOM**, **Export JLC BOM+CPL…**.

---

## How things work

### Project folder

```
my-board/
  design.json          the design (parts, nets, net voltages) - the source of truth
  <name>.md            the Schematic MD, rewritten after every change
  <name>.sch           generated Eagle schematic
  datasheets/          PDFs for "Datasheet -> part"
  libs/                library parts the studio created
  jlc/                 JLC BOM / CPL exports
  history/             snapshots for Undo
  .eagleai/            chat log, chat images, cache, project state (links, prices)
```

### Schematic MD
A Markdown document generated from the design (never typed by the AI, so it can't drift). If you edit it outside
the studio, the **Schematic MD** tab turns red and offers **Import edits from file** or **Overwrite with design**.
Adopting an older, hand- or AI-written MD keeps its title, summary, citations and section names and saves a
`.orig` backup.

### Part ratings
- **Net voltages**: the worst-case maximum of each net (Nets tab, double-click *Max V*). Names like `3V3`, `+12V`,
  `VCC5` and `GND` are guessed until set.
- **Stress**: what a part really sees where the nets can't tell (an LED resistor's current, a resonant capacitor's
  peak voltage, an inductor's current) - BOM column *Sees (worst case)*, e.g. `60mW`, `20mA`, `28V`.
- **Derating**: ceramic capacitors 2×, C0G/film 1.5×, electrolytic 1.25×, tantalum 2×, resistor power 2×,
  inductor current 1.3×, rounded up to standard ratings.
- Write the chosen rating into the value or note (`100n 50V X7R`, `0.25W`); otherwise a resistor's package size
  is used (0603 = 1/10 W).

### Linked designs
**Linked designs…** links another board's project folder (or its `.sch` / MD), normally in both directions. Links
are stored in `.eagleai/state.json` with relative paths, so the folders can move together. Linked boards are read
only; open that project to edit it.

### JLCPCB part numbers
1. **Fill missing LCSC + prices** - free: exact part numbers, resistors and capacitors (value, size, voltage).
2. **Find missing with AI (web)** - the AI searches jlcpcb.com / lcsc.com; every number is re-checked before you
   apply it.
3. **Paste LCSC list…** - paste `ATTRIBUTE C1 LCSC 'C15849';` lines or a table.

Parts you confirmed are remembered for future projects (**My JLC parts…**). Prices are a snapshot, not a quote.
For the most reliable data, export JLCPCB's parts CSV and set it in Settings.

### Eagle link and placement
1. **Eagle link…** → copy the `ASSIGN` line → paste it into Eagle's command line once.
2. In Eagle, **F12** sends the open board to the studio.
3. Ask the AI to place the parts; review the preview and choose **Send to Eagle** or **Write the .brd**.

### Saving tokens
Most work is done in code at zero tokens (imports, passive part picking, checks, ratings). The AI uses batch
tools, compact results and prompt caching; previous turns are sent as a short summary. **New chat** keeps the
design and drops the conversation. The status bar shows the running cost and cache savings.

---

## Where data is stored

| What | Where |
|------|-------|
| API keys | Windows Credential Manager (via `keyring`, service `eagleai-ide`) |
| Settings | Registry, `HKCU\Software\EagleAI\IDE` |
| Converted libraries, part and JLC memory, helper scripts, JLC rotation fixes | `Documents\EagleAI\` |
| Per-project data | the project folder (see above) |
| Diagnostic logs (`dialog_debug.log`, `faulthandler.log`, `launcher.log`, `crash.log`) | the app folder (git-ignored) |

What leaves your PC: your messages, the design (as compact text), images you attach and datasheet pages you send
go to the AI provider you picked. Part lookups go to JLCPCB/LCSC search. Nothing else is uploaded.

## File dialogs

By default Windows file dialogs run in a small helper process, so a misbehaving Explorer add-on can't crash the
studio. If a dialog hangs, use **Use built-in dialog instead**, or switch permanently in
**Settings → File dialogs**. **About → Crash report** names the add-on involved.

## Running the tests

The tests are plain scripts (no pytest needed) and run headless:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$env:PYTHONIOENCODING = "utf-8"
Get-ChildItem tests\test_*.py | ForEach-Object { .venv\Scripts\python $_.FullName }
```

`test_openai_wire.py` and `test_tokens.py` additionally need `httpx` and `reportlab`.

## Known limitations

- Targets Eagle 7.x file formats. Fusion 360 opens Eagle 7 `.sch` / `.brd` / `.lbr` files directly.
- No `.brd` generation from scratch, ERC/DRC/Gerber export through Eagle, or supply symbols yet.
- The router is pure Python and meant for small and medium 2-layer boards.
- JLC prices and stock are snapshots; the free search only matches exact part numbers and R/C values.

## Contributing

Bug reports and pull requests are welcome. When reporting a crash, include the text from **About → Crash report**
(it lists the relevant log lines). See [CHANGELOG.md](CHANGELOG.md) for the version history.
