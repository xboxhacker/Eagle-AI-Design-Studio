"""Launcher: never die silently. Every failure goes to the console AND crash.log."""
import faulthandler
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
CRASH = HERE / "crash.log"
# Hard crashes inside Qt/Windows (no Python traceback) are written here. Appended, not overwritten, so the
# crash from the previous run is still there after you restart; trimmed when it gets big.
_FH = HERE / "faulthandler.log"
try:
    if _FH.exists() and _FH.stat().st_size > 512_000:
        _FH.write_text(_FH.read_text(errors="replace")[-200_000:])
except OSError:
    pass
_fh = open(_FH, "a")
import datetime as _dt
_fh.write(f"\n=== session started {_dt.datetime.now():%Y-%m-%d %H:%M:%S} ===\n"); _fh.flush()
faulthandler.enable(_fh, all_threads=True)


def _version() -> str:
    try:
        from eagleai import __version__
        return __version__
    except Exception:
        return "?"


def report(text: str, show_box: bool = True):
    import datetime
    text = f"[Eagle AI Design Studio {_version()} - {datetime.datetime.now():%Y-%m-%d %H:%M}]\n{text}"
    print(text, file=sys.stderr)
    try:
        with open(CRASH, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    if show_box:
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance():
                QMessageBox.critical(None, "Eagle AI Design Studio error", text[-3000:])
        except Exception:
            pass


def hook(t, v, tb):
    # PySide6 routes exceptions raised inside button handlers here
    report("".join(traceback.format_exception(t, v, tb)))


sys.excepthook = hook

if "--check" in sys.argv:
    print("Eagle AI Design Studio", _version())
    print("python", sys.version)
    for mod in ("PySide6", "anthropic", "openai", "keyring", "pypdf", "openpyxl"):
        try:
            m = __import__(mod)
            print("ok  ", mod, getattr(m, "__version__", ""))
        except Exception as exc:
            print("FAIL", mod, exc)
    sys.exit(0)

try:
    from eagleai.app import main
except Exception:
    report("Startup import failed:\n" + traceback.format_exc(), show_box=False)
    sys.exit(1)

try:
    main()
except SystemExit as e:
    sys.exit(e.code)
except Exception:
    report("Crash:\n" + traceback.format_exc())
    sys.exit(1)
