"""Runs ONE Windows file/folder dialog in its own process and prints the result as JSON.

Why a separate process: Windows' file dialog loads every Explorer add-on (cloud drives, archivers, git overlays,
preview handlers...) into the program that shows it. When one of them is broken it crashes that program. Shown
from here, a crash only takes down this small helper - the studio notices, logs the exit code and falls back.

Tk 8.6 on Windows uses the modern Explorer dialog (IFileDialog), so it looks the same as any other app.
Usage: python native_dialog_child.py '<json request>'  ->  stdout: {"ok": true, "path": "..."}  ("" = cancelled)
"""
import json
import sys


def main():
    req = json.loads(sys.argv[1])
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    # an invisible 1x1 owner window that is on top and focused, so the dialog opens in front of the studio
    root.overrideredirect(True)
    root.geometry("1x1+{}+{}".format(root.winfo_screenwidth() // 2, root.winfo_screenheight() // 3))
    try:
        root.attributes("-alpha", 0.0)
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    root.deiconify()
    root.lift()
    root.focus_force()
    root.update()
    kind = req.get("kind")
    common = {"parent": root, "title": req.get("title") or "", "initialdir": req.get("dir") or None}
    types = [tuple(t) for t in req.get("types") or []] or [("All files", "*.*")]
    if kind == "folder":
        path = filedialog.askdirectory(mustexist=False, **common)
    elif kind == "open":
        path = filedialog.askopenfilename(filetypes=types, **common)
    else:
        kw = dict(filetypes=types, initialfile=req.get("file") or "", defaultextension=req.get("ext") or "")
        if req.get("no_confirm"):
            kw["confirmoverwrite"] = False
        try:
            path = filedialog.asksaveasfilename(**kw, **common)
        except tk.TclError:                       # very old Tk without -confirmoverwrite
            kw.pop("confirmoverwrite", None)
            path = filedialog.asksaveasfilename(**kw, **common)
    root.destroy()
    sys.stdout.write(json.dumps({"ok": True, "path": path or ""}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
