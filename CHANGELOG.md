# Changelog

## 0.9.4 - 2026-10-10
- Recommended ratings in the MD and the BOM: resistor watts, capacitor minimum voltage, inductor current.
  Worked out from worst-case net voltages (set by the AI or in the Nets tab; 3V3 / +12V / VCC9 / GND are guessed
  from the name until set) or from what a part really sees (V / W / A, set by the AI or typed in the BOM's
  "Sees (worst case)" column), then derated and rounded up to a standard rating: ceramic caps 2x (DC bias), C0G
  and film 1.5x, electrolytic 1.25x, tantalum 2x; resistor power 2x; inductor current 1.3x. A resistor from a
  supply to an unknown node uses the worst case (the other end at 0 V).
- The rating chosen (a voltage or wattage in the value/note, or the resistor's package size) is checked: Check
  and run_checks list UNDER-RATED parts, resistors above their package's working voltage, and nets whose voltage
  is still needed.
- MD: "Rating (min)" column in the BOM table, a "Rating:" line under each part (how it was worked out), and the
  net voltages in the summary. BOM: "Sees (worst case)" (editable) and "Rating (min)" (tooltip shows the working).
- JLC: "Fill missing LCSC" only offers capacitors at or above the recommended voltage (16 V if unknown, as
  before); "Find missing with AI" gets the minimum rating of each line.
- AI tools set_net_voltage, set_stress and get_ratings; the AI is told to fill them in and fix under-rated parts.

## 0.9.3 - 2026-10-10
- Linked designs: toolbar **Linked designs…** links other boards that work with the open one (project folder,
  design.json, .sch or schematic MD), and links back from the other studio project. The AI gets a short card of
  each linked board in its system prompt (cached by Claude) and a read-only `linked_design` tool for parts, nets,
  one part's pins, or an MD section. The dialog shows exactly what the AI gets and roughly how many tokens.
- Change notices: when a linked board changed since this project last saw it (values, parts added/removed, net
  connections, its summary), the chat shows what changed and the AI is told, on opening and before each message.
- Shared system requirements file (template: boards, shared values, interfaces, cross-board checks) used by all
  linked projects; sent to the AI, editable by it with `update_system_doc` (backup .bak). Reviews check against
  linked boards and the requirements. Projects without links get no extra tools or prompt text.

## 0.9.2 - 2026-10-10
- "Undo AI edit" moved from the toolbar to the chat (next to Image… and New chat); Ctrl+Z still works. It now
  waits while the AI is working instead of undoing under it.
- New toolbar button **Calculators** (a separate window, results update as you type, can be copied):
  resistors in series/parallel (+ the best two standard resistors for a target), capacitors in series/parallel
  (+ best pair), LED resistor (strings of LEDs in series, strings in parallel, next standard value up, dissipation
  and resistor size, total current and efficiency), capacitor units and the 3-digit code (104 = 100 nF, both ways),
  Ohm's law (any two of V/I/R/P), voltage divider and regulator feedback (best E24/E96 pairs for a target),
  RC / LC (cutoff, time constant, R for a cutoff, resonance), SMD resistor codes (103, 1002, 4R7, EIA-96 01C - both
  ways), and trace width from current (IPC-2221, same formula as the router).
  Values accept 10k, 4k7, 4R7, 100n, 0.1u, 2.2M.

## 0.9.1 - 2026-10-10
- Images in the AI chat: paste a screenshot or copied picture into the message box (Ctrl+V), drop image files on
  it, or use "Image…". Thumbnails show above the box (remove any before sending); up to 10 per message. Works with
  Claude, ChatGPT and Grok (the model must accept images). Pictures are scaled to 1568 px on the long side (what
  Claude recommends; larger only costs more), saved in .eagleai/chat_images and shown again when the chat is
  reopened. Later messages only mention them, so a picture is paid for once. The cost estimate includes them.
- Copying from Word / Excel still pastes text (those copies carry a picture of the text too).

## 0.9.0 - 2026-10-10
- Opening a project folder loads the .sch AND the Schematic MD: the .sch becomes the design (pins exactly as in
  Eagle), the MD is linked to it (title, summary, citations and sections kept, .orig backup, kept up to date). No
  extra "<name>_studio.md" copy. Several .sch / MD files: one dialog with both picks, pre-selected (the MD with the
  same name as the .sch, e.g. v2.sch + v2.md; else the newest in the folder). An existing design without a linked
  MD links the one it finds instead of asking. No .sch: the MD builds the design as before.
- Preview: mouse-wheel zoom towards the pointer, drag (left or middle button) to pan, double-click / Fit / F to fit,
  + and - keys. Stays sharp at any zoom, and keeps your zoom and position when the design changes. Same viewer in
  the placement / routing previews.
- AI chat: text can be selected and copied while the AI is writing (new text no longer clears your selection);
  right-click -> "Copy all chat". The chat / tabs divider goes much further both ways (narrow ~240 px to almost
  the full window; pages with long button rows scroll sideways) and its position is remembered.

## 0.8.7 - 2026-10-10
- Fix: the studio could still crash while closing (Windows logged an access violation in pyside6.abi3.dll every
  time). The 0.8.6 "end with TerminateProcess" never worked - ctypes passed the process handle as a 32-bit number,
  Windows refused it, and Python went on into the normal exit that crashes. The handle types are now declared.
- Windows' event log showed the file-dialog problem is a FREEZE, not a crash: the Windows Open dialog stopped
  responding (in the studio, in its helper, and in Notepad). That is an Explorer add-on hanging, system-wide.
  The "File dialog is open" box now has "Use built-in dialog instead": stops the frozen helper and shows the
  built-in dialog. Closing the box with X or Esc now stops the helper too (before, it kept running in the
  background and was reported as a crash).
- Crash report: also lists freezes ("stopped interacting with Windows") of the studio and of other programs, so
  you can see whether other programs' dialogs freeze too. After a session that died in a dialog, the start-up
  message also offers "Use built-in dialogs".
- requirements.txt: tested version ranges (PySide6-Essentials instead of the full PySide6, which also pulled in
  the large WebEngine). run.bat prefers Python 3.13 / 3.12.

## 0.8.6 - 2026-10-09
- Your faulthandler.log showed the "access violations" happened while the studio was CLOSING, with background
  jobs still running (crash report, file-dialog helper) - Qt crashed while its DLLs were being unloaded. The studio
  now ends with TerminateProcess on Windows (no DLL unloading), so closing it can't crash any more.
- File dialog helper: a small "File dialog is open" box with "Cancel dialog" replaces the greyed-out main window,
  so the studio never looks frozen; the helper's dialog is forced to the front (AllowSetForegroundWindow + a
  focused owner window).
- Crash report: shows the studio's logs instantly; Windows' crash records are read with wevtutil (built in,
  fast) instead of PowerShell, with time limits - no more endless "collecting…". Includes PySide6/Qt versions
  and crash.log.

## 0.8.5 - 2026-10-09
- Crash-safe Windows file dialogs (new default): the normal Windows dialog is shown by a small helper process
  (native_dialog_child.py, Tk's Explorer-style dialog). Explorer add-ons load into the helper, not the studio,
  so a broken one can no longer take the app down. If the helper crashes, the studio logs its exit code
  (e.g. 0xc0000005), shows the built-in dialog for that one time and tells you. Python without Tk: the in-app
  Windows dialog is used.
- Settings -> File dialogs: crash-safe Windows dialogs / Windows dialogs inside the app / built-in Qt dialogs.
- Your faulthandler.log (v0.8.3) showed an access violation while the app was shutting down, after Explorer
  code had been loaded into it - consistent with a broken Explorer add-on. With the helper, none is loaded.

## 0.8.4 - 2026-10-09
- Windows' own file dialogs are back as the default (Settings switch kept for the built-in ones).
- Crash tracing for file dialogs: every dialog writes OPEN/CLOSE to dialog_debug.log (flushed to disk before
  it opens). If the studio dies inside one, the next start says so and offers the crash report, or a switch to
  the built-in dialogs.
- Crash report (About -> Crash report…): reads Windows' own crash records for Python (Event Viewer ->
  Application, event 1000) and names the DLL that crashed, with its maker (e.g. a cloud-drive or archiver
  Explorer add-on), plus the dialog trace and faulthandler.log. Copy all / Open Event Viewer / Open log folder.

## 0.8.3 - 2026-10-09
- Fix: the app could crash when opening a folder/file dialog (e.g. choosing a new project folder). Windows' own
  dialog loads Explorer add-ons (cloud drives, archivers, git tools…) into the app, and a broken one takes it
  down. All file and folder dialogs now use Qt's built-in dialog. Settings -> "Use Windows' own file dialogs"
  switches back if you prefer.
- faulthandler.log now keeps earlier sessions (appended, trimmed when large), so a hard crash is still recorded
  after you restart the app.

## 0.8.2 - 2026-10-09
- Overwrite check for everything the studio writes into your folders. Before an existing file is replaced you
  choose: Save as new (name_v2, _v3…), Overwrite (the old file is first copied to .eagleai/backups/ with a
  date-time stamp) or Cancel. "Don't ask again for this file" lasts until the studio is closed.
  Covers: Generate .sch (also by the AI), JLC BOM + CPL (one question for the pair, matching _vN names),
  library parts (replacing a part of the same name in an existing .lbr asks; adding a new part to it makes a
  backup), Ultra Librarian imports, converted / repaired libraries, design_rules.dru when it isn't the
  studio's own copy. AI helper scripts keep their previous versions in scripts/_history.
- Generate .sch warns when the file was changed in Eagle since the studio wrote it, when the studio didn't write
  it, and when a .brd of the same name exists (a new schematic breaks Eagle's board/schematic link).
- Writing a .brd (placement / routing) says "Overwrite <name>.brd (backup kept)" on the button.
- Without a window (tests, scripts) nothing is ever overwritten - a new numbered file is written instead.

## 0.8.1 - 2026-10-09
- The Schematic MD follows its format strictly (checked by a test): Datasheet Citations right after
  the summary; exact COMPLETE BILL OF MATERIALS heading and columns; Package as a footprint token (0603, SOT-23-5,
  SOIC-8, DO-214AA, DPAK); capacitors marked Ceramic/Electrolytic; ### REF - Description followed directly by the pin
  block; every non-passive pin as "Pin N (name)" (NC pins too); ONE target per pin line, the same connection on
  both sides; Net summary lines in their own SECTION; ASCII only except ─ Ω µ.
- The file starts with a note: "Created with Eagle AI Design Studio vX.Y.Z - kept in sync with design.json
  (last updated ...)".
- Open older MD files ("Open MD file…" in the Schematic MD tab): link one to the project (its title, subtitle,
  summary, citations and section names are kept, a .orig backup is saved once, then it's kept up to date) or import
  it into the design. An MD you import a design from automatically becomes the project's MD.
- Old projects (a design but no MD linked): on opening, the studio asks whether to open an existing MD for it;
  if not, it creates a new one.
- The MD is never rewritten while an import is still in progress, and never replaced by an empty document.

## 0.8.0 - 2026-10-09
- Schematic MD: the project's Markdown schematic document (Schematic MD format - title,
  Datasheet Citations, COMPLETE BILL OF MATERIALS, SECTION blocks with Pin N (name): Net X ─── Ref.PinN, and the
  Net summary) is written from the design by code after EVERY change - AI edits, BOM edits (value, MPN, LCSC,
  note, DNP), re-imports from Eagle. It re-imports cleanly (tested round trip).
- The AI adds the prose with set_doc: title, subtitle, summary, datasheet citations per IC, sections.
- "Schematic MD" tab: rendered reader (or raw), find, copy, open the file. Green = matches the design.
  If the file is edited outside the studio it is never overwritten silently: red banner, then "Import edits
  from file" (updates the design) or "Overwrite with design".
- The MD is <design name>.md in the project (or <name>_studio.md if a different MD already has that name).

## 0.7.1 - 2026-10-08
- Every trace that meets a pad now ends exactly on the pad's centre (Eagle's airwire point), on the right layer.
- Branches no longer end in the middle of another trace: the trace is split at the junction, so every joint is
  an endpoint-to-endpoint connection Eagle recognises (no leftover airwires).
- New check after every route: a pad without a trace ending at its centre, or a trace end that connects to
  nothing, is reported as a DRC error.

## 0.7.0 - 2026-10-08
- Design rules per project ("Design rules…"): presets from JLCPCB's published capabilities (JLC standard
  0.2/0.2 mm, 0.3/0.6 mm vias, 0.5 mm edge; JLC minimum 1 oz; JLC 2 oz; JLC 4 layer), load any Eagle .dru, edit
  the numbers (checked against the fab limits), export a .dru for Eagle's DRC. Saved as design_rules.dru too.
- AI routing (route_board): 2-layer A* router in code with 45-degree traces, vias (never in SMD pads), net classes
  with widths from current (IPC-2221), routing order, rip-up and retry, optional ground pour. Every result is
  checked again with exact geometry (clearance + board edge) before you see it.
- Preview with traces and vias, then Send to Eagle (F12) or write the .brd (rules written into it, backup kept).
- Placement now leaves 1 mm between parts by default (room for traces); decoupling-type parts may sit closer.
- Eagle link dump now includes existing traces/vias and pad types (existing routing is kept and avoided).
- App exit no longer risks a Qt teardown crash.

## 0.6.0 - 2026-10-08
- Eagle link: studio.ulp (Documents\EagleAI\eagle_link) bound to F12 with one ASSIGN command ("Eagle link…").
  Each press sends the open board/schematic to the studio and runs commands the studio queued; then re-syncs.
- Board placement by the AI: board_state, layout_rules_from_datasheet (reads only the datasheet's layout pages),
  place_parts (AI gives intent - near pin X within N mm, edge, fixed - code finds positions inside the outline,
  no overlaps, short ratsnest). Locked and board-sized parts never move.
- You approve every placement in a preview, then either send it to Eagle (F12 applies, undo works) or, with Eagle
  closed, write the .brd directly (timestamped backup kept; warns about routed traces).
- NOT yet run inside Eagle 7.4: the ULP and the ROTATE/MIRROR/MOVE script - first use: try on a copy of a board.

## 0.5.1 - 2026-10-08
- No coil button. Library parts are made by the AI whenever the design needs one search_library doesn't have:
  from a datasheet PDF if there is one, otherwise designed by the AI with build_lbr - template packages, or
  'custom' parts with any geometry (pads with shape/rotation/roundness, copper traces and arcs, polygons, board
  outline, silkscreen, mask openings, keepouts, non-plated holes, text). PCB-trace coils stay an AI tool (exact code).
- Every built part returns a geometry report (closest pads, overlaps, drill and annular-ring minimums); the AI fixes
  problems and rebuilds before handing it over.
- The chat AI's web search can now look at any site (e.g. manufacturer land patterns); JLC sourcing stays on
  jlcpcb.com / lcsc.com.

## 0.5.0 - 2026-10-08
- PCB coil generator ("PCB coil…" toolbar, and the AI's build_coil tool): racetrack or round spiral coils as
  Eagle 7.4 library parts. Exact geometry from code with true Eagle arcs - continuous, every turn exactly one gap
  apart. One or two layers (two = series, same direction; centre pad = CT). Live preview, DC resistance, and an
  inductance estimate for round coils. Eagle 7 packages can't hold vias, so the centre uses a through-hole pad.
- Choose where new libraries are saved: a Save dialog for coils and Datasheet → part; pick an existing .lbr to ADD
  the part to it (same-name part replaced, others kept). Settings: "New libraries go to" folder + "Ask every time"
  (also applies when the AI builds a part). Chosen folders are added to the scanned library folders.

## 0.4.6 - 2026-10-08
- JLC parts memory shared by all projects (Documents\EagleAI\jlc_memory.json). Every LCSC number confirmed at
  JLC (priced, applied from the AI search, or assigned by the AI) is remembered by library part + value, part
  number, and passive value + size (capacitors keep their voltage). New projects fill these first - free, no AI -
  after a fresh stock check; if your usual part is out of stock, the normal search runs instead.
- "My JLC parts…" in the BOM: see what's remembered, forget entries.

## 0.4.5 - 2026-10-08
- JLC part numbers found like a person would: "Find missing with AI (web)" in the BOM - Claude searches
  jlcpcb.com / lcsc.com (smallest-buy pricing, not reel; Basic vs Extended fee; in stock; footprint fit;
  pin-compatible second sources). Every number is re-checked at JLC before you apply it; package changes flagged.
- The chat AI (Claude) can web-search JLC/LCSC too; jlc_assign now refuses numbers JLC doesn't list; new jlc_lookup.
- "Paste LCSC list…": paste Eagle ATTRIBUTE lines or a table from a Claude chat - numbers go into the BOM.
- Free local search: capacitors by value + size + voltage (>= 16 V if not given), part numbers tried with
  -TR / TRPBF / (ES) variants and from the library device name; existing LCSC numbers looked up exactly.
- Web searches are counted in the running cost ($10 per 1,000).

## 0.4.4 - 2026-10-08
- Library auto-detect lists folders directly (finds C:\\EAGLE-7.4.0\\lbr). Libraries tab shows how many .lbr files each folder gave.

## 0.4.3 - 2026-10-08
- Readable colors: one high-contrast dark theme for the whole app (no more light/dark clashes).
- Libraries: "Add library folder…" (Libraries tab and Settings), "Auto-detect Eagle folders", wider auto-detect (Eagle 6-9, Program Files, Documents, OneDrive), and a clear message when no libraries are found. An empty saved folder list now re-detects instead of staying empty.

## 0.4.2 - 2026-10-08
- Renamed to "Eagle AI Design Studio" everywhere (window title, About, launcher, README). Settings and recent projects are kept.

## 0.4.1 - 2026-10-08
- Live BOM: "BOM (live)" tab + pop-out window (toolbar / Ctrl+B), always rebuilt from the design - never old data.
- Edit Value, MPN, LCSC, Note and DNP right in the BOM; edits go into the design (undo + noted in the AI chat).
- Exported BOMs are checked live against the design: green = matches, red = out of date with the exact
  differences; edits made to the exported .xlsx in Excel are detected and can be pulled back into the design.
- Prices only show while they belong to the part's current LCSC number, with their age.
- DNP parts stay in the schematic but are left out of JLC files.

## 0.4.0 - 2026-10-08
- AI brand switcher: Claude, ChatGPT (OpenAI) and Grok (xAI), each with its own model list.
- "Review with…" - a second AI double-checks the design read-only (catches hallucinated pins/nets).
- Chat history is persistent per project and shared by all brands (compact summary, low token cost).
- Recent projects menu (persistent).
- JLC export: BOM + CPL as Excel (.xlsx) from the .brd, Top / Bottom / Both selector, LCSC numbers,
  pad-centre placement, missing-LCSC rows highlighted, optional rotation offsets.
- Ultra Librarian zip importer (Eagle script / Eagle lbr / Fusion 360 lbr + 3D models), destination dialog,
  Fusion libraries down-converted to an Eagle 7.4 copy, 3D files kept for Fusion 360.
- Local helper scripts: the AI can write small Python tools that run on your PC after you approve them.
- Version number in the title bar and Help > About.

## 0.3.0
- Token savings: local part matching, part memory, batch tools, caching, trimmed history, datasheet page selection.

## 0.2.0
- Open existing projects (.sch / .md import), recursive library scan, binary library conversion, new-model check.

## 0.1.0
- First version: library index, design model, .sch writer, LBR builder, SVG preview, JLC pricing, cost tracking.
