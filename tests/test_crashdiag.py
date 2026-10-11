"""Dialog breadcrumbs: a session that died inside a dialog is detected on the next start."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eagleai import crashdiag as cd


def test_breadcrumbs():
    cd.LOG = Path(tempfile.mkdtemp()) / "dialog_debug.log"
    cd.session_start('safe')
    t = cd.opened("folder", "Project folder", "C:/x", "safe")
    cd.closed("folder", t, "C:/x/proj")
    cd.session_end()
    cd.session_start('safe')
    assert cd.last_session_died_in_dialog() is None              # clean exit
    cd.opened("folder", "Project folder (created if needed)", "", "inproc")   # ...and the app dies here
    cd.session_start('safe')                                         # next start
    died = cd.last_session_died_in_dialog()
    assert died and "OPEN folder mode=inproc" in died and "Project folder" in died
    cd.session_start('safe')
    assert cd.last_session_died_in_dialog() is None               # only reported once
    r = cd.report('safe')
    assert "File dialog trace" in r and ("only available on Windows" in r or "Windows crash / freeze records" in r)


def test_parse_event():
    msg = ("Faulting application name: pythonw.exe, version: 3.12.4150.1013, time stamp: 0x6655\r\n"
           "Faulting module name: DropboxExt64.52.dll, version: 0.0.0.0, time stamp: 0x0\r\n"
           "Exception code: 0xc0000005\r\nFault offset: 0x0000000000012345\r\n"
           "Faulting module path: C:\\Program Files\\Dropbox\\Client\\DropboxExt64.52.dll\r\n")
    e = cd.parse_event(msg)
    assert e["app"] == "pythonw.exe" and e["module"] == "DropboxExt64.52.dll", e
    assert e["exception"] == "0xc0000005" and e["module_path"].endswith("DropboxExt64.52.dll"), e


def test_parse_events_crashes_and_hangs():
    text = ("Event[0]:\n  Date: 2026-10-09T20:22:01\n  Description: \nFaulting application name: python.exe, "
            "version: 3.13\nFaulting module name: pyside6.abi3.dll, version: 0.0.0.0\nException code: 0xc0000005\n"
            "Fault offset: 0x1\nFaulting module path: C:\\venv\\pyside6.abi3.dll\n"
            "Event[1]:\n  Date: 2026-10-09T20:19:38\n  Description: \nThe program pythonw.exe version 3.13.9150.1013 "
            "stopped interacting with Windows and was closed.\n"
            "Event[2]:\n  Date: 2026-10-09T20:05:20\n  Description: \nThe program notepad.exe version 10.0.19041 "
            "stopped interacting with Windows and was closed.\n"
            "Event[3]:\n  Date: 2026-10-09T19:00:00\n  Description: \nFaulting application name: dwm.exe, "
            "version: 10\nFaulting module name: dwmcore.dll\n")
    events, others = cd.parse_events(text)
    assert [e["kind"] for e in events] == ["crash", "hang"], events
    assert events[0]["module"] == "pyside6.abi3.dll" and events[1]["app"] == "pythonw.exe"
    assert others == ["2026-10-09T20:05:20  notepad.exe"], others


if __name__ == "__main__":
    test_breadcrumbs(); test_parse_event(); test_parse_events_crashes_and_hangs(); print("ok crashdiag")
