"""Calculators: the math against known answers, every tab shows a result for its defaults, Undo sits in the chat."""
import math, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eagleai import calc as C


def close(a, b, rel=1e-6):
    return math.isclose(a, b, rel_tol=rel)


def test_parse_and_format():
    for s, v in (("10k", 10e3), ("4k7", 4700), ("4R7", 4.7), ("330R", 330), ("100nF", 1e-7), ("0.1uF", 1e-7),
                 ("0.1µF", 1e-7), ("2.2M", 2.2e6), ("2.2 MΩ", 2.2e6), ("20m", 0.02), ("20mA", 0.02), ("1e-6", 1e-6),
                 ("2n2", 2.2e-9), ("10kHz", 1e4)):
        assert close(C.parse(s), v), (s, C.parse(s))
    assert C.parse_list("10k,4k7 2.2k; 1k") == [10e3, 4700, 2200, 1000]
    assert C.fmt(4700, "Ω") == "4.7 kΩ" and C.fmt(1e-7, "F") == "100 nF" and C.fmt(0.02, "A") == "20 mA"
    try:
        C.parse("abc"); assert False
    except C.CalcError:
        pass


def test_series_parallel_and_standard_values():
    assert C.series_total([10e3, 4700]) == 14700 and close(C.parallel_total([10e3, 10e3]), 5e3)
    assert C.nearest(4800, "E24") == 4700 and C.nearest(4800, "E24", "up") == 5100
    assert C.nearest(1234, "E96") == 1240 and len(C.E96) == 96 and C.E96[:4] == [1.0, 1.02, 1.05, 1.07]
    a, b, r = C.best_pairs(5e3, "E24", "parallel")[0]
    assert close(r, 5e3) and close(C.parallel_total([a, b]), 5e3)
    a, b, r = C.best_pairs(15.5e3, "E24", "series")[0]
    assert abs(r - 15.5e3) / 15.5e3 < 0.002


def test_led():
    r = C.led_resistor(5, 2.0, 20)
    assert close(r["r"], 150) and r["r_std"] == 150 and close(r["p_r"], 0.06) and r["package"] == "0805"
    r = C.led_resistor(12, 3.1, 20, n_series=3, n_strings=4)              # 12 V, 3 white LEDs per string
    assert close(r["r"], 135) and r["r_std"] == 150 and r["i_act"] < 0.02 and close(r["i_total"], 4 * r["i_act"])
    try:
        C.led_resistor(5, 3.0, 20, n_series=2); assert False
    except C.CalcError as e:
        assert "max 1" in str(e)


def test_capacitor_codes_and_units():
    assert C.cap_code(100e-9) == "104" and C.cap_code(4.7e-12) == "4R7" and C.cap_code(2.2e-6) == "225"
    assert C.cap_code(10e-12) == "100" and C.cap_code(1e-9) == "102" and C.cap_code(123e-12).startswith("~")
    assert close(C.decode_cap_code("104"), 100e-9) and close(C.decode_cap_code("473K"), 47e-9)
    assert close(C.decode_cap_code("4R7"), 4.7e-12)
    u = C.cap_units(100e-9)
    assert close(u["pF"], 1e5) and close(u["nF"], 100) and close(u["µF"], 0.1)


def test_smd_resistor_codes():
    assert C.decode_smd_resistor("103")[0] == 10e3 and C.decode_smd_resistor("1002")[0] == 10e3
    assert close(C.decode_smd_resistor("01C")[0], 10e3) and close(C.decode_smd_resistor("4R7")[0], 4.7)
    assert close(C.decode_smd_resistor("R10")[0], 0.1) and C.decode_smd_resistor("000")[0] == 0
    assert C.encode_smd_resistor(10e3) == {"3-digit": "103", "4-digit": "1002", "EIA-96": "01C"}
    assert C.encode_smd_resistor(4.7) == {"3-digit": "4R7", "4-digit": "4R70", "EIA-96": "-"}
    assert C.encode_smd_resistor(12.4e3)["EIA-96"] == "10C"


def test_ohm_divider_rc():
    r = C.ohms_law(v=12, r=1000)
    assert close(r["i"], 0.012) and close(r["p"], 0.144)
    r = C.ohms_law(r=100, p=0.25)
    assert close(r["v"], 5) and close(r["i"], 0.05)
    assert close(C.divider(5, 10e3, 10e3)["vout"], 2.5)
    best = C.best_dividers(3.3, 0.8, "E24")[0]                           # regulator: Vout 3.3 V, Vref 0.8 V
    assert abs(best["err"]) < 0.001
    d = C.rc_filter(10e3, 100e-9)
    assert close(d["tau"], 1e-3) and close(d["fc"], 159.1549, 1e-5)
    assert close(C.r_for_cutoff(159.1549, 100e-9), 10e3, 1e-5)
    assert close(C.lc_resonance(10e-6, 100e-9), 159154.94, 1e-5)


def test_dialog_tabs_and_buttons():
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    tmp = Path(tempfile.mkdtemp())
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp / "qs"))
    app = QApplication.instance() or QApplication([])
    from eagleai.calc_view import CalculatorsDialog
    dlg = CalculatorsDialog()
    for title, page in dlg.pages.items():                                 # defaults give a result, not an error
        txt = page.out.text()
        assert "<table" in txt and "ffb454" not in txt and "ff8a80" not in txt, (title, txt)
    led = dlg.pages["LED resistor"]
    led.inputs["ns"].setText("3")                                         # 3 x 2 V on 5 V: impossible -> message
    assert "ffb454" in led.out.text() and "max 2" in led.out.text()
    r = dlg.pages["R series/parallel"]
    r.inputs["vals"].setText("10k 10k")
    assert "5 kΩ" in r.out.text()
    from eagleai.app import MainWindow
    w = MainWindow()
    labels = [a.text() for a in w.findChildren(type(w.actions()[0])) if a.text()]
    assert "Calculators" in labels and "Undo AI edit" not in labels
    assert w.undo_btn.text() == "Undo AI edit" and w.undo_btn.parentWidget() is w.send_btn.parentWidget()
    w.show_calculators()
    assert w.calc_window.isVisible()


if __name__ == "__main__":
    import traceback
    rc = 0
    try:
        for n, fn in list(globals().items()):
            if n.startswith("test_"):
                fn(); print("ok", n)
    except Exception:
        traceback.print_exc(); rc = 1
    sys.stdout.flush(); sys.stderr.flush()
    if os.name == "nt":                     # end like the app does: Qt teardown with live workers can crash
        from eagleai.app import terminate_self
        terminate_self(rc)
    os._exit(rc)
