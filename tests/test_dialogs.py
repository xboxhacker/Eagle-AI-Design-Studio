"""Crash-safe file dialogs: helper result used; helper crash -> built-in dialog + note; no Tk -> in-app dialog."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QTimer
from eagleai import app as A, crashdiag

app = QApplication.instance() or QApplication([])
d = Path(tempfile.mkdtemp())
crashdiag.LOG = d / "dialog_debug.log"
QMessageBox.warning = staticmethod(lambda *a, **k: None)


def child(code):
    f = d / f"child_{abs(hash(code))}.py"
    f.write_text(code)
    A.CHILD = f


def close_modal(tries=60):
    """Close the built-in dialog; the helper's waiting box is left alone (closing it means 'cancel')."""
    w = app.activeModalWidget()
    if w is None or w.windowTitle() == "File dialog":
        if tries:
            QTimer.singleShot(100, lambda: close_modal(tries - 1))
        return
    w.reject()


def test_helper_result_and_types():
    A.DIALOG_MODE = "safe"
    child("import json,sys; r=json.loads(sys.argv[1]); print(json.dumps({'ok':True,'path':'C:/picked/' + r['kind']}))")
    assert A._dlg_dir(None, "Project folder") == "C:/picked/folder"
    assert A._dlg_open(None, "x", "", "Markdown (*.md)") == ("C:/picked/open", "")
    assert A._tk_types("Eagle library (*.lbr)") == [("Eagle library", "*.lbr"), ("All files", "*.*")]


def test_helper_crash_falls_back():
    A.DIALOG_MODE = "safe"
    child("import os; os.abort()")
    QTimer.singleShot(400, close_modal)
    assert A._dlg_dir(None, "Project folder") == ""              # built-in dialog shown (closed by the test)
    assert A._child_failed_note and "exit" in A._child_failed_note[-1]
    log = crashdiag.LOG.read_text()
    assert "HELPER FAILED" in log and "folder (fallback Qt)" in log


def _wait_box():
    from PySide6.QtWidgets import QDialog
    return next(w for w in app.topLevelWidgets() if isinstance(w, QDialog) and w.windowTitle() == "File dialog"
                and w.isVisible())


def test_closing_wait_box_stops_helper():
    A.DIALOG_MODE = "safe"
    A._child_failed_note.clear()
    pid_file = d / "hung_pid.txt"
    child(f"import os,time; open(r'{pid_file}','w').write(str(os.getpid())); time.sleep(60)")   # a frozen dialog
    QTimer.singleShot(1500, lambda: _wait_box().reject())          # the X / Esc, not the Cancel button
    assert A._dlg_dir(None, "Project folder") == ""
    assert not A._child_failed_note                                 # not reported as a crash
    assert "HELPER FAILED" not in crashdiag.LOG.read_text().splitlines()[-1]
    import subprocess
    pid = pid_file.read_text().strip()
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
    assert pid not in out, "helper still running"


def test_builtin_instead_of_frozen_helper():
    A.DIALOG_MODE = "safe"
    child("import time; time.sleep(60)")

    def press():
        from PySide6.QtWidgets import QPushButton
        b = next(b for b in _wait_box().findChildren(QPushButton) if b.text().startswith("Use built-in"))
        b.click()
        QTimer.singleShot(400, close_modal)                          # the built-in dialog that follows
    QTimer.singleShot(1500, press)
    assert A._dlg_open(None, "x", "", "PDF (*.pdf)") == ("", "")
    log = crashdiag.LOG.read_text()
    assert "stopped by the user" in log and "open (built-in)" in log


def test_terminate_self_really_terminates():
    import subprocess
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, r'%s'); from eagleai.app import "
                        "terminate_self; terminate_self(7); print('STILL ALIVE')" % root],
                       capture_output=True, text=True, env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert r.returncode == 7 and "STILL ALIVE" not in r.stdout, (r.returncode, r.stdout, r.stderr[-500:])


def test_no_tk_uses_inproc():
    A.DIALOG_MODE = "safe"
    child("import sys; sys.stderr.write(\"ModuleNotFoundError: No module named 'tkinter'\"); sys.exit(1)")
    QTimer.singleShot(400, close_modal)
    A._dlg_open(None, "x", "", "PDF (*.pdf)")
    assert "no Tk" in crashdiag.LOG.read_text()


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn(); print("ok", n)
