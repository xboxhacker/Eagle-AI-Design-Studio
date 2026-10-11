"""Calculators window: one tab per calculator, results update as you type. Values accept 10k, 4k7, 4R7, 100n,
0.1u, 2.2M (m = milli, M = mega). Math is in calc.py."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QStackedWidget, QVBoxLayout, QWidget)

from . import calc as C
from .calc import CalcError, fmt

OHM, FARAD = "Ω", "F"


def _opt(s: str):
    return C.parse(s) if (s or "").strip() else None


def _table(rows) -> str:
    return "<table cellspacing='0' cellpadding='3'>" + "".join(
        f"<tr><td style='color:#9aa4b2'>{html.escape(a)}</td><td><b>{b}</b></td></tr>" for a, b in rows) + "</table>"


def _note(t: str) -> str:
    return f"<p style='color:#9aa4b2'>{t}</p>"


class CalcTab(QWidget):
    """fields: (key, label, default[, [choices]]); fn(values: dict of str) -> HTML."""

    def __init__(self, fields, fn, help_text: str = ""):
        super().__init__()
        self.fn, self.inputs = fn, {}
        lay = QVBoxLayout(self)
        form = QFormLayout()
        for key, label, default, *choices in fields:
            if choices:
                w = QComboBox(); w.addItems(choices[0]); w.setCurrentText(default)
                w.currentTextChanged.connect(self.update_result)
            else:
                w = QLineEdit(default); w.textChanged.connect(self.update_result)
                w.setClearButtonEnabled(True)
            self.inputs[key] = w
            form.addRow(label, w)
        lay.addLayout(form)
        if help_text:
            h = QLabel(help_text); h.setWordWrap(True); h.setStyleSheet("color:#9aa4b2;")
            lay.addWidget(h)
        self.out = QLabel(); self.out.setTextFormat(Qt.RichText); self.out.setWordWrap(True)
        self.out.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.out.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.out.setStyleSheet("QLabel{background:#15171c; border:1px solid #3a4150; border-radius:4px; "
                               "padding:8px; font-size:10.5pt;}")
        lay.addWidget(self.out, 1)
        self.update_result()

    def values(self):
        return {k: (w.currentText() if isinstance(w, QComboBox) else w.text()) for k, w in self.inputs.items()}

    def update_result(self, *_):
        try:
            self.out.setText(self.fn(self.values()))
        except CalcError as exc:
            self.out.setText(f"<span style='color:#ffb454'>{html.escape(str(exc))}</span>")
        except (ValueError, ZeroDivisionError, OverflowError) as exc:
            self.out.setText(f"<span style='color:#ff8a80'>Check the inputs ({html.escape(str(exc))})</span>")


# ------------------------------------------------------------------ the calculators
def resistors(v):
    out = []
    if v["vals"].strip():
        vals = C.parse_list(v["vals"])
        s, p = C.series_total(vals), C.parallel_total(vals)
        out.append(_table([("Values", ", ".join(fmt(x, OHM) for x in vals)),
                           ("Series", f"{fmt(s, OHM)}  (nearest {v['ser']}: {fmt(C.nearest(s, v['ser']), OHM)})"),
                           ("Parallel", f"{fmt(p, OHM)}  (nearest {v['ser']}: {fmt(C.nearest(p, v['ser']), OHM)})")]))
    if v["target"].strip():
        t = C.parse(v["target"])
        rows = [(f"{fmt(a, OHM)} + {fmt(b, OHM)}", f"{fmt(r, OHM)}  ({(r - t) / t * 100:+.2f} %)")
                for a, b, r in C.best_pairs(t, v["ser"], "series", 3)]
        rows += [(f"{fmt(a, OHM)} ∥ {fmt(b, OHM)}", f"{fmt(r, OHM)}  ({(r - t) / t * 100:+.2f} %)")
                 for a, b, r in C.best_pairs(t, v["ser"], "parallel", 3)]
        out.append(f"<p><b>Make {fmt(t, OHM)} from two {v['ser']} resistors</b></p>" + _table(rows))
    if not out:
        raise CalcError("enter values to combine, or a target value")
    return "".join(out)


def capacitors(v):
    out = []
    if v["vals"].strip():
        vals = C.parse_list(v["vals"])
        p, s = C.series_total(vals), C.parallel_total(vals)       # caps: parallel adds, series is reciprocal
        out.append(_table([("Values", ", ".join(fmt(x, FARAD) for x in vals)),
                           ("Parallel", f"{fmt(p, FARAD)}  (nearest {v['ser']}: {fmt(C.nearest(p, v['ser']), FARAD)})"),
                           ("Series", f"{fmt(s, FARAD)}  (nearest {v['ser']}: {fmt(C.nearest(s, v['ser']), FARAD)})")]))
        out.append(_note("Series capacitors: the voltage splits in inverse proportion to capacitance - "
                         "the smallest cap sees the most voltage. Each needs its own voltage rating."))
    if v["target"].strip():
        t = C.parse(v["target"])
        rows = [(f"{fmt(a, FARAD)} ∥ {fmt(b, FARAD)}", f"{fmt(r, FARAD)}  ({(r - t) / t * 100:+.2f} %)")
                for a, b, r in C.best_pairs(t, v["ser"], "series", 3)]
        rows += [(f"{fmt(a, FARAD)} in series with {fmt(b, FARAD)}", f"{fmt(r, FARAD)}  ({(r - t) / t * 100:+.2f} %)")
                 for a, b, r in C.best_pairs(t, v["ser"], "parallel", 3)]
        out.append(f"<p><b>Make {fmt(t, FARAD)} from two {v['ser']} capacitors</b></p>" + _table(rows))
    if not out:
        raise CalcError("enter values to combine, or a target value")
    return "".join(out)


def led(v):
    r = C.led_resistor(C.parse(v["vs"]), C.parse(v["vf"]), C.parse(v["ima"]), int(C.parse(v["ns"])),
                       int(C.parse(v["np"])), v["ser"])
    strings = int(C.parse(v["np"]))
    rows = [("Exact resistor", fmt(r["r"], OHM)),
            (f"Use ({v['ser']}, next value up)", fmt(r["r_std"], OHM)),
            ("LED current", fmt(r["i_act"], "A")),
            ("Resistor dissipation", f"{fmt(r['p_r'], 'W')}  →  use ≥ {fmt(r['rating'], 'W')} ({r['package']})"),
            ("Resistors needed", f"{strings} (one per string - never share one resistor between parallel strings)"),
            ("Total current", fmt(r["i_total"], "A")),
            ("Total power", f"{fmt(r['p_total'], 'W')}  (LEDs {fmt(r['p_leds'], 'W')}, efficiency "
                            f"{r['efficiency'] * 100:.0f} %)")]
    warn = _note("<span style='color:#ffb454'>Less than 10 % of the supply is left across the resistor - the current "
                 "will change a lot with supply and LED voltage. Use fewer LEDs per string.</span>") \
        if r["headroom_warn"] else ""
    return _table(rows) + warn


def cap_units(v):
    out = []
    if v["val"].strip():
        f = C.parse(v["val"])
        u = C.cap_units(f)
        out.append(_table([(k, f"{x:.6g}") for k, x in u.items() if k != "mF" or f >= 1e-4]
                          + [("Code (pF, EIA)", html.escape(C.cap_code(f)))]))
    if v["code"].strip():
        f = C.decode_cap_code(v["code"])
        out.append(f"<p><b>Code {html.escape(v['code'].strip().upper())}</b></p>"
                   + _table([(k, f"{x:.6g}") for k, x in C.cap_units(f).items() if k in ("pF", "nF", "µF")]))
    if not out:
        raise CalcError("enter a value (e.g. 0.1u, 100n, 4.7p) or a code (e.g. 104)")
    return "".join(out) + _note("Code = two digits + number of zeros, in pF: 104 = 10 x 10⁴ pF = 100 nF. R = decimal "
                                "point (4R7 = 4.7 pF). A trailing letter is the tolerance (J 5 %, K 10 %, M 20 %). "
                                "~ = not exact in 2 digits.")


def ohms(v):
    r = C.ohms_law(_opt(v["v"]), _opt(v["i"]), _opt(v["r"]), _opt(v["p"]))
    return _table([("Voltage", fmt(r["v"], "V")), ("Current", fmt(r["i"], "A")), ("Resistance", fmt(r["r"], OHM)),
                   ("Power", fmt(r["p"], "W"))])


def divider(v):
    out = []
    vin = C.parse(v["vin"])
    if v["r1"].strip() and v["r2"].strip():
        d = C.divider(vin, C.parse(v["r1"]), C.parse(v["r2"]))
        out.append(_table([("Vout", fmt(d["vout"], "V")), ("Divider current", fmt(d["i"], "A")),
                           ("R1 / R2 dissipation", f"{fmt(d['p1'], 'W')} / {fmt(d['p2'], 'W')}")]))
    if v["target"].strip():
        rows = [(f"R1 {fmt(d['r1'], OHM)}  R2 {fmt(d['r2'], OHM)}",
                 f"{fmt(d['vout'], 'V')}  ({d['err'] * 100:+.2f} %, {fmt(d['i'], 'A')})")
                for d in C.best_dividers(vin, C.parse(v["target"]), v["ser"])]
        out.append(f"<p><b>Best {v['ser']} pairs for {fmt(C.parse(v['target']), 'V')}</b></p>" + _table(rows))
    if not out:
        raise CalcError("enter R1 and R2, or a target output voltage")
    return "".join(out)


def rc(v):
    out = []
    r, c, l, fc = _opt(v["r"]), _opt(v["c"]), _opt(v["l"]), _opt(v["fc"])
    if r and c:
        d = C.rc_filter(r, c)
        out.append(_table([("Time constant τ = RC", fmt(d["tau"], "s")), ("-3 dB cutoff 1/(2πRC)", fmt(d["fc"], "Hz")),
                           ("Settles to 99 % (5τ)", fmt(d["t99"], "s"))]))
    if fc and c:
        out.append(_table([(f"R for {fmt(fc, 'Hz')} with {fmt(c, 'F')}", fmt(C.r_for_cutoff(fc, c), OHM))]))
    if l and c:
        out.append(_table([("LC resonance 1/(2π√LC)", fmt(C.lc_resonance(l, c), "Hz"))]))
    if not out:
        raise CalcError("enter R and C (RC filter), C and a cutoff (find R), or L and C (resonance)")
    return "".join(out)


def smd_code(v):
    out = []
    if v["code"].strip():
        ohms_, kind = C.decode_smd_resistor(v["code"])
        out.append(_table([(f"Code {html.escape(v['code'].strip().upper())}", f"{fmt(ohms_, OHM)}  ({kind})")]))
    if v["val"].strip():
        codes = C.encode_smd_resistor(C.parse(v["val"]))
        out.append(_table([(k, html.escape(x)) for k, x in codes.items()]))
    if not out:
        raise CalcError("enter a code from the part (103, 4R7, 1002, 01C) or a value")
    return "".join(out) + _note("3-digit: 2 digits + zeros (103 = 10 kΩ). 4-digit: 3 digits + zeros (1002 = 10 kΩ). "
                                "EIA-96: E96 index + letter (01C = 100 x 100 = 10 kΩ). R = decimal point. "
                                "~ = the value needs more digits than the code has.")


def trace(v):
    from .rules import MIL, trace_width_for_current
    amps = C.parse(v["a"])
    oz = float(v["oz"].split()[0])
    rise = C.parse(v["rise"])
    outer = v["layer"].startswith("Outer")
    w = trace_width_for_current(amps, oz, rise, outer)
    return _table([("Minimum width (IPC-2221)", f"{w:.2f} mm  ({w / MIL:.0f} mil)"),
                   ("Copper", f"{oz:g} oz ({oz * 35:.0f} µm), {'outer' if outer else 'inner'} layer, "
                              f"+{rise:g} °C")]) + \
        _note("Rounded up to 0.05 mm. Inner layers need about twice the width (no air cooling). Short necks into "
              "pads are fine; wider is always cooler.")


TABS = [
    ("R series/parallel", [("vals", "Resistors", "10k, 4k7"), ("target", "Target (optional)", ""),
                           ("ser", "Standard series", "E24", ["E12", "E24", "E96"])], resistors,
     "Separate values with commas or spaces. Target: the best two standard resistors that make it."),
    ("C series/parallel", [("vals", "Capacitors", "100n, 47n"), ("target", "Target (optional)", ""),
                           ("ser", "Standard series", "E12", ["E6", "E12", "E24"])], capacitors,
     "Parallel capacitors add up; in series they combine like parallel resistors."),
    ("LED resistor", [("vs", "Supply voltage (V)", "5"), ("vf", "LED forward voltage (V)", "2.0"),
                      ("ima", "LED current (mA)", "20"), ("ns", "LEDs in series per string", "1"),
                      ("np", "Strings in parallel", "1"), ("ser", "Standard series", "E24", ["E12", "E24", "E96"])],
     led, "Typical forward voltage: red / yellow 1.8-2.2 V, green 2.0-3.2 V, blue / white 2.8-3.4 V (see the "
          "datasheet at your current). Each string gets its own resistor."),
    ("Cap units / code", [("val", "Value", "0.1u"), ("code", "Code on the part", "")], cap_units,
     "Converts between pF, nF and µF and the 3-digit code printed on ceramic / film capacitors."),
    ("Ohm's law", [("v", "Voltage (V)", "12"), ("i", "Current (A)", ""), ("r", "Resistance (Ω)", "1k"),
                   ("p", "Power (W)", "")], ohms, "Fill in any two; the other two are calculated. 20m = 20 mA."),
    ("Divider / feedback", [("vin", "Input voltage (V)", "5"), ("r1", "R1 (top)", "10k"), ("r2", "R2 (bottom)", "10k"),
                            ("target", "Target output (V, optional)", "3.3"),
                            ("ser", "Standard series", "E24", ["E12", "E24", "E96"])], divider,
     "Vout = Vin x R2 / (R1 + R2). Regulator feedback pin: Input = the output voltage you want, Target = Vref "
     "from the datasheet (e.g. 0.8 V); R1 goes from the output to FB, R2 from FB to ground."),
    ("RC / LC", [("r", "R", "10k"), ("c", "C", "100n"), ("fc", "Wanted cutoff (Hz, optional)", ""),
                 ("l", "L (optional)", "")], rc, "Low-pass / high-pass RC cutoff, time constant, and LC resonance."),
    ("SMD resistor code", [("code", "Code on the part", "103"), ("val", "Value → code", "")], smd_code, ""),
    ("Trace width", [("a", "Current (A)", "1"), ("oz", "Copper", "1 oz", ["0.5 oz", "1 oz", "2 oz"]),
                     ("rise", "Allowed temperature rise (°C)", "10"),
                     ("layer", "Layer", "Outer (top / bottom)", ["Outer (top / bottom)", "Inner"])], trace, ""),
]


class CalculatorsDialog(QDialog):
    """A list of calculators on the left (all visible at once), the chosen one on the right."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Calculators")
        self.resize(820, 560)
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.setStyleSheet("QListWidget::item{padding:5px 6px;}")
        self.stack = QStackedWidget()
        self.pages = {}
        for title, fields, fn, help_text in TABS:
            page = CalcTab(fields, fn, help_text)
            self.pages[title] = page
            self.list.addItem(title)
            self.stack.addWidget(page)
        self.list.setFixedWidth(self.list.sizeHintForColumn(0) + 24)
        self.list.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.list.setCurrentRow(0)
        row.addWidget(self.list); row.addWidget(self.stack, 1)
        lay.addLayout(row, 1)
        hint = QLabel("Values: 10k, 4k7, 4R7, 100n, 0.1u, 2.2M  (m = milli, M = mega). Results can be selected "
                      "and copied.")
        hint.setStyleSheet("color:#9aa4b2;")
        lay.addWidget(hint)
